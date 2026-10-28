# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Temporarily paste content through the system clipboard and restore the original afterwards."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from enum import Enum, auto
from time import monotonic
from typing import TYPE_CHECKING, Protocol

import addonHandler
from core import callLater
from keyboardHandler import KeyboardInputGesture
from logHandler import log
import ui
import winUser

from .background import LatestWorkerResult
from .clipboard.payload import normalizeNewlinesForWin32, normalizeUnicodeText
from .clipboard.monitor import ClipboardContentType, ClipboardSequenceChangedError, ClipboardSnapshot
from .clipboard.imageCodec import pngToPackedDib
from .storage.models import StorageError
from .summary.formatting import getTextOrCharacterCount

if TYPE_CHECKING:
	from .clipboard.monitor import ClipboardMonitor
	from .storage.database import ClipboardStorage
	from .storage.models import ClipboardItem


addonHandler.initTranslation()

PASTE_KEY_RELEASE_POLL_INTERVAL_MS = 25
PASTE_KEY_RELEASE_TIMEOUT_SECONDS = 3.0
_TEMPORARY_PASTE_RETRY_DELAY_MS = 50
_TEMPORARY_PASTE_MAX_RETRIES = 2
_TEMPORARY_CLIPBOARD_RESTORE_DELAY = 300


def waitForTriggerKeysReleased(
	triggerKeyCodes: frozenset[int],
	deadline: float,
	retry: Callable[[], None],
) -> bool | None:
	"""Return key-release status, scheduling another check while keys remain held."""
	if not any(winUser.getAsyncKeyState(vkCode) & 0x8000 for vkCode in triggerKeyCodes):
		return True
	if monotonic() >= deadline:
		return False
	callLater(PASTE_KEY_RELEASE_POLL_INTERVAL_MS, retry)
	return None


class ClipboardChangeSource(Enum):
	"""Identify how one clipboard snapshot updates current state, history, and cloud sync."""

	# Apply as the current clipboard and record it in history and cloud sync.
	RECORD = auto()
	# Apply as the current clipboard without recording it.
	NO_RECORD = auto()
	# Record appended text, replacing the history entry of the text it extends.
	APPEND_TEXT = auto()
	# Record manager editor text, ignoring its history result if the text became stale.
	MANAGER_TEXT_REPLACEMENT = auto()
	# Only register the write as the add-on's own; current clipboard state stays unchanged.
	# Used for temporary paste content and for restoring the exact original afterwards.
	REGISTER_ONLY = auto()


@dataclass(frozen=True, slots=True)
class _TemporaryPasteRequest:
	"""Carry one immutable temporary paste through deferred preparation and retries."""

	snapshot: ClipboardSnapshot
	feedbackText: str
	triggerKeyCodes: frozenset[int]
	keyReleaseDeadline: float
	sourceSequenceNumber: int | None = None
	expectedFocus: object | None = None
	preparedPngDib: bytes | None = None
	# Whether rich content was degraded to plain text, so feedback must say formatting was lost.
	usedPlainTextFallback: bool = False


@dataclass(frozen=True, slots=True)
class _TemporaryPasteState:
	"""Retain the data needed to restore one temporary clipboard paste."""

	originalSnapshot: ClipboardSnapshot | None
	temporarySequenceNumber: int
	temporarySnapshot: ClipboardSnapshot
	allowsPartialWrite: bool = False
	# Whether current clipboard state already reflects originalSnapshot, so restoring it needs no update.
	originalIsCurrent: bool = False


def _isTemporarySnapshot(
	snapshot: ClipboardSnapshot,
	state: _TemporaryPasteState,
) -> bool:
	"""Match temporary content after Windows has synthesized additional clipboard formats."""
	expected = state.temporarySnapshot
	return (
		snapshot.contentType == expected.contentType
		and snapshot.text == normalizeNewlinesForWin32(expected.text)
		and snapshot.html == expected.html
		and snapshot.rtf == expected.rtf
		and snapshot.imageFormat == expected.imageFormat
		and snapshot.imageData == expected.imageData
		and snapshot.imageWidth == expected.imageWidth
		and snapshot.imageHeight == expected.imageHeight
		and snapshot.imageBitDepth == expected.imageBitDepth
		and snapshot.files == expected.files
		and not snapshot.canIncludeInHistory
		and not snapshot.canUpload
		and bool(snapshot.sequenceNumber)
	)


