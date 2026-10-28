# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Record clipboard history in order on one serial background worker."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import partial
from threading import BoundedSemaphore
from typing import TYPE_CHECKING

import addonHandler
from logHandler import log
import ui

from .background import queueMainThread
from .clipboard.payload import MAX_IMAGE_BYTES, getPngImageInfo, isImageSizeSafe
from .clipboard.monitor import ClipboardContentType, ClipboardSnapshot
from .clipboard.imageCodec import ImageDataTooLargeError, normalizeImageDataToPng
from .storage.models import ClipboardItem, ClipboardItemType

if TYPE_CHECKING:
	from .storage.database import ClipboardStorage


addonHandler.initTranslation()

# Content types whose snapshots carry an image.
IMAGE_CONTENT_TYPES = frozenset({ClipboardContentType.IMAGE, ClipboardContentType.TEXT_AND_IMAGE})


@dataclass(frozen=True, slots=True)
class HistoryWriteResult:
	"""Describe the outcome of one ordered history write."""

	itemId: int | None
	imageWasDropped: bool = False
	imageWasTooLarge: bool = False


def clipboardItemFromSnapshot(snapshot: ClipboardSnapshot) -> ClipboardItem | None:
	"""Convert one supported snapshot to the fixed immutable storage model."""
	if snapshot.contentType == ClipboardContentType.FILES and snapshot.files:
		return ClipboardItem(
			ClipboardItemType.FILES,
			files=snapshot.files,
			canUpload=snapshot.canUpload,
		)
	if snapshot.contentType not in (
		ClipboardContentType.TEXT,
		ClipboardContentType.FORMATTED_TEXT,
		ClipboardContentType.TEXT_AND_IMAGE,
		ClipboardContentType.IMAGE,
	):
		return None
	if snapshot.imageData is not None:
		assert snapshot.imageFormat == "PNG"
		hasText = bool(snapshot.text)
		contentType = ClipboardItemType.TEXT_AND_IMAGE if hasText else ClipboardItemType.IMAGE
		return ClipboardItem(
			contentType,
			text=snapshot.text,
			html=snapshot.html if hasText else None,
			rtf=snapshot.rtf if hasText else None,
			imageData=snapshot.imageData,
			imageWidth=snapshot.imageWidth,
			imageHeight=snapshot.imageHeight,
			imageBitDepth=snapshot.imageBitDepth,
			canUpload=snapshot.canUpload,
		)
	if not snapshot.text:
		return None
	contentType = (
		ClipboardItemType.FORMATTED_TEXT
		if snapshot.html is not None or snapshot.rtf is not None
		else ClipboardItemType.PLAIN_TEXT
	)
	return ClipboardItem(
		contentType,
		text=snapshot.text,
		html=snapshot.html,
		rtf=snapshot.rtf,
		canUpload=snapshot.canUpload,
	)


def normalizeImageSnapshot(snapshot: ClipboardSnapshot) -> tuple[ClipboardSnapshot, bool]:
	"""Return a PNG-backed snapshot and whether normalization exceeded the byte limit.

	The monitor has already verified PNG data and applied its DIB and bitmap fallbacks.
	"""
	if snapshot.contentType not in IMAGE_CONTENT_TYPES:
		return snapshot, False
	canDecodeSafely = isImageSizeSafe(
		snapshot.imageWidth,
		snapshot.imageHeight,
		max(32, snapshot.imageBitDepth),
	)
	if not canDecodeSafely:
		return replace(snapshot, imageFormat=None, imageData=None), False
	try:
		pngData = (
			normalizeImageDataToPng(
				snapshot.imageFormat,
				snapshot.imageData,
				(snapshot.imageWidth, snapshot.imageHeight, snapshot.imageBitDepth),
			)
			if snapshot.imageData is not None
			else None
		)
	except ImageDataTooLargeError:
		return replace(snapshot, imageFormat=None, imageData=None), True
	info = getPngImageInfo(pngData) if pngData is not None else None
	if pngData is not None and info is not None and isImageSizeSafe(info[0], info[1], max(32, info[2])):
		return (
			replace(
				snapshot,
				imageFormat="PNG",
				imageData=pngData,
				imageWidth=info[0],
				imageHeight=info[1],
				imageBitDepth=info[2],
			),
			False,
		)
	return replace(snapshot, imageFormat=None, imageData=None), False


class HistoryWriter:
	"""Queue history records on one serial worker and report their results on NVDA's main thread.

	The same worker also runs the controller's other clipboard preparation jobs, so all of them
	keep their submission order.
	"""

	def __init__(
		self,
		storage: ClipboardStorage,
		*,
		isStarted: Callable[[], bool],
		onItemStored: Callable[[HistoryWriteResult, str | None], None],
	) -> None:
		"""Create the serial worker; ``onItemStored`` runs on the main thread after each stored record."""
		super().__init__()
		self._storage = storage
		self._isControllerStarted = isStarted
		self._onItemStored = onItemStored
		self._executor = ThreadPoolExecutor(
			max_workers=1,
			thread_name_prefix="nvdaClipboard.historyWrite",
		)
		self._imageSlots = BoundedSemaphore(2)
		self._saveFailureReported = False

	def submit[*Ts, T](self, fn: Callable[[*Ts], T], /, *args: *Ts) -> Future[T]:
		"""Run ``fn(*args)`` on the serial worker after every previously queued job."""
		return self._executor.submit(fn, *args)

	def shutdown(self) -> None:
		"""Wait for queued jobs and stop the serial worker."""
		self._executor.shutdown(wait=True)

	def queueItem(
		self,
		item: ClipboardItem,
		imageWasDropped: bool = False,
		*,
		replaceText: str | None = None,
		expectedText: str | None = None,
	) -> None:
		"""Queue one immutable item for ordered history storage."""
		future = self._executor.submit(self._storeItem, item, imageWasDropped, replaceText)
		future.add_done_callback(
			partial(self._queueWriteCompletion, expectedText=expectedText),
		)

	def queueImageSnapshot(self, snapshot: ClipboardSnapshot) -> None:
		"""Queue one image snapshot without allowing unbounded retained image data."""
		imageDataMissing = snapshot.imageData is None
		if imageDataMissing or not self._imageSlots.acquire(blocking=False):
			item = clipboardItemFromSnapshot(
				replace(snapshot, imageFormat=None, imageData=None),
			)
			if item is None and imageDataMissing:
				# Translators: Message shown when a clipboard image could not be normalized for history.
				ui.message(_("The image could not be added to clipboard history"))
			elif item is not None:
				self.queueItem(item, imageWasDropped=imageDataMissing)
			return
		try:
			future = self._executor.submit(self._storeImageSnapshot, snapshot)
		except Exception:
			self._imageSlots.release()
			raise
		future.add_done_callback(self._queueImageWriteCompletion)

	def _storeImageSnapshot(self, snapshot: ClipboardSnapshot) -> HistoryWriteResult:
		"""Normalize and store one retained image snapshot on the serial worker."""
		snapshot, imageWasTooLarge = normalizeImageSnapshot(snapshot)
		item = clipboardItemFromSnapshot(snapshot)
		if item is None:
			return HistoryWriteResult(
				None,
				imageWasDropped=True,
				imageWasTooLarge=imageWasTooLarge,
			)
		return self._storeItem(
			item,
			imageWasDropped=snapshot.imageData is None,
			imageWasTooLarge=imageWasTooLarge,
		)

	def _storeItem(
		self,
		item: ClipboardItem,
		imageWasDropped: bool,
		replaceText: str | None = None,
		imageWasTooLarge: bool = False,
	) -> HistoryWriteResult:
		"""Write history on the serial worker, preserving text when a mixed image is rejected."""
		itemId = self._storage.addHistory(item, replaceText=replaceText)
		if itemId is not None or item.contentType != ClipboardItemType.TEXT_AND_IMAGE:
			return HistoryWriteResult(
				itemId,
				imageWasDropped=imageWasDropped,
				imageWasTooLarge=imageWasTooLarge or len(item.imageData or b"") > MAX_IMAGE_BYTES,
			)
		fallbackType = (
			ClipboardItemType.FORMATTED_TEXT
			if item.html is not None or item.rtf is not None
			else ClipboardItemType.PLAIN_TEXT
		)
		fallbackId = self._storage.addHistory(
			ClipboardItem(
				fallbackType,
				text=item.text,
				html=item.html,
				rtf=item.rtf,
				canUpload=item.canUpload,
			),
			replaceText=replaceText,
		)
		return HistoryWriteResult(
			fallbackId,
			imageWasDropped=True,
			imageWasTooLarge=imageWasTooLarge or len(item.imageData or b"") > MAX_IMAGE_BYTES,
		)

	def _queueWriteCompletion(
		self,
		future: Future[HistoryWriteResult],
		*,
		expectedText: str | None = None,
	) -> None:
		"""Queue one history write result for reporting on the main thread."""
		_ = queueMainThread(
			self._finishWrite,
			future,
			expectedText,
			onFailure=self._isControllerStarted,
			failureMessage="Could not schedule a clipboard history write result.",
		)

	def _queueImageWriteCompletion(self, future: Future[HistoryWriteResult]) -> None:
		"""Release one image slot before reporting its history result."""
		self._imageSlots.release()
		self._queueWriteCompletion(future)

	def _finishWrite(
		self,
		future: Future[HistoryWriteResult],
		expectedText: str | None = None,
	) -> None:
		"""Report history write results and refresh visible metadata on the main thread."""
		if not self._isControllerStarted():
			return
		try:
			result = future.result()
		except Exception:
			if not self._saveFailureReported:
				self._saveFailureReported = True
				log.exception("Failed to save clipboard history.")
				# Translators: Error shown when clipboard content changed but history could not be saved.
				ui.message(_("Clipboard updated, but history could not be saved"))
			return
		self._saveFailureReported = False
		if result.itemId is None:
			if result.imageWasTooLarge:
				# Translators: Message shown when a clipboard image exceeds the single-image history limit.
				ui.message(
					_("The image was not added because it exceeds the {limit} MB history limit").format(
						limit=MAX_IMAGE_BYTES // (1024 * 1024),
					),
				)
			elif result.imageWasDropped:
				# Translators: Message shown when a clipboard image could not be normalized for history.
				ui.message(_("The image could not be added to clipboard history"))
			else:
				# Translators: Message shown when an image exceeds the total history storage limit.
				ui.message(_("The image was not added because image history storage is full"))
			return
		self._onItemStored(result, expectedText)