def preparePngDib(snapshot: ClipboardSnapshot) -> bytes:
	"""Decode a validated PNG snapshot into its DIB clipboard fallback."""
	imageData = snapshot.imageData
	if snapshot.imageFormat != "PNG" or imageData is None:
		raise ValueError(snapshot.imageFormat)
	dibData = pngToPackedDib(
		imageData,
		(snapshot.imageWidth, snapshot.imageHeight, snapshot.imageBitDepth),
	)
	if dibData is None:
		raise ValueError(snapshot.imageFormat)
	return dibData


def _prepareTemporaryPngPaste(request: _TemporaryPasteRequest) -> _TemporaryPasteRequest:
	"""Attach a worker-prepared DIB fallback to one temporary PNG paste."""
	try:
		return replace(request, preparedPngDib=preparePngDib(request.snapshot))
	except (OSError, ValueError):
		fallback = _getPlainTextPasteRequest(request)
		if fallback is None:
			raise
		log.debugWarning("Could not prepare the image; retaining temporary paste text.", exc_info=True)
		return fallback


def _getPlainTextPasteRequest(request: _TemporaryPasteRequest) -> _TemporaryPasteRequest | None:
	"""Retain actual text when a rich temporary paste cannot be prepared or written."""
	if not request.snapshot.text or request.snapshot.contentType not in (
		ClipboardContentType.FORMATTED_TEXT,
		ClipboardContentType.TEXT_AND_IMAGE,
	):
		return None
	return replace(
		request,
		snapshot=ClipboardSnapshot(
			ClipboardContentType.TEXT,
			text=normalizeUnicodeText(request.snapshot.text),
			canIncludeInHistory=False,
			canUpload=False,
		),
		preparedPngDib=None,
		usedPlainTextFallback=True,
	)


class ClipboardWriteFailedError(RuntimeError):
	"""Report a failed clipboard write together with the sequence number it left behind."""

	def __init__(self, sequenceNumber: int) -> None:
		"""Remember the sequence number of the partially written clipboard."""
		# Translators: Error shown when supported formats cannot be written to the clipboard.
		super().__init__(_("Could not write to the system clipboard"))
		self.sequenceNumber = sequenceNumber


class _SnapshotWriter(Protocol):
	"""Write one snapshot through the controller and apply the semantics of its change source."""

	def __call__(
		self,
		snapshot: ClipboardSnapshot,
		source: ClipboardChangeSource,
		*,
		expectedSequenceNumber: int | None = None,
		preparedPngDib: bytes | None = None,
	) -> int:
		"""Return the clipboard sequence number of the completed write."""
		...


class _ClipboardClearer(Protocol):
	"""Clear the clipboard through the controller and apply the semantics of its change source."""

	def __call__(
		self,
		*,
		source: ClipboardChangeSource = ...,
		expectedSequenceNumber: int | None = None,
	) -> int:
		"""Return the clipboard sequence number of the completed clear."""
		...


class _RegisteredStateDistruster(Protocol):
	"""Stop trusting controller state registered for a sequence number that could not be reflected."""

	def __call__(self, sequenceNumber: int, *, reread: bool) -> None:
		"""Make sequence checks re-read the clipboard, optionally re-reading it now."""
		...


class _SerialWorker(Protocol):
	"""Run jobs one at a time in submission order on the controller's background worker."""

	def submit[*Ts, T](self, fn: Callable[[*Ts], T], /, *args: *Ts) -> Future[T]:
		"""Run ``fn(*args)`` after every previously queued job."""
		...


class TemporaryPaster:
	"""Own one temporary clipboard paste at a time on behalf of the clipboard controller.

	The controller keeps current clipboard state; this class reaches it only through the
	callables passed to its constructor.
	"""

	def __init__(
		self,
		*,
		monitor: ClipboardMonitor,
		storage: ClipboardStorage,
		worker: _SerialWorker,
		isStarted: Callable[[], bool],
		getLastAppliedSequenceNumber: Callable[[], int],
		clearPendingWrites: Callable[[], None],
		captureOriginalClipboard: Callable[[], tuple[ClipboardSnapshot | None, int, bool]],
		writeSnapshot: _SnapshotWriter,
		clearClipboard: _ClipboardClearer,
		applyLeftoverClipboardContent: Callable[[int, ClipboardSnapshot | None], None],
		distrustRegisteredClipboardState: _RegisteredStateDistruster,
		getClipboardOwnerHandle: Callable[[], int],
		isSameFocus: Callable[[object], bool],
		snapshotFromItem: Callable[[ClipboardItem], ClipboardSnapshot],
	) -> None:
		"""Connect the paster to the monitor, storage and controller operations it relies on."""
		super().__init__()
		self._monitor = monitor
		self._storage = storage
		self._worker = worker
		self._isControllerStarted = isStarted
		self._getLastAppliedSequenceNumber = getLastAppliedSequenceNumber
		self._clearPendingWrites = clearPendingWrites
		self._captureOriginalClipboardForTemporaryPaste = captureOriginalClipboard
		self._writeSnapshot = writeSnapshot
		self._clearClipboard = clearClipboard
		self._applyLeftoverClipboardContent = applyLeftoverClipboardContent
		self._distrustRegisteredClipboardState = distrustRegisteredClipboardState
		self._getClipboardOwnerHandle = getClipboardOwnerHandle
		self._isSameFocus = isSameFocus
		self._snapshotFromItem = snapshotFromItem
		self._temporaryPasteInProgress = False
		self._temporaryPasteState: _TemporaryPasteState | None = None
		self._temporaryPngPreparation: LatestWorkerResult[_TemporaryPasteRequest] = LatestWorkerResult(
			self._finishTemporaryPngPastePreparation,
			isOwnerActive=isStarted,
			scheduleFailureMessage="Could not schedule a prepared temporary PNG paste.",
			onScheduleFailure=self._endTemporaryPaste,
			queueStaleResults=False,
		)

	@property
	def hasPendingRestore(self) -> bool:
		"""Whether the clipboard holds temporary content whose original is not yet restored."""
		return self._temporaryPasteState is not None

	def matchesTemporaryContent(self, snapshot: ClipboardSnapshot) -> bool:
		"""Return whether a monitor snapshot is the temporary content awaiting restoration."""
		state = self._temporaryPasteState
		return state is not None and _isTemporarySnapshot(snapshot, state)

	def cancel(self) -> None:
		"""Cancel pending preparation and forget any temporary paste without restoring it."""
		self._temporaryPngPreparation.cancel()
		self._temporaryPasteInProgress = False
		self._temporaryPasteState = None

	def pasteTextTemporarily(
		self,
		text: str,
		triggerKeyCodes: frozenset[int],
		*,
		sourceSequenceNumber: int | None = None,
	) -> None:
		"""Schedule a temporary text paste, optionally tied to the current clipboard."""
		text = normalizeUnicodeText(text)
		self.pasteSnapshotTemporarily(
			ClipboardSnapshot(ClipboardContentType.TEXT, text=text),
			text,
			triggerKeyCodes,
			sourceSequenceNumber=sourceSequenceNumber,
		)

	def pasteSnapshotTemporarily(
		self,
		snapshot: ClipboardSnapshot,
		feedbackText: str,
		triggerKeyCodes: frozenset[int],
		*,
		sourceSequenceNumber: int | None = None,
		expectedFocus: object | None = None,
	) -> None:
		"""Schedule one temporary clipboard paste, preparing PNG data off-thread."""
		if self._temporaryPasteInProgress:
			# Translators: Message shown when another temporary clipboard paste is still running.
			raise RuntimeError(_("Pasting is in progress. Please wait."))
		request = _TemporaryPasteRequest(
			snapshot=replace(snapshot, canIncludeInHistory=False, canUpload=False),
			feedbackText=getTextOrCharacterCount(feedbackText),
			triggerKeyCodes=triggerKeyCodes,
			keyReleaseDeadline=monotonic() + PASTE_KEY_RELEASE_TIMEOUT_SECONDS,
			sourceSequenceNumber=sourceSequenceNumber,
			expectedFocus=expectedFocus,
		)
		self._temporaryPasteInProgress = True
		if request.snapshot.imageFormat != "PNG":
			self._beginTemporaryPaste(request, 0)
			return
		if request.expectedFocus is None:
			self._temporaryPasteInProgress = False
			# Translators: Temporary clipboard paste cancellation because keyboard focus could not be identified.
			ui.message(_("Keyboard focus could not be identified. Paste cancelled."))
			return
		try:
			future = self._worker.submit(_prepareTemporaryPngPaste, request)
		except Exception:
			fallback = _getPlainTextPasteRequest(request)
			if fallback is not None:
				log.debugWarning(
					"Could not queue image preparation; retaining temporary paste text.",
					exc_info=True,
				)
				self._beginTemporaryPaste(fallback, 0)
				return
			self._temporaryPasteInProgress = False
			log.exception("Failed to queue temporary PNG paste preparation.")
			# Translators: Error shown when clipboard content cannot be prepared for pasting.
			ui.message(_("Could not paste the content"))
			return
		self._temporaryPngPreparation.track(future)

	def _endTemporaryPaste(self) -> None:
		"""Allow another temporary paste after the pending one was abandoned."""
		self._temporaryPasteInProgress = False

	def _finishTemporaryPngPastePreparation(self, future: Future[_TemporaryPasteRequest]) -> None:
		"""Start the latest temporary PNG paste after worker preparation."""
		if not self._isControllerStarted():
			self._temporaryPasteInProgress = False
			return
		try:
			request = future.result()
		except ValueError as error:
			self._temporaryPasteInProgress = False
			log.debugWarning("Stored PNG data could not be prepared for temporary paste.", exc_info=error)
			# Translators: Error shown when clipboard content cannot be prepared for pasting.
			ui.message(_("Could not paste the content"))
			return
		except Exception as error:
			self._temporaryPasteInProgress = False
			log.exception("Failed to prepare stored PNG data for temporary paste.", exc_info=error)
			# Translators: Error shown when clipboard content cannot be prepared for pasting.
			ui.message(_("Could not paste the content"))
			return
		self._beginTemporaryPaste(request, 0)

	def _beginTemporaryPaste(  # noqa: C901 - ordered recovery branches keep clipboard ownership explicit.
		self,
		request: _TemporaryPasteRequest,
		retryCount: int,
	) -> None:
		"""Write and paste temporary clipboard content, retrying brief clipboard races."""
		if not self._isControllerStarted():
			self._temporaryPasteInProgress = False
			return
		if request.expectedFocus is not None and not self._isSameFocus(request.expectedFocus):
			self._temporaryPasteInProgress = False
			# Translators: Temporary clipboard paste cancellation because focus changed before pasting.
			ui.message(_("Focus changed. Paste cancelled."))
			return
		originalSnapshot: ClipboardSnapshot | None = None
		originalIsCurrent = False
		try:
			keyReleaseState = waitForTriggerKeysReleased(
				request.triggerKeyCodes,
				request.keyReleaseDeadline,
				lambda: self._beginTemporaryPaste(request, retryCount),
			)
		except Exception:
			self._temporaryPasteInProgress = False
			log.exception("Failed while waiting for the temporary paste keys to be released.")
			# Translators: Error shown when clipboard content cannot be prepared for pasting.
			ui.message(_("Could not paste the content"))
			return
		if keyReleaseState is None:
			return
		if not keyReleaseState:
			self._temporaryPasteInProgress = False
			# Translators: Message shown when a paste shortcut remains held until timeout.
			ui.message(_("The keyboard shortcut was not released, so the paste was cancelled"))
			return
		try:
			originalSnapshot, expectedSequenceNumber, originalWasApplied = (
				self._captureOriginalClipboardForTemporaryPaste()
			)
			if (
				request.sourceSequenceNumber is not None
				and expectedSequenceNumber != request.sourceSequenceNumber
			):
				self._temporaryPasteInProgress = False
				# Translators: Message shown when a clipboard navigation selection becomes stale before pasting.
				ui.message(_("The clipboard changed. Paste cancelled."))
				return
			originalIsCurrent = (
				originalWasApplied
				and originalSnapshot is not None
				and originalSnapshot.sequenceNumber
				== expectedSequenceNumber
				== self._getLastAppliedSequenceNumber()
				== self._monitor.getSequenceNumber()
			)
		except Exception:
			self._temporaryPasteInProgress = False
			log.exception("Failed to capture the original clipboard before a temporary paste.")
			# Translators: Error shown when clipboard content cannot be prepared for pasting.
			ui.message(_("Could not paste the content"))
			return
		try:
			partialWriteError: ClipboardWriteFailedError | None = None
			# At most two writes: the plain-text fallback has no further fallback of its own.
			while True:
				try:
					temporarySequenceNumber = self._writeSnapshot(
						request.snapshot,
						ClipboardChangeSource.REGISTER_ONLY,
						expectedSequenceNumber=expectedSequenceNumber,
						preparedPngDib=request.preparedPngDib,
					)
					break
				except ClipboardSequenceChangedError as error:
					raise partialWriteError or error
				except RuntimeError as error:
					if isinstance(error, ClipboardWriteFailedError):
						partialWriteError = error
					fallback = _getPlainTextPasteRequest(request)
					if (
						fallback is None
						or not isinstance(error, ClipboardWriteFailedError)
						and not isinstance(error.__cause__, (OSError, ValueError))
						or request.expectedFocus is not None
						and not self._isSameFocus(request.expectedFocus)
						# Never recapture a partial write as the original clipboard for the fallback.
						or partialWriteError is not None
						and (
							not partialWriteError.sequenceNumber
							or self._monitor.getSequenceNumber() != partialWriteError.sequenceNumber
							or self._monitor.getOwnerHandle() != self._getClipboardOwnerHandle()
						)
					):
						raise partialWriteError or error
					if partialWriteError is not None:
						expectedSequenceNumber = partialWriteError.sequenceNumber
					log.debugWarning(
						"Could not write rich formats; retrying temporary paste as text.",
						exc_info=True,
					)
					request = fallback
		except ClipboardSequenceChangedError:
			if retryCount < _TEMPORARY_PASTE_MAX_RETRIES:
				callLater(
					_TEMPORARY_PASTE_RETRY_DELAY_MS,
					self._beginTemporaryPaste,
					request,
					retryCount + 1,
				)
				return
			self._temporaryPasteInProgress = False
			# Translators: Error shown when the clipboard keeps changing before a temporary paste.
			ui.message(_("The clipboard kept changing. Paste cancelled."))
			return
		except ClipboardWriteFailedError as error:
			state = _TemporaryPasteState(
				originalSnapshot,
				error.sequenceNumber,
				request.snapshot,
				allowsPartialWrite=True,
				originalIsCurrent=originalIsCurrent,
			)
			self._temporaryPasteState = state
			self._restoreClipboardAfterTemporaryPaste(state, reportFailure=False)
			log.debugWarning("Failed to write temporary clipboard content.", exc_info=True)
			# Translators: Error shown when clipboard content cannot be written for pasting.
			ui.message(_("Could not paste the content"))
			return
		except Exception:
			self._temporaryPasteInProgress = False
			log.exception("Failed to write temporary clipboard content.")
			# Translators: Error shown when clipboard content cannot be written for pasting.
			ui.message(_("Could not paste the content"))
			return
		state = _TemporaryPasteState(
			originalSnapshot,
			temporarySequenceNumber,
			request.snapshot,
			originalIsCurrent=originalIsCurrent,
		)
		self._temporaryPasteState = state
		verifiedSequenceNumber = self._getVerifiedTemporarySequenceNumber(state)
		if verifiedSequenceNumber is None:
			self._clearPendingWrites()
			self._restoreClipboardAfterTemporaryPaste(state, reportFailure=False)
			self._monitor.handleClipboardUpdate()
			# Translators: Error shown when the clipboard keeps changing before a temporary paste.
			ui.message(_("The clipboard kept changing. Paste cancelled."))
			return
		if verifiedSequenceNumber != temporarySequenceNumber:
			state = replace(state, temporarySequenceNumber=verifiedSequenceNumber)
			self._temporaryPasteState = state
		if request.expectedFocus is not None and not self._isSameFocus(request.expectedFocus):
			self._restoreClipboardAfterTemporaryPaste(state, reportFailure=False)
			# Translators: Temporary clipboard paste cancellation because focus changed before pasting.
			ui.message(_("Focus changed. Paste cancelled."))
			return
		try:
			KeyboardInputGesture.fromName("control+v").send()
		except Exception:
			log.exception("Failed to send the temporary clipboard paste gesture.")
			self._restoreClipboardAfterTemporaryPaste(state, reportFailure=False)
			# Translators: Error shown when the temporary clipboard paste gesture cannot be sent.
			ui.message(_("Could not paste the content"))
			return
		try:
			callLater(
				_TEMPORARY_CLIPBOARD_RESTORE_DELAY,
				self._restoreClipboardAfterTemporaryPaste,
				state,
			)
		except Exception:
			log.debugWarning("Could not schedule temporary clipboard restoration.", exc_info=True)
			self._temporaryPasteState = None
			self._temporaryPasteInProgress = False
			# The verified temporary content stays on the clipboard, so it becomes the current content.
			self._applyLeftoverClipboardContent(state.temporarySequenceNumber, state.temporarySnapshot)
			# Translators: Error shown after content was pasted but clipboard restoration could not be scheduled.
			ui.message(_("The content was pasted, but the original clipboard content could not be restored"))
			return
		feedbackText = request.feedbackText
		if request.usedPlainTextFallback:
			# Translators: Spoken when formatted clipboard content had to be pasted as plain text.
			feedbackText = _("Pasted as plain text; formatting could not be kept")
		try:
			ui.delayedMessage(feedbackText)
		except Exception:
			log.debugWarning("Could not delay temporary paste feedback.", exc_info=True)
			ui.message(feedbackText)

	def _getVerifiedTemporarySequenceNumber(
		self,
		state: _TemporaryPasteState,
	) -> int | None:
		"""Return a stable sequence only while the temporary clipboard content remains current."""
		try:
			expectedOwnerHandle = self._getClipboardOwnerHandle()
		except RuntimeError:
			return None
		if self._monitor.getOwnerHandle() != expectedOwnerHandle:
			return None
		# Re-read only if Windows may have synthesized formats after our write.
		if (
			state.temporarySequenceNumber
			and self._monitor.getSequenceNumber() == state.temporarySequenceNumber
		):
			return state.temporarySequenceNumber
		temporarySnapshot = self._monitor.readNow()
		if (
			not _isTemporarySnapshot(temporarySnapshot, state)
			or self._monitor.getOwnerHandle() != expectedOwnerHandle
			or self._monitor.getSequenceNumber() != temporarySnapshot.sequenceNumber
		):
			return None
		return temporarySnapshot.sequenceNumber

	def _restoreClipboardAfterTemporaryPaste(
		self,
		state: _TemporaryPasteState,
		*,
		reportFailure: bool = True,
	) -> None:
		"""Restore the original clipboard, then fall back to newest history."""
		if self._temporaryPasteState is not state:
			return
		ownedSequenceNumber = state.temporarySequenceNumber
		# Known content at ownedSequenceNumber, and whether it was only registered rather than applied.
		ownedSnapshot: ClipboardSnapshot | None = None
		ownedContentNeedsApply = True
		try:
			verifiedSequenceNumber = self._getVerifiedTemporarySequenceNumber(state)
			if verifiedSequenceNumber is not None:
				ownedSequenceNumber = verifiedSequenceNumber
				# A partial temporary write left unknown content under our sequence number, so it is re-read.
				ownedSnapshot = None if state.allowsPartialWrite else state.temporarySnapshot
			elif (
				not state.allowsPartialWrite
				or not ownedSequenceNumber
				or self._monitor.getSequenceNumber() != ownedSequenceNumber
			):
				# Unverified content under our registered sequence number is re-read instead of trusted.
				self._distrustRegisteredClipboardState(ownedSequenceNumber, reread=True)
				return
			try:
				if self._restoreOriginalClipboardAfterTemporaryPaste(state, ownedSequenceNumber):
					return
			except ClipboardWriteFailedError as error:
				ownedSequenceNumber = error.sequenceNumber
				ownedSnapshot = None
				# A failed exact restore is registered only, while a failed applied restore was applied.
				ownedContentNeedsApply = state.originalIsCurrent
			if self._monitor.getSequenceNumber() != ownedSequenceNumber:
				return
			try:
				latestItem = self._storage.getLatestHistoryItem()
			except StorageError:
				latestItem = None
				log.debugWarning("Could not load the newest clipboard history item.", exc_info=True)
			if latestItem is not None:
				try:
					self._writeSnapshot(
						self._snapshotFromItem(latestItem),
						ClipboardChangeSource.NO_RECORD,
						expectedSequenceNumber=ownedSequenceNumber,
					)
					return
				except ClipboardSequenceChangedError:
					return
				except ClipboardWriteFailedError as error:
					ownedSequenceNumber = error.sequenceNumber
					ownedSnapshot = None
					ownedContentNeedsApply = False
					log.debugWarning("Could not restore the newest clipboard history item.", exc_info=True)
				except Exception:
					log.debugWarning("Could not restore the newest clipboard history item.", exc_info=True)
			if ownedContentNeedsApply:
				# Content written only as our own remains on the clipboard, so it becomes the current content.
				self._applyLeftoverClipboardContent(ownedSequenceNumber, ownedSnapshot)
			if reportFailure and self._monitor.getSequenceNumber() == ownedSequenceNumber:
				# Translators: Error shown after content was pasted but no clipboard content could be restored.
				ui.message(
					_("The content was pasted, but the original clipboard content could not be restored"),
				)
		finally:
			if self._temporaryPasteState is state:
				self._temporaryPasteState = None
				self._temporaryPasteInProgress = False

	def _restoreOriginalClipboardAfterTemporaryPaste(
		self,
		state: _TemporaryPasteState,
		expectedSequenceNumber: int,
	) -> bool:
		"""Try to restore every retained format captured before a temporary paste."""
		snapshot = state.originalSnapshot
		if snapshot is None:
			return False
		# Current state still shows the original, so an exact restore only needs registering.
		source = (
			ClipboardChangeSource.REGISTER_ONLY
			if state.originalIsCurrent
			else ClipboardChangeSource.NO_RECORD
		)
		try:
			if snapshot.contentType == ClipboardContentType.EMPTY:
				_restoredSequenceNumber = self._clearClipboard(
					source=source,
					expectedSequenceNumber=expectedSequenceNumber,
				)
			else:
				_restoredSequenceNumber = self._writeSnapshot(
					snapshot,
					source,
					expectedSequenceNumber=expectedSequenceNumber,
				)
			return True
		except ClipboardSequenceChangedError:
			return False
		except ClipboardWriteFailedError:
			raise
		except Exception:
			log.debugWarning("Could not restore the original clipboard snapshot.", exc_info=True)
			return False
