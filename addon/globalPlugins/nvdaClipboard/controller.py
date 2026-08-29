# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Coordinate clipboard monitoring, navigation, history, categories, and cloud actions."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path
from threading import BoundedSemaphore, Event
from time import monotonic
from typing import TYPE_CHECKING, Never

import addonHandler
import api
import braille
import colors
import controlTypes
from core import callLater
import eventHandler
import gui
from keyboardHandler import KeyboardInputGesture
from logHandler import log
import speech
import textInfos
import ui
import winUser
import wx

from .clipboardMonitor import (
	ClipboardContentType,
	ClipboardMonitor,
	ClipboardSequenceChangedError,
	ClipboardSnapshot,
	ClipboardWriteError,
)
from .clipboardData import (
	MAX_IMAGE_BYTES,
	getPngImageInfo,
	isImageSizeSafe,
)
from .imageCodec import (
	ImageDataTooLargeError,
	isPngImageDecodable,
	normalizeImageDataToPng,
	pngToPackedDib,
)
from .fileSize import FileSizeCalculation
from . import lastSpoken
from .cloudSync import CloudClipboardWriteError, CloudSyncManager
from .navigation import ClipboardNavigator, NavigationResult
from .oneDriveSync import OneDriveSyncManager
from .search import matchesSearchKeywords, normalizeSearchText
from .cues import playBoundary, playCopy, playLineBoundary, playNonPlainText
from .images import (
	captureNavigatorObjectPng,
	isScreenCurtainEnabled,
	savePngImage,
)
from .tiantanSupport import getTiantanSupportState
from .storage import (
	CategoryExistsError,
	CategoryNameError,
	CategoryNotEmptyError,
	CategoryNotFoundError,
	ClipboardStorage,
	ImageStorageLimitError,
	InvalidItemError,
	ItemNotFoundError,
	ReservedCategoryNameError,
	StorageError,
	StorageFormatError,
)
from .storageModels import ClipboardItem, ClipboardItemContent, ClipboardItemSummary, ClipboardItemType
from .textStats import TextStatistics, TextStatisticsCalculation

if TYPE_CHECKING:
	from .cloudDialog import CloudClipboardDialog
	from .manager import ClipboardManagerFrame
	from .oneDriveDialog import OneDriveSyncDialog


addonHandler.initTranslation()

_PASTE_KEY_RELEASE_POLL_INTERVAL_MS = 25
_PASTE_KEY_RELEASE_TIMEOUT_SECONDS = 3.0
_LAST_SPOKEN_CLIPBOARD_RESTORE_DELAY = 300
_SUMMARY_REPORT_TIMEOUT_MS = 2000

# Translators: Error shown when an add-on window cannot open because NVDA's main window is unavailable.
_MAIN_FRAME_UNAVAILABLE = _("NVDA's main window is unavailable")

# Translators: Name of the built-in category containing recent clipboard text.
_HISTORY_CATEGORY_NAME = _("Clipboard history")

# Translators: Message shown when Tiantan Cloud Clipboard is disabled.
_TIANTAN_DISABLED_MESSAGE = _(
	"Tiantan Cloud Clipboard is disabled. Enable it in settings, then restart NVDA.",
)

_CANONICAL_SOLID_COLOR_NAMES = {
	# Translators: Exact name of the RGB color (255, 0, 0).
	(255, 0, 0): _("red"),
	# Translators: Exact name of the RGB color (0, 255, 0).
	(0, 255, 0): _("green"),
	# Translators: Exact name of the RGB color (0, 0, 255).
	(0, 0, 255): _("blue"),
	# Translators: Exact name of the RGB color (255, 255, 0).
	(255, 255, 0): _("yellow"),
	# Translators: Exact name of the RGB color (0, 255, 255).
	(0, 255, 255): _("cyan"),
	# Translators: Exact name of the RGB color (255, 0, 255).
	(255, 0, 255): _("magenta"),
}


class _BuiltinCategory(Enum):
	"""Identify built-in categories without using translated display text as data."""

	HISTORY = auto()


type CategoryId = str | _BuiltinCategory

_HISTORY_CATEGORY_ID = _BuiltinCategory.HISTORY


def _waitForTriggerKeysReleased(
	triggerKeyCodes: frozenset[int],
	deadline: float,
	retry: Callable[[], None],
) -> bool | None:
	"""Return key-release status, scheduling another check while keys remain held."""
	if not any(winUser.getAsyncKeyState(vkCode) & 0x8000 for vkCode in triggerKeyCodes):
		return True
	if monotonic() >= deadline:
		return False
	callLater(_PASTE_KEY_RELEASE_POLL_INTERVAL_MS, retry)
	return None


def _normalizeUnicodeText(text: str) -> str:
	"""Replace isolated UTF-16 surrogate code units at the clipboard boundary."""
	return text.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")


class _ClipboardChangeSource(Enum):
	"""Identify why one clipboard snapshot is being applied."""

	EXTERNAL = auto()
	INITIAL = auto()
	APPEND_TEXT = auto()
	LAST_SPOKEN_PASTE = auto()
	LAST_SPOKEN_RESTORE = auto()
	LOCAL_WRITE_FAILURE = auto()
	MANAGER_TEXT_REPLACEMENT = auto()
	HISTORY_RESTORE = auto()
	CATEGORY_RESTORE = auto()
	CLOUD_FETCH = auto()
	NAVIGATOR_SCREENSHOT = auto()
	CLEAR = auto()


@dataclass(frozen=True, slots=True)
class ClipboardItemDetails:
	"""Describe one selected item for the clipboard manager."""

	key: int
	kind: ClipboardItemType
	content: str
	isEditable: bool
	hasImage: bool
	canUpload: bool


@dataclass(frozen=True, slots=True)
class MissingFileCleanupResult:
	"""Describe selected file-group cleanup performed by the manager."""

	changedCount: int
	deletedCount: int
	removedCount: int
	replacementIds: dict[int, int]


@dataclass(frozen=True, slots=True)
class _HistoryWriteResult:
	itemId: int | None
	imageWasDropped: bool = False
	imageWasTooLarge: bool = False


@dataclass(frozen=True, slots=True)
class _PreparedPngRestore:
	"""Retain one worker-prepared PNG restore until its main-thread commit."""

	snapshot: ClipboardSnapshot
	source: _ClipboardChangeSource
	expectedSequenceNumber: int
	confirmation: str
	dibData: bytes


@dataclass(frozen=True, slots=True)
class _LastSpokenPasteState:
	"""Retain the data needed to restore one temporary last-spoken paste."""

	originalSnapshot: ClipboardSnapshot | None
	temporarySequenceNumber: int
	temporaryText: str
	allowsPartialWrite: bool = False
	originalNavigationOffset: int | None = None


def _isLastSpokenTemporarySnapshot(
	snapshot: ClipboardSnapshot,
	state: _LastSpokenPasteState,
) -> bool:
	"""Match temporary text after Windows has synthesized additional clipboard formats."""
	return (
		snapshot.contentType == ClipboardContentType.TEXT
		and snapshot.text == state.temporaryText
		and not snapshot.canIncludeInHistory
		and not snapshot.canUpload
		and bool(snapshot.sequenceNumber)
	)


def _isMissingFilePath(filePath: str) -> bool:
	"""Return whether a stored file path cannot currently be found."""
	try:
		return not Path(filePath).exists()
	except OSError:
		return True


class _ClipboardWriteFailedError(RuntimeError):
	def __init__(self, sequenceNumber: int) -> None:
		# Translators: Error shown when supported clipboard formats cannot be restored.
		super().__init__(_("Could not write to the system clipboard"))
		self.sequenceNumber = sequenceNumber


class _LastSpokenTemporaryApplyError(RuntimeError):
	"""Report a completed temporary write whose local controller update failed."""

	def __init__(self, sequenceNumber: int) -> None:
		super().__init__(sequenceNumber)
		self.sequenceNumber = sequenceNumber


class ClipboardController:
	"""Own all non-script behavior for the NVDA Clipboard global plugin."""

	def __init__(self, *, tiantanEnabled: bool = False) -> None:
		self.storage = ClipboardStorage(reservedCategoryNames=(_HISTORY_CATEGORY_NAME,))
		self._tiantanUnavailableMessage = ""
		try:
			self.navigator = ClipboardNavigator()
			self.cloudSync = None
			if tiantanEnabled:
				tiantanSupportState = getTiantanSupportState()
				self._tiantanUnavailableMessage = tiantanSupportState.statusMessage
				if tiantanSupportState.isAvailable:
					self.cloudSync = CloudSyncManager(onStateChanged=self._onCloudStateChanged)
					cloudState = self.cloudSync.getState()
					if not cloudState.isAvailable:
						self._tiantanUnavailableMessage = cloudState.statusMessage
						self.cloudSync.terminate()
						self.cloudSync = None
			self.oneDriveSync = OneDriveSyncManager(
				self.storage,
				onStateChanged=self._onOneDriveStateChanged,
				onDataChanged=self._onOneDriveDataChanged,
			)
			self.monitor = ClipboardMonitor(onSnapshot=self._onClipboardSnapshot)
			self._historyExecutor = ThreadPoolExecutor(
				max_workers=1,
				thread_name_prefix="nvdaClipboard.historyWrite",
			)
			self._imageHistorySlots = BoundedSemaphore(2)
		except Exception:
			oneDriveSync = getattr(self, "oneDriveSync", None)
			if oneDriveSync is not None:
				oneDriveSync.terminate()
			cloudSync = getattr(self, "cloudSync", None)
			if cloudSync is not None:
				cloudSync.terminate()
			self.storage.close()
			raise
		self.manager: ClipboardManagerFrame | None = None
		self.cloudDialog: CloudClipboardDialog | None = None
		self.oneDriveDialog: OneDriveSyncDialog | None = None
		self._contentType = ClipboardContentType.EMPTY
		self._text = ""
		self._canUpload = False
		self._fileSizeCalculation: FileSizeCalculation | None = None
		self._pendingFileSizeReport: FileSizeCalculation | None = None
		self._textStatisticsCalculation: TextStatisticsCalculation | None = None
		self._pendingTextStatisticsReport: TextStatisticsCalculation | None = None
		# Translators: Summary used when the system clipboard is empty.
		self._summary = _("Clipboard is empty")
		self.navigator.setText(self._summary)
		self._historyIndex = 0
		self._pendingWrites: set[int] = set()
		self._pendingPngRestore: Future[_PreparedPngRestore] | None = None
		self._awaitingInitialSnapshot = False
		self._lastAppliedSequenceNumber = 0
		self._historySaveFailureReported = False
		self._cloudFetchInProgress = False
		self._lastSpokenPasteInProgress = False
		self._lastSpokenPasteState: _LastSpokenPasteState | None = None
		self._isStarted = False

	@property
	def isCloudAvailable(self) -> bool:
		"""Return whether the native cloud clipboard SDK is available."""
		cloudSync = self.cloudSync
		return bool(cloudSync is not None and cloudSync.getState().isAvailable)

	def start(self) -> None:
		"""Start cloud state detection and clipboard monitoring."""
		if self._isStarted:
			return
		self._isStarted = True
		try:
			lastSpoken.initialize()
			cloudSync = self.cloudSync
			if cloudSync is not None:
				cloudSync.initialize()
			self.oneDriveSync.initialize()
			self._awaitingInitialSnapshot = True
			self.monitor.start()
		except Exception:
			try:
				self.terminate()
			except Exception:
				log.exception("Failed to release clipboard services after startup failure.")
			raise
		self.monitor.handleClipboardUpdate()

	def terminate(self) -> None:
		"""Stop services and destroy any add-on windows."""
		self._isStarted = False
		self._awaitingInitialSnapshot = False
		self._pendingWrites.clear()
		self._cancelPendingPngRestore()
		self._cloudFetchInProgress = False
		self._lastSpokenPasteInProgress = False
		self._lastSpokenPasteState = None
		self._pendingFileSizeReport = None
		if self._fileSizeCalculation is not None:
			self._fileSizeCalculation.cancel()
			self._fileSizeCalculation = None
		self._pendingTextStatisticsReport = None
		if self._textStatisticsCalculation is not None:
			self._textStatisticsCalculation.cancel()
			self._textStatisticsCalculation = None
		lastSpoken.terminate()
		try:
			self.monitor.stop()
		except Exception:
			log.debugWarning("Failed to stop clipboard monitoring.", exc_info=True)
		cloudSync = self.cloudSync
		if cloudSync is not None:
			cloudSync.terminate()
		if self.cloudDialog is not None:
			self.cloudDialog.Destroy()
			self.cloudDialog = None
		oneDriveDialog = self.oneDriveDialog
		self.oneDriveDialog = None
		if oneDriveDialog is not None:
			oneDriveDialog.Destroy()
		self.oneDriveSync.terminate()
		manager = self.manager
		self.manager = None
		if manager is not None:
			manager.terminate()
		self._historyExecutor.shutdown(wait=True)
		self.storage.close()

	def showManager(self) -> None:
		"""Create or show the single non-modal clipboard manager window."""
		mainFrame = gui.mainFrame
		if mainFrame is None:
			raise RuntimeError(_MAIN_FRAME_UNAVAILABLE)
		mainFrame.prePopup()
		try:
			if self.manager is None:
				from .manager import ClipboardManagerFrame

				self.manager = ClipboardManagerFrame(mainFrame, self)
			self.manager.showManager()
		finally:
			mainFrame.postPopup()

	def showCloudDialog(self, parent: wx.Window | None = None) -> None:
		"""Create or raise the Tiantan Cloud Clipboard account dialog."""
		cloudSync = self.cloudSync
		if cloudSync is None:
			ui.message(self._tiantanUnavailableMessage or _TIANTAN_DISABLED_MESSAGE)
			return
		if not self.isCloudAvailable:
			ui.message(cloudSync.getState().statusMessage)
			return
		if self.cloudDialog is not None:
			try:
				self.cloudDialog.Raise()
				self.cloudDialog.SetFocus()
				return
			except RuntimeError:
				self.cloudDialog = None
		mainFrame = gui.mainFrame
		dialogParent = parent if parent is not None else mainFrame
		if dialogParent is None:
			raise RuntimeError(_MAIN_FRAME_UNAVAILABLE)
		from .cloudDialog import CloudClipboardDialog

		self.cloudDialog = CloudClipboardDialog(
			dialogParent,
			cloudSync,
			self._clearCloudDialog,
		)
		if mainFrame is not None:
			mainFrame.prePopup()
		try:
			self.cloudDialog.Show()
			self.cloudDialog.Raise()
		finally:
			if mainFrame is not None:
				mainFrame.postPopup()

	def showOneDriveDialog(self, parent: wx.Window | None = None) -> None:
		"""Create or raise the OneDrive history synchronization dialog."""
		if self.oneDriveDialog is not None:
			try:
				self.oneDriveDialog.Raise()
				self.oneDriveDialog.SetFocus()
				return
			except RuntimeError:
				self.oneDriveDialog = None
		mainFrame = gui.mainFrame
		dialogParent = parent if parent is not None else mainFrame
		if dialogParent is None:
			raise RuntimeError(_MAIN_FRAME_UNAVAILABLE)
		from .oneDriveDialog import OneDriveSyncDialog

		self.oneDriveDialog = OneDriveSyncDialog(
			dialogParent,
			self.oneDriveSync,
			self._clearOneDriveDialog,
		)
		if mainFrame is not None:
			mainFrame.prePopup()
		try:
			self.oneDriveDialog.Show()
			self.oneDriveDialog.Raise()
		finally:
			if mainFrame is not None:
				mainFrame.postPopup()

	def pasteCloudText(self, triggerKeyCodes: frozenset[int]) -> None:
		"""Fetch cloud text, leave it on the clipboard, and send the paste gesture."""
		cloudSync = self.cloudSync
		if cloudSync is None:
			ui.message(self._tiantanUnavailableMessage or _TIANTAN_DISABLED_MESSAGE)
			return
		state = cloudSync.getState()
		if not state.isAvailable:
			ui.message(state.statusMessage)
			return
		if self._cloudFetchInProgress:
			# Translators: Message shown when another Tiantan Cloud Clipboard receive is still running.
			ui.message(_("A Tiantan Cloud Clipboard receive is already in progress"))
			return
		focusObject = self._getFocusObject()
		if focusObject is None:
			# Translators: Cloud paste cancellation because keyboard focus could not be identified safely.
			ui.message(
				_("Keyboard focus could not be identified. Tiantan Cloud Clipboard paste was cancelled."),
			)
			return
		self._cloudFetchInProgress = True

		def writeFetchedText(text: str) -> None:
			"""Write downloaded text only while keyboard focus remains unchanged."""
			if not self._isSameFocus(focusObject):
				# Translators: Cloud paste cancellation because focus changed while downloading.
				raise CloudClipboardWriteError(
					_("Focus changed. Tiantan Cloud Clipboard paste was cancelled."),
				)
			sequenceNumber = self._writeClipboardText(text, _ClipboardChangeSource.CLOUD_FETCH)
			callLater(
				_PASTE_KEY_RELEASE_POLL_INTERVAL_MS,
				self._sendCloudPasteGesture,
				focusObject,
				sequenceNumber,
				text,
				triggerKeyCodes,
				monotonic() + _PASTE_KEY_RELEASE_TIMEOUT_SECONDS,
			)

		cloudSync.fetchToClipboard(
			writeFetchedText,
			onDone=self._onCloudFetchDone,
			onDispatchFailed=self._abandonCloudFetch,
		)

	def sendToTiantan(self) -> None:
		"""Send the current allowed clipboard text to Tiantan Cloud Clipboard."""
		cloudSync = self.cloudSync
		if cloudSync is None:
			ui.message(self._tiantanUnavailableMessage or _TIANTAN_DISABLED_MESSAGE)
			return
		cloudSync.uploadCurrentText(self._getCurrentCloudUploadText())

	def receiveFromTiantan(self) -> None:
		"""Receive Tiantan Cloud Clipboard text into the system clipboard without pasting it."""
		cloudSync = self.cloudSync
		if cloudSync is None:
			ui.message(self._tiantanUnavailableMessage or _TIANTAN_DISABLED_MESSAGE)
			return
		state = cloudSync.getState()
		if not state.isAvailable:
			ui.message(state.statusMessage)
			return
		if self._cloudFetchInProgress:
			# Translators: Message shown when another Tiantan Cloud Clipboard receive is still running.
			ui.message(_("A Tiantan Cloud Clipboard receive is already in progress"))
			return
		self._cloudFetchInProgress = True

		def writeFetchedText(text: str) -> None:
			"""Write received Tiantan Cloud Clipboard text without recording or resending it."""
			self._writeClipboardText(text, _ClipboardChangeSource.CLOUD_FETCH)

		cloudSync.fetchToClipboard(
			writeFetchedText,
			onDone=self._onTiantanReceiveDone,
			onDispatchFailed=self._abandonCloudFetch,
		)

	def syncOneDrive(self) -> None:
		"""Start an immediate OneDrive synchronization and announce its result."""
		self.oneDriveSync.syncNow(
			onDone=lambda _success, message: ui.message(message) if message else None,
		)

	def reportClipboardSummary(self) -> None:
		"""Report a concise summary of the current clipboard content."""
		calculation = self._fileSizeCalculation
		if self._contentType == ClipboardContentType.FILES and calculation is not None:
			if calculation.start(onComplete=self._onFileSizeCalculationComplete):
				self._pendingFileSizeReport = calculation
				try:
					callLater(
						_SUMMARY_REPORT_TIMEOUT_MS,
						self._reportPendingFileSizeSummary,
						calculation,
					)
				except Exception:
					self._pendingFileSizeReport = None
					self._reportFileSizeSummary(calculation)
					return
				if speech.getState().speechMode == speech.SpeechMode.onDemand:
					self._reportFileSizeSummary(calculation)
				return
			self._pendingFileSizeReport = None
			self._reportFileSizeSummary(calculation)
			return
		calculation = self._textStatisticsCalculation
		if calculation is not None:
			if calculation.start(onComplete=self._onTextStatisticsCalculationComplete):
				self._pendingTextStatisticsReport = calculation
				try:
					callLater(
						_SUMMARY_REPORT_TIMEOUT_MS,
						self._reportPendingTextStatisticsSummary,
						calculation,
					)
				except Exception:
					self._pendingTextStatisticsReport = None
					self._reportTextStatisticsSummary(calculation)
					return
				if speech.getState().speechMode == speech.SpeechMode.onDemand:
					self._reportTextStatisticsSummary(calculation)
				return
			self._pendingTextStatisticsReport = None
			self._reportTextStatisticsSummary(calculation)
			return
		ui.message(self._summary)

	def _onFileSizeCalculationComplete(self, calculation: FileSizeCalculation) -> None:
		"""Queue the response for a pending summary request on NVDA's main thread."""
		callLater(0, self._reportPendingFileSizeSummary, calculation)

	def _reportPendingFileSizeSummary(self, calculation: FileSizeCalculation) -> None:
		"""Report one requested summary while its calculation is still current."""
		if self._pendingFileSizeReport is not calculation:
			return
		self._pendingFileSizeReport = None
		if not self._isStarted or self._fileSizeCalculation is not calculation:
			return
		self._reportFileSizeSummary(calculation)

	def _reportFileSizeSummary(self, calculation: FileSizeCalculation) -> None:
		"""Report the latest size and item counts for the current clipboard files."""
		progress = calculation.getProgress()
		hasResults = bool(progress.fileCount or progress.directoryCount)
		if hasResults:
			details = [self._formatDataSize(progress.byteCount)]
			if progress.fileCount:
				details.append(
					ngettext(
						# Translators: Number of regular files included in a clipboard size calculation.
						"{count} file",
						"{count} files",
						progress.fileCount,
					).format(count=progress.fileCount),
				)
			if progress.directoryCount:
				details.append(
					ngettext(
						# Translators: Number of folders included in a clipboard size calculation.
						"{count} folder",
						"{count} folders",
						progress.directoryCount,
					).format(count=progress.directoryCount),
				)
			# Translators: Separator between file size, file count, and folder count.
			detailsText = _(", ").join(details)
			if progress.isComplete:
				if progress.isIncomplete:
					# Translators: Clipboard file calculation with partial size and item counts.
					summary = _("{summary}; contents total about {details}").format(
						details=detailsText,
						summary=self._summary,
					)
				else:
					# Translators: Completed clipboard file calculation with size and item counts.
					summary = _("{summary}; contents total {details}").format(
						details=detailsText,
						summary=self._summary,
					)
			else:
				# Translators: Clipboard file calculation with size and item counts accumulated so far.
				summary = _("{summary}; calculating contents, {details}").format(
					details=detailsText,
					summary=self._summary,
				)
		else:
			if progress.isComplete:
				# Translators: Clipboard file calculation where no content details were available.
				summary = _("{summary}; could not calculate contents").format(summary=self._summary)
			else:
				# Translators: Clipboard file calculation before any content details are available.
				summary = _("{summary}; calculating contents").format(summary=self._summary)
		ui.message(summary)

	def _onTextStatisticsCalculationComplete(self, calculation: TextStatisticsCalculation) -> None:
		"""Queue the response for a pending text summary request on NVDA's main thread."""
		callLater(0, self._reportPendingTextStatisticsSummary, calculation)

	def _reportPendingTextStatisticsSummary(self, calculation: TextStatisticsCalculation) -> None:
		"""Report one requested text summary while its calculation is still current."""
		if self._pendingTextStatisticsReport is not calculation:
			return
		self._pendingTextStatisticsReport = None
		if not self._isStarted or self._textStatisticsCalculation is not calculation:
			return
		self._reportTextStatisticsSummary(calculation)

	def _reportTextStatisticsSummary(self, calculation: TextStatisticsCalculation) -> None:
		"""Report exact completed text statistics or the current calculation state."""
		progress = calculation.getProgress()
		lineNumber, columnNumber = self.navigator.getLineAndColumn()
		summary = _(
			# Translators: Clipboard text type followed by the current one-based navigation line and column.
			"{summary}: line {line}, column {column}",
		).format(summary=self._summary, line=lineNumber, column=columnNumber)
		if not progress.isComplete:
			summary = _(
				# Translators: Clipboard text summary with its position while exact statistics are calculated.
				"{summary}; calculating; check again shortly",
			).format(summary=summary)
		elif progress.statistics is not None:
			summary = self._formatTextStatisticsSummary(summary, progress.statistics)
		ui.message(summary)

	def moveToFirstLine(self) -> None:
		"""Move to and report the first clipboard line."""
		if not self._ensureCurrentContentIsNavigable():
			return
		info = self.navigator.moveToFirstLine()
		self._playLineNavigationCue(atBoundary=True)
		self._speakTextInfo(info, textInfos.UNIT_LINE)

	def moveToLastLine(self) -> None:
		"""Move to and report the last clipboard line."""
		if not self._ensureCurrentContentIsNavigable():
			return
		info = self.navigator.moveToLastLine()
		self._playLineNavigationCue(atBoundary=True)
		self._speakTextInfo(info, textInfos.UNIT_LINE)

	def moveLine(self, direction: int, lineCount: int = 1) -> None:
		"""Move and report one or more clipboard lines."""
		if not self._ensureCurrentContentIsNavigable():
			return
		result = self.navigator.move(textInfos.UNIT_LINE, direction)
		for _ in range(lineCount - 1):
			if result.isAtBoundary or result.isAtStoryBoundary:
				break
			result = self.navigator.move(textInfos.UNIT_LINE, direction)
		self._playLineNavigationCue(atBoundary=result.isAtBoundary or result.isAtStoryBoundary)
		self._speakTextInfo(result.textInfo, textInfos.UNIT_LINE)

	def moveNavigationUnit(self, direction: int) -> None:
		"""Move and report one configured clipboard navigation unit."""
		if not self._ensureCurrentContentIsNavigable():
			return
		result = self.navigator.move(textInfos.UNIT_WORD, direction)
		self._playInlineNavigationCue(result)
		self._speakTextInfo(result.textInfo, textInfos.UNIT_WORD)

	def moveCharacter(self, direction: int) -> None:
		"""Move and report one clipboard character."""
		if not self._ensureCurrentContentIsNavigable():
			return
		result = self.navigator.move(textInfos.UNIT_CHARACTER, direction)
		self._playInlineNavigationCue(result)
		self._speakTextInfo(result.textInfo, textInfos.UNIT_CHARACTER)

	def reportCurrentLine(self, repeatCount: int) -> None:
		"""Report, spell, or describe the current clipboard line."""
		if not self._ensureCurrentContentIsNavigable():
			return
		self._reportTextInfo(self.navigator.getCurrentLine(), textInfos.UNIT_LINE, repeatCount)

	def reportCurrentNavigationUnit(self, repeatCount: int) -> None:
		"""Report, spell, or describe the current clipboard navigation unit."""
		if not self._ensureCurrentContentIsNavigable():
			return
		self._reportTextInfo(self.navigator.getCurrentNavigationUnit(), textInfos.UNIT_WORD, repeatCount)

	def reportCurrentCharacter(self, repeatCount: int) -> None:
		"""Report the current character using NVDA review-command repeat semantics."""
		if not self._ensureCurrentContentIsNavigable():
			return
		info = self.navigator.getCurrentCharacter()
		if repeatCount == 0:
			self._speakTextInfo(info, textInfos.UNIT_CHARACTER)
		elif repeatCount == 1:
			braille.handler.message(info.text)
			speech.spellTextInfo(info, useCharacterDescriptions=True)
		else:
			self._reportCharacterCodePoints(info.text)

	def appendLastSpokenText(self) -> None:
		"""Append the most recent NVDA speech text to text clipboard content."""
		text = self._requireLastSpokenText()
		self._appendTextToClipboard(
			text,
			canReplaceNonText=False,
		)
		textLength = len(text)
		if textLength < 1024:
			spokenText = text
		else:
			# Translators: Spoken instead of lengthy text appended to the clipboard.
			spokenText = ngettext("%d character", "%d characters", textLength) % textLength
		ui.message(
			# Translators: Announced after text is appended to the clipboard.
			_("Appended to clipboard: {text}").format(text=spokenText),
			# Translators: Displayed in braille after text is appended to the clipboard.
			brailleText=_("Appended: {text}").format(text=text),
		)

	def pasteLastSpokenText(self, triggerKeyCodes: frozenset[int]) -> None:
		"""Schedule a temporary paste of the most recent NVDA speech text."""
		text = _normalizeUnicodeText(self._requireLastSpokenText())
		if self._lastSpokenPasteInProgress:
			# Translators: Message shown when another last-spoken paste is still running.
			raise RuntimeError(_("A last spoken paste is already in progress"))
		self._lastSpokenPasteInProgress = True
		self._beginLastSpokenPaste(
			text,
			0,
			triggerKeyCodes,
			monotonic() + _PASTE_KEY_RELEASE_TIMEOUT_SECONDS,
		)

	def appendSelectedText(self) -> None:
		"""Append the focused selection to existing clipboard text."""
		selectedText = self._getSelectionText()
		if not selectedText:
			# Translators: Message shown when append is requested without selected text.
			ui.message(_("No text is selected"))
			return
		self._appendTextToClipboard(selectedText, canReplaceNonText=True)
		# Translators: Confirmation after selected text is appended to the clipboard.
		ui.message(_("Selected text appended"))

	def copyNavigatorObjectImage(self) -> None:
		"""Copy a screenshot of the current navigator object."""
		if isScreenCurtainEnabled():
			# Translators: Message shown when a screenshot is blocked by screen curtain.
			ui.message(_("Disable screen curtain before taking a screenshot"))
			return
		try:
			pngData = captureNavigatorObjectPng()
		except Exception:
			log.exception("Failed to copy a navigator object screenshot.")
			# Translators: Error shown when a navigator object screenshot cannot be copied.
			ui.message(_("Could not copy the navigator object image"))
			return
		if pngData is None:
			# Translators: Error shown when a navigator object screenshot cannot be copied.
			ui.message(_("Could not copy the navigator object image"))
			return
		imageInfo = getPngImageInfo(pngData)
		if imageInfo is None:
			# Translators: Error shown when a navigator object screenshot cannot be encoded for history.
			ui.message(_("Could not prepare the navigator object image"))
			return
		self._writeSnapshot(
			ClipboardSnapshot(
				ClipboardContentType.IMAGE,
				imageFormat="PNG",
				imageData=pngData,
				imageWidth=imageInfo[0],
				imageHeight=imageInfo[1],
				imageBitDepth=imageInfo[2],
			),
			_ClipboardChangeSource.NAVIGATOR_SCREENSHOT,
		)
		# Translators: Confirmation after a navigator object screenshot is copied.
		ui.message(_("Navigator object image copied to the clipboard"))

	def saveCurrentClipboardImage(self) -> None:
		"""Save the current clipboard image using NVDA's main frame as parent."""
		mainFrame = gui.mainFrame
		if mainFrame is None:
			raise RuntimeError(_MAIN_FRAME_UNAVAILABLE)
		mainFrame.prePopup()
		try:
			self._saveCurrentClipboardImage(mainFrame)
		finally:
			mainFrame.postPopup()

	def moveToNextHistoryItem(self) -> None:
		"""Move to and report the next older history entry."""
		self._moveHistoryItem(1)

	def moveToPreviousHistoryItem(self) -> None:
		"""Move to and report the previous newer history entry."""
		self._moveHistoryItem(-1)

	def restoreCurrentHistoryItem(self) -> None:
		"""Put the globally selected history entry on the system clipboard."""
		try:
			item, resolvedIndex, _itemCount = self.storage.getHistorySummaryAt(self._historyIndex)
		except StorageError as error:
			self._raiseUserStorageError(error)
		if item is None:
			self._reportEmptyHistory()
			return
		self._historyIndex = resolvedIndex
		self.restoreItemToClipboard(_HISTORY_CATEGORY_ID, item.itemId)

	def getCategories(self) -> list[CategoryId]:
		"""Return stable category identifiers with history first."""
		return [_HISTORY_CATEGORY_ID, *self.storage.categoryNames]

	def getCategoryLabel(self, category: CategoryId) -> str:
		"""Return the localized display label for a category identifier."""
		if self.isHistoryCategory(category):
			return _HISTORY_CATEGORY_NAME
		assert isinstance(category, str)
		if category.casefold() == _HISTORY_CATEGORY_NAME.casefold():
			# Translators: Disambiguates a synchronized user category from the built-in history view.
			return _("{name} (OneDrive category)").format(name=category)
		return category

	def isHistoryCategory(self, category: object) -> bool:
		"""Return whether a stable category identifier represents history."""
		return category is _HISTORY_CATEGORY_ID

	def getItems(
		self,
		category: CategoryId,
	) -> tuple[tuple[int, str, ClipboardItemType], ...]:
		"""Return stable identifiers, concise summaries, and types in one metadata query."""
		return tuple(
			(item.itemId, self._formatItemSummary(item), item.contentType)
			for item in self._getCategorySummaries(category)
		)

	def getSearchResultKeys(
		self,
		category: CategoryId,
		items: tuple[tuple[int, str, ClipboardItemType], ...],
		keywords: tuple[str, ...],
		cancelEvent: Event,
	) -> tuple[int, ...]:
		"""Scan stable item keys in order for a cancellable background search."""
		if cancelEvent.is_set():
			return ()
		# ponytail: Re-scan off-thread to bound memory; add a byte-capped cache only if profiling justifies it.
		visibleSummaries = {itemId: summary for itemId, summary, _itemType in items}
		matchingKeys: list[int] = []
		try:
			if self.isHistoryCategory(category):
				contents = self.storage.iterHistoryItemContents()
			else:
				assert isinstance(category, str)
				contents = self.storage.iterCategoryItemContents(category)
			with closing(contents):
				for item in contents:
					if cancelEvent.is_set():
						break
					visibleSummary = visibleSummaries.get(item.itemId)
					if visibleSummary is None:
						continue
					fields = [visibleSummary]
					if item.contentType in (
						ClipboardItemType.PLAIN_TEXT,
						ClipboardItemType.FORMATTED_TEXT,
						ClipboardItemType.TEXT_AND_IMAGE,
					):
						fields.append(item.text)
					elif item.contentType == ClipboardItemType.FILES:
						fields.extend(item.files)
					matches = matchesSearchKeywords(
						(normalizeSearchText(field) for field in fields if not cancelEvent.is_set()),
						keywords,
					)
					if cancelEvent.is_set():
						break
					if matches:
						matchingKeys.append(item.itemId)
		except StorageError as error:
			self._raiseUserStorageError(error)
		return tuple(matchingKeys)

	def getItemDetails(self, category: CategoryId, itemId: int) -> ClipboardItemDetails:
		"""Load the selected entry only and describe it for the manager content control."""
		item = self._getStoredItemContentById(category, itemId)
		if item.contentType in (
			ClipboardItemType.PLAIN_TEXT,
			ClipboardItemType.FORMATTED_TEXT,
			ClipboardItemType.TEXT_AND_IMAGE,
		):
			content = item.text
		elif item.contentType == ClipboardItemType.FILES:
			content = "\n".join(item.files)
		else:
			content = self._formatImageDetails(item)
		return ClipboardItemDetails(
			key=item.itemId,
			kind=item.contentType,
			content=content,
			isEditable=item.contentType
			in (
				ClipboardItemType.PLAIN_TEXT,
				ClipboardItemType.FORMATTED_TEXT,
				ClipboardItemType.TEXT_AND_IMAGE,
			),
			hasImage=item.hasImage,
			canUpload=item.canUpload,
		)

	def selectedFileGroupsHaveMissingFiles(self, category: CategoryId, itemIds: tuple[int, ...]) -> bool:
		"""Return whether selected file groups contain any missing paths."""
		for itemId in itemIds:
			item = self._getStoredItemContentById(category, itemId)
			if item.contentType == ClipboardItemType.FILES and any(
				_isMissingFilePath(filePath) for filePath in item.files
			):
				return True
		return False

	def getNavigationPositionForText(self, text: str) -> tuple[int, int] | None:
		"""Return the navigation offset and sequence when text is the current clipboard text."""
		sequenceNumber = self._lastAppliedSequenceNumber
		if not (
			text
			and self._contentType
			in (
				ClipboardContentType.TEXT,
				ClipboardContentType.FORMATTED_TEXT,
				ClipboardContentType.TEXT_AND_IMAGE,
			)
			and text == self._text
			and sequenceNumber
			and self.monitor.getSequenceNumber() == sequenceNumber
		):
			return None
		return self.navigator.getPosition(), sequenceNumber

	def setNavigationPositionForText(
		self,
		text: str,
		offset: int,
		*,
		expectedSequenceNumber: int,
	) -> bool:
		"""Set the navigation offset only while the matching clipboard snapshot remains current."""
		state = self.getNavigationPositionForText(text)
		if state is None or state[1] != expectedSequenceNumber:
			return False
		previousOffset = state[0]
		self.navigator.setPosition(offset)
		if self.monitor.getSequenceNumber() != expectedSequenceNumber:
			self.navigator.setPosition(previousOffset)
			return False
		return True

	def replaceClipboardWithText(self, text: str, *, canUpload: bool) -> bool:
		"""Replace or clear the system clipboard from manager editor text."""
		if text:
			self._writeClipboardText(
				text,
				_ClipboardChangeSource.MANAGER_TEXT_REPLACEMENT,
				canUpload=canUpload,
			)
			return True
		if not self._confirmClearClipboard():
			return False
		self._clearClipboard()
		return True

	def savePlainTextToCategory(
		self,
		category: CategoryId,
		text: str,
		*,
		canUpload: bool,
		notifyManager: bool = True,
	) -> None:
		"""Save an immutable plain-text copy and optionally queue a manager refresh."""
		text = _normalizeUnicodeText(text)
		if self.isHistoryCategory(category):
			# Translators: Error shown when a draft is saved directly to history.
			raise ValueError(_("Clipboard history can only be changed through the system clipboard"))
		if not text:
			# Translators: Error shown when an empty category entry is requested.
			raise ValueError(_("An empty entry cannot be saved"))
		assert isinstance(category, str)
		try:
			self.storage.addCategoryItem(
				category,
				ClipboardItem(ClipboardItemType.PLAIN_TEXT, text=text, canUpload=canUpload),
			)
		except StorageError as error:
			self._raiseUserStorageError(error)
		self.oneDriveSync.notifyLocalChange()
		if notifyManager:
			self._refreshManager()

	def restoreItemToClipboard(self, category: CategoryId, itemId: int) -> None:
		"""Restore every supported format of one stored item."""
		expectedSequenceNumber = self.monitor.getSequenceNumber()
		self._cancelPendingPngRestore()
		item = self._getStoredItemById(category, itemId)
		snapshot = self._snapshotFromItem(item)
		source = (
			_ClipboardChangeSource.HISTORY_RESTORE
			if self.isHistoryCategory(category)
			else _ClipboardChangeSource.CATEGORY_RESTORE
		)
		confirmation = self._formatRestoreConfirmation(item)
		if snapshot.imageFormat != "PNG":
			try:
				self._writeSnapshot(
					snapshot,
					source,
					expectedSequenceNumber=expectedSequenceNumber,
				)
			except ClipboardSequenceChangedError:
				# Translators: Error shown when another application changes the clipboard during a restore.
				ui.message(_("The clipboard changed before it could be updated. Try again."))
				return
			ui.message(confirmation)
			return
		future = self._historyExecutor.submit(
			self._preparePngRestore,
			snapshot,
			source,
			expectedSequenceNumber,
			confirmation,
		)
		self._pendingPngRestore = future
		future.add_done_callback(self._queuePngRestoreCompletion)

	def _cancelPendingPngRestore(self) -> None:
		"""Cancel a queued PNG restore and make any running result stale."""
		future = self._pendingPngRestore
		self._pendingPngRestore = None
		if future is not None:
			future.cancel()

	@staticmethod
	def _preparePngRestore(
		snapshot: ClipboardSnapshot,
		source: _ClipboardChangeSource,
		expectedSequenceNumber: int,
		confirmation: str,
	) -> _PreparedPngRestore:
		"""Decode a stored PNG into its DIB fallback on the serial worker."""
		imageData = snapshot.imageData
		if snapshot.imageFormat != "PNG" or imageData is None:
			raise ValueError(snapshot.imageFormat)
		dibData = pngToPackedDib(
			imageData,
			(snapshot.imageWidth, snapshot.imageHeight, snapshot.imageBitDepth),
		)
		if dibData is None:
			raise ValueError(snapshot.imageFormat)
		return _PreparedPngRestore(
			snapshot=snapshot,
			source=source,
			expectedSequenceNumber=expectedSequenceNumber,
			confirmation=confirmation,
			dibData=dibData,
		)

	def _queuePngRestoreCompletion(self, future: Future[_PreparedPngRestore]) -> None:
		"""Queue one prepared PNG restore for main-thread validation and commit."""
		try:
			wx.CallAfter(self._finishPngRestore, future)
		except RuntimeError:
			if future is self._pendingPngRestore:
				self._pendingPngRestore = None
			if self._isStarted:
				log.debugWarning("Could not schedule a prepared PNG clipboard restore.", exc_info=True)

	def _finishPngRestore(self, future: Future[_PreparedPngRestore]) -> None:
		"""Commit the latest prepared PNG restore without replacing newer clipboard data."""
		if future is not self._pendingPngRestore:
			return
		self._pendingPngRestore = None
		if not self._isStarted:
			return
		try:
			prepared = future.result()
		except ValueError as error:
			log.debugWarning("Stored PNG data could not be prepared for clipboard restore.", exc_info=error)
			# Translators: Error shown when supported clipboard formats cannot be restored.
			ui.message(_("Could not write to the system clipboard"))
			return
		except Exception as error:
			log.exception("Failed to prepare stored PNG data for clipboard restore.", exc_info=error)
			# Translators: Error shown when supported clipboard formats cannot be restored.
			ui.message(_("Could not write to the system clipboard"))
			return
		if self.monitor.getSequenceNumber() != prepared.expectedSequenceNumber:
			# Translators: Error shown when another application changes the clipboard during a restore.
			ui.message(_("The clipboard changed before it could be updated. Try again."))
			return
		try:
			self._writeSnapshot(
				prepared.snapshot,
				prepared.source,
				expectedSequenceNumber=prepared.expectedSequenceNumber,
				preparedPngDib=prepared.dibData,
			)
		except ClipboardSequenceChangedError:
			# Translators: Error shown when another application changes the clipboard during a restore.
			ui.message(_("The clipboard changed before it could be updated. Try again."))
			return
		except RuntimeError as error:
			log.debugWarning("Could not commit prepared PNG clipboard data.", exc_info=error)
			ui.message(str(error))
			return
		except Exception as error:
			log.exception("Failed to commit prepared PNG clipboard data.", exc_info=error)
			# Translators: Error shown when supported clipboard formats cannot be restored.
			ui.message(_("Could not write to the system clipboard"))
			return
		ui.message(prepared.confirmation)

	def transferItemsToCategory(
		self,
		sourceCategory: CategoryId,
		itemIds: tuple[int, ...],
		targetCategory: CategoryId,
	) -> None:
		"""Transfer stored entries between categories."""
		if self.isHistoryCategory(targetCategory):
			# Translators: Error shown when an item is moved into clipboard history.
			raise ValueError(_("Entries cannot be moved into clipboard history"))
		assert isinstance(targetCategory, str)
		try:
			if self.isHistoryCategory(sourceCategory):
				self.storage.copyHistoryItemsToCategoryById(itemIds, targetCategory)
			else:
				assert isinstance(sourceCategory, str)
				self.storage.moveCategoryItemsById(sourceCategory, itemIds, targetCategory)
		except (StorageError, ValueError) as error:
			self._raiseUserStorageError(error)
		self.oneDriveSync.notifyLocalChange()

	def deleteItems(self, category: CategoryId, itemIds: tuple[int, ...]) -> None:
		"""Delete stored entries."""
		try:
			if self.isHistoryCategory(category):
				self.storage.deleteHistoryItemsById(itemIds)
				self._historyIndex = self.storage.getHistorySummaryAt(self._historyIndex)[1]
			else:
				assert isinstance(category, str)
				self.storage.deleteCategoryItemsById(category, itemIds)
		except StorageError as error:
			self._raiseUserStorageError(error)
		self.oneDriveSync.notifyLocalChange()

	def removeMissingFiles(self, category: CategoryId, itemIds: tuple[int, ...]) -> MissingFileCleanupResult:
		"""Remove missing paths from selected file groups."""
		try:
			if self.isHistoryCategory(category):
				changedCount, deletedCount, removedCount, replacementIds = (
					self.storage.removeMissingHistoryFileReferences(
						itemIds,
						_isMissingFilePath,
					)
				)
				if changedCount:
					self._historyIndex = self.storage.getHistorySummaryAt(self._historyIndex)[1]
			else:
				assert isinstance(category, str)
				changedCount, deletedCount, removedCount, replacementIds = (
					self.storage.removeMissingCategoryFileReferences(
						category,
						itemIds,
						_isMissingFilePath,
					)
				)
		except StorageError as error:
			self._raiseUserStorageError(error)
		if changedCount:
			self.oneDriveSync.notifyLocalChange()
		return MissingFileCleanupResult(changedCount, deletedCount, removedCount, replacementIds)

	def createCategory(self, name: str) -> str:
		"""Create a user category and return its stored name."""
		try:
			actualName = self.storage.createCategory(name)
		except StorageError as error:
			self._raiseUserStorageError(error)
		self.oneDriveSync.notifyLocalChange()
		return actualName

	def renameCategory(self, oldName: str, newName: str) -> str:
		"""Rename a user category and return its stored name."""
		try:
			actualName = self.storage.renameCategory(oldName, newName)
		except StorageError as error:
			self._raiseUserStorageError(error)
		self.oneDriveSync.notifyLocalChange()
		return actualName

	def deleteCategory(self, name: str) -> None:
		"""Delete an empty user category."""
		try:
			self.storage.deleteCategory(name)
		except StorageError as error:
			self._raiseUserStorageError(error)
		self.oneDriveSync.notifyLocalChange()

	def _saveCurrentClipboardImage(self, parent: wx.Window) -> None:
		"""Save the current clipboard image as a validated PNG file."""
		snapshot = self.monitor.readNow(decodePng=True)
		if snapshot.contentType == ClipboardContentType.ERROR:
			# Translators: Error shown when the current clipboard image cannot be read.
			raise RuntimeError(_("Could not read the clipboard image"))
		if snapshot.contentType not in (ClipboardContentType.IMAGE, ClipboardContentType.TEXT_AND_IMAGE):
			# Translators: Message shown when no clipboard image is available to save.
			ui.message(_("There is no clipboard image to save"))
			return
		snapshot, _imageWasTooLarge = self._normalizeImageSnapshot(
			snapshot,
			pngIsDecodable=snapshot.imageFormat == "PNG" and snapshot.imageData is not None,
		)
		if snapshot.imageData is None:
			# Translators: Error shown when an expected clipboard image cannot be read.
			raise RuntimeError(_("Could not read the clipboard image"))
		try:
			# Translators: Title of the dialog used to save the current clipboard image.
			result = savePngImage(parent, snapshot.imageData, _("Save clipboard image"))
		except OSError as error:
			# Translators: Error shown when the clipboard image cannot be saved.
			raise RuntimeError(_("Could not save the clipboard image")) from error
		if result is None:
			# Translators: Error shown when an expected clipboard image cannot be read.
			raise RuntimeError(_("Could not read the clipboard image"))
		if not result:
			return
		# Translators: Confirmation after saving a clipboard image.
		ui.message(_("Clipboard image saved"))

	def saveStoredItemImage(self, category: CategoryId, itemId: int, parent: wx.Window) -> None:
		"""Save the normalized PNG contained by one stored entry."""
		item = self._getStoredItemById(category, itemId)
		if item.imageData is None:
			# Translators: Error shown when a selected history entry has no exportable image.
			raise ValueError(_("The selected entry does not contain a saved image"))
		try:
			# Translators: Title of the dialog used to save an image selected in clipboard history.
			result = savePngImage(parent, item.imageData, _("Save selected image"))
		except OSError as error:
			# Translators: Error shown when a stored clipboard image cannot be saved.
			raise RuntimeError(_("Could not save the selected image")) from error
		if result is None:
			# Translators: Error shown when stored PNG image data is invalid.
			raise RuntimeError(_("The saved image data is invalid"))
		if result:
			# Translators: Confirmation after saving an image selected from clipboard history.
			ui.message(_("Selected image saved"))

	def _onClipboardSnapshot(self, snapshot: ClipboardSnapshot) -> None:
		"""Handle a monitor snapshot not already processed by a local write."""
		if not self._isStarted or snapshot.contentType == ClipboardContentType.ERROR:
			return
		if self._consumePendingWrite(snapshot):
			self._awaitingInitialSnapshot = False
			if snapshot.contentType in (ClipboardContentType.IMAGE, ClipboardContentType.TEXT_AND_IMAGE):
				self._summary = self._formatCurrentSummary(snapshot)
				if snapshot.contentType == ClipboardContentType.IMAGE:
					self.navigator.setText(self._summary)
			return
		state = self._lastSpokenPasteState
		if state is not None and _isLastSpokenTemporarySnapshot(snapshot, state):
			self._awaitingInitialSnapshot = False
			self._lastAppliedSequenceNumber = snapshot.sequenceNumber
			return
		source = (
			_ClipboardChangeSource.INITIAL
			if self._awaitingInitialSnapshot
			else _ClipboardChangeSource.EXTERNAL
		)
		self._awaitingInitialSnapshot = False
		self._applySnapshot(
			snapshot,
			source,
			pngIsDecodable=snapshot.imageFormat == "PNG" and snapshot.imageData is not None,
		)

	def _consumePendingWrite(self, snapshot: ClipboardSnapshot) -> bool:
		if snapshot.sequenceNumber in self._pendingWrites:
			self._pendingWrites.discard(snapshot.sequenceNumber)
			return True
		self._pendingWrites.clear()
		return False

	def _applySnapshot(
		self,
		snapshot: ClipboardSnapshot,
		source: _ClipboardChangeSource,
		*,
		pngIsDecodable: bool = False,
	) -> None:
		"""Apply current state immediately and queue source-dependent history work."""
		if snapshot.contentType == ClipboardContentType.ERROR:
			return
		shouldRecord = source in {
			_ClipboardChangeSource.EXTERNAL,
			_ClipboardChangeSource.APPEND_TEXT,
			_ClipboardChangeSource.MANAGER_TEXT_REPLACEMENT,
			_ClipboardChangeSource.HISTORY_RESTORE,
			_ClipboardChangeSource.CATEGORY_RESTORE,
			_ClipboardChangeSource.NAVIGATOR_SCREENSHOT,
		}
		if snapshot.sequenceNumber:
			self._lastAppliedSequenceNumber = snapshot.sequenceNumber
		self._applyCurrentSnapshot(snapshot)
		if shouldRecord and snapshot.canIncludeInHistory:
			if snapshot.contentType in (ClipboardContentType.IMAGE, ClipboardContentType.TEXT_AND_IMAGE):
				self._queueImageHistorySnapshot(snapshot, pngIsDecodable=pngIsDecodable)
			else:
				item = self._clipboardItemFromSnapshot(snapshot)
				if item is not None:
					self._queueHistoryItem(item)
		if shouldRecord and snapshot.canUpload and snapshot.text:
			cloudSync = self.cloudSync
			if cloudSync is not None:
				cloudSync.onClipboardTextChanged(snapshot.text)
		if shouldRecord and snapshot.canIncludeInHistory and snapshot.richFormatsDropped and snapshot.text:
			# Translators: Message shown when oversized rich formats are omitted from a history entry.
			ui.message(_("Some text formatting was too large and was not saved in history"))
		if shouldRecord:
			playCopy()

	def _applyCurrentSnapshot(self, snapshot: ClipboardSnapshot) -> None:
		"""Update current clipboard navigation without reading or writing history."""
		self._pendingFileSizeReport = None
		if self._fileSizeCalculation is not None:
			self._fileSizeCalculation.cancel()
		self._fileSizeCalculation = (
			FileSizeCalculation(snapshot.files)
			if snapshot.contentType == ClipboardContentType.FILES and snapshot.files
			else None
		)
		self._pendingTextStatisticsReport = None
		if self._textStatisticsCalculation is not None:
			self._textStatisticsCalculation.cancel()
		self._textStatisticsCalculation = (
			TextStatisticsCalculation(snapshot.text)
			if snapshot.text
			and snapshot.contentType
			in (
				ClipboardContentType.TEXT,
				ClipboardContentType.FORMATTED_TEXT,
				ClipboardContentType.TEXT_AND_IMAGE,
			)
			else None
		)
		self._contentType = snapshot.contentType
		self._text = snapshot.text
		self._canUpload = snapshot.canUpload
		self._summary = self._formatCurrentSummary(snapshot)
		if (
			snapshot.contentType
			in (
				ClipboardContentType.TEXT,
				ClipboardContentType.FORMATTED_TEXT,
				ClipboardContentType.TEXT_AND_IMAGE,
			)
			and snapshot.text
		):
			navigationText = snapshot.text
		elif snapshot.contentType == ClipboardContentType.FILES:
			navigationText = self._formatFileNavigationText(snapshot.files)
		else:
			navigationText = self._summary
		self.navigator.setText(navigationText)

	def _queueHistoryItem(self, item: ClipboardItem, imageWasDropped: bool = False) -> None:
		"""Queue one immutable item for ordered history storage."""
		future = self._historyExecutor.submit(self._storeHistoryItem, item, imageWasDropped)
		future.add_done_callback(self._queueHistoryWriteCompletion)

	def _queueImageHistorySnapshot(self, snapshot: ClipboardSnapshot, *, pngIsDecodable: bool) -> None:
		"""Queue one image snapshot without allowing unbounded retained image data."""
		pngIsDecodable = pngIsDecodable and snapshot.imageFormat == "PNG"
		imageDataMissing = snapshot.imageData is None
		if imageDataMissing or not self._imageHistorySlots.acquire(blocking=False):
			item = self._clipboardItemFromSnapshot(
				replace(snapshot, imageFormat=None, imageData=None),
			)
			if item is None and imageDataMissing:
				# Translators: Message shown when a clipboard image could not be normalized for history.
				ui.message(_("The image could not be added to clipboard history"))
			elif item is not None:
				self._queueHistoryItem(item, imageWasDropped=imageDataMissing)
			return
		try:
			future = self._historyExecutor.submit(
				self._storeImageHistorySnapshot,
				snapshot,
				pngIsDecodable,
			)
		except Exception:
			self._imageHistorySlots.release()
			raise
		future.add_done_callback(self._queueImageHistoryWriteCompletion)

	def _storeImageHistorySnapshot(
		self,
		snapshot: ClipboardSnapshot,
		pngIsDecodable: bool,
	) -> _HistoryWriteResult:
		"""Normalize and store one retained image snapshot on the serial worker."""
		snapshot, imageWasTooLarge = self._normalizeImageSnapshot(
			snapshot,
			pngIsDecodable=pngIsDecodable,
		)
		item = self._clipboardItemFromSnapshot(snapshot)
		if item is None:
			return _HistoryWriteResult(
				None,
				imageWasDropped=True,
				imageWasTooLarge=imageWasTooLarge,
			)
		return self._storeHistoryItem(
			item,
			imageWasDropped=snapshot.imageData is None,
			imageWasTooLarge=imageWasTooLarge,
		)

	def _storeHistoryItem(
		self,
		item: ClipboardItem,
		imageWasDropped: bool,
		imageWasTooLarge: bool = False,
	) -> _HistoryWriteResult:
		"""Write history on the serial worker, preserving text when a mixed image is rejected."""
		itemId = self.storage.addHistory(item)
		if itemId is not None or item.contentType != ClipboardItemType.TEXT_AND_IMAGE:
			return _HistoryWriteResult(
				itemId,
				imageWasDropped=imageWasDropped,
				imageWasTooLarge=imageWasTooLarge or len(item.imageData or b"") > MAX_IMAGE_BYTES,
			)
		fallbackType = (
			ClipboardItemType.FORMATTED_TEXT
			if item.html is not None or item.rtf is not None
			else ClipboardItemType.PLAIN_TEXT
		)
		fallbackId = self.storage.addHistory(
			ClipboardItem(
				fallbackType,
				text=item.text,
				html=item.html,
				rtf=item.rtf,
				canUpload=item.canUpload,
			),
		)
		return _HistoryWriteResult(
			fallbackId,
			imageWasDropped=True,
			imageWasTooLarge=imageWasTooLarge or len(item.imageData or b"") > MAX_IMAGE_BYTES,
		)

	def _queueHistoryWriteCompletion(self, future: Future[_HistoryWriteResult]) -> None:
		try:
			wx.CallAfter(self._finishHistoryWrite, future)
		except RuntimeError:
			if self._isStarted:
				log.debugWarning("Could not schedule a clipboard history write result.", exc_info=True)

	def _queueImageHistoryWriteCompletion(self, future: Future[_HistoryWriteResult]) -> None:
		"""Release one image slot before reporting its history result."""
		self._imageHistorySlots.release()
		self._queueHistoryWriteCompletion(future)

	def _finishHistoryWrite(self, future: Future[_HistoryWriteResult]) -> None:
		"""Report history write results and refresh visible metadata on the main thread."""
		if not self._isStarted:
			return
		try:
			result = future.result()
		except Exception:
			if not self._historySaveFailureReported:
				self._historySaveFailureReported = True
				log.exception("Failed to save clipboard history.")
				# Translators: Error shown when clipboard content changed but history could not be saved.
				ui.message(_("Clipboard updated, but history could not be saved"))
			return
		self._historySaveFailureReported = False
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
		self._historyIndex = 0
		if result.imageWasDropped:
			# Translators: Message shown when mixed clipboard content is retained as text only.
			ui.message(_("The text was saved in history, but its image could not be stored"))
		self.oneDriveSync.notifyLocalChange()
		self._refreshManager()

	def _clipboardItemFromSnapshot(self, snapshot: ClipboardSnapshot) -> ClipboardItem | None:
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

	def _normalizeImageSnapshot(
		self,
		snapshot: ClipboardSnapshot,
		*,
		pngIsDecodable: bool = False,
		allowBitmapFallback: bool = True,
	) -> tuple[ClipboardSnapshot, bool]:
		"""Return a PNG-backed snapshot and whether normalization exceeded the byte limit."""
		if snapshot.contentType not in (ClipboardContentType.IMAGE, ClipboardContentType.TEXT_AND_IMAGE):
			return snapshot, False
		canDecodeSafely = isImageSizeSafe(
			snapshot.imageWidth,
			snapshot.imageHeight,
			max(32, snapshot.imageBitDepth),
		)
		if not canDecodeSafely:
			return replace(snapshot, imageFormat=None, imageData=None), False
		if snapshot.imageFormat == "PNG" and snapshot.imageData is not None and not pngIsDecodable:
			pngIsDecodable = isPngImageDecodable(
				snapshot.imageData,
				(snapshot.imageWidth, snapshot.imageHeight, snapshot.imageBitDepth),
			)
		try:
			pngData = (
				normalizeImageDataToPng(
					snapshot.imageFormat,
					snapshot.imageData,
					(snapshot.imageWidth, snapshot.imageHeight, snapshot.imageBitDepth),
					pngIsDecodable=pngIsDecodable,
				)
				if snapshot.imageData is not None
				else None
			)
		except ImageDataTooLargeError:
			return replace(snapshot, imageFormat=None, imageData=None), True
		if (
			pngData is None
			and allowBitmapFallback
			and snapshot.imageFormat in ("DIB", "DIBV5")
			and (fallback := self.monitor.readBitmapDib(snapshot.sequenceNumber)) is not None
		):
			fallbackData, fallbackInfo = fallback
			return self._normalizeImageSnapshot(
				replace(
					snapshot,
					imageFormat="DIB",
					imageData=fallbackData,
					imageWidth=fallbackInfo[0],
					imageHeight=fallbackInfo[1],
					imageBitDepth=fallbackInfo[2],
				),
				allowBitmapFallback=False,
			)
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

	def _requireLastSpokenText(self) -> str:
		text = lastSpoken.getText()
		if not text:
			# Translators: Error shown when a last-spoken command has no captured speech text.
			raise ValueError(_("No spoken text is available"))
		return text

	def _appendTextToClipboard(self, text: str, *, canReplaceNonText: bool) -> None:
		"""Append text while preserving current clipboard policy flags."""
		snapshot = self.monitor.readNow()
		if snapshot.contentType == ClipboardContentType.ERROR:
			# Translators: Error shown when text cannot be appended because the clipboard is busy.
			raise RuntimeError(_("Could not read the current clipboard"))
		if not snapshot.sequenceNumber or snapshot.sequenceNumber != self._lastAppliedSequenceNumber:
			self._pendingWrites.clear()
			self._awaitingInitialSnapshot = False
			self._applySnapshot(snapshot, _ClipboardChangeSource.EXTERNAL)
		textContentTypes = (
			ClipboardContentType.TEXT,
			ClipboardContentType.FORMATTED_TEXT,
			ClipboardContentType.TEXT_AND_IMAGE,
		)
		if snapshot.contentType in textContentTypes:
			clipboardText = snapshot.text
		elif snapshot.contentType == ClipboardContentType.EMPTY or canReplaceNonText:
			clipboardText = ""
		else:
			# Translators: Error shown when last spoken text would replace non-text clipboard content.
			raise ValueError(_("Last spoken text can only be appended to an empty or text clipboard"))
		separator = "\n" if clipboardText and not clipboardText.endswith(("\r", "\n")) else ""
		self._writeClipboardText(
			f"{clipboardText}{separator}{text}",
			_ClipboardChangeSource.APPEND_TEXT,
			expectedSequenceNumber=snapshot.sequenceNumber,
			canIncludeInHistory=snapshot.canIncludeInHistory,
			canUpload=snapshot.canUpload,
		)

	def _beginLastSpokenPaste(  # noqa: C901 - ordered recovery branches keep clipboard ownership explicit.
		self,
		text: str,
		retryCount: int,
		triggerKeyCodes: frozenset[int],
		keyReleaseDeadline: float,
	) -> None:
		"""Write and paste temporary text, retrying one clipboard race."""
		if not self._isStarted:
			self._lastSpokenPasteInProgress = False
			return
		originalSnapshot: ClipboardSnapshot | None = None
		originalNavigationOffset: int | None = None
		try:
			keyReleaseState = _waitForTriggerKeysReleased(
				triggerKeyCodes,
				keyReleaseDeadline,
				lambda: self._beginLastSpokenPaste(
					text,
					retryCount,
					triggerKeyCodes,
					keyReleaseDeadline,
				),
			)
		except Exception:
			self._lastSpokenPasteInProgress = False
			log.exception("Failed while waiting for the last-spoken paste keys to be released.")
			# Translators: Error shown when temporary last-spoken text cannot be prepared for pasting.
			ui.message(_("Could not paste the last spoken text"))
			return
		if keyReleaseState is None:
			return
		if not keyReleaseState:
			self._lastSpokenPasteInProgress = False
			# Translators: Message shown when a paste shortcut remains held until timeout.
			ui.message(_("The keyboard shortcut was not released, so the paste was cancelled"))
			return
		try:
			originalSnapshot, expectedSequenceNumber = self._captureLastSpokenOriginalClipboard()
			if (
				originalSnapshot is not None
				and originalSnapshot.sequenceNumber
				== expectedSequenceNumber
				== self._lastAppliedSequenceNumber
				== self.monitor.getSequenceNumber()
			):
				originalNavigationOffset = self.navigator.getPosition()
		except Exception:
			self._lastSpokenPasteInProgress = False
			log.exception("Failed to capture the original clipboard before a last-spoken paste.")
			# Translators: Error shown when temporary last-spoken text cannot be prepared for pasting.
			ui.message(_("Could not paste the last spoken text"))
			return
		try:
			temporarySequenceNumber = self._writeSnapshot(
				ClipboardSnapshot(
					ClipboardContentType.TEXT,
					text=text,
					canIncludeInHistory=False,
					canUpload=False,
				),
				_ClipboardChangeSource.LAST_SPOKEN_PASTE,
				expectedSequenceNumber=expectedSequenceNumber,
			)
		except ClipboardSequenceChangedError:
			if retryCount == 0:
				self._beginLastSpokenPaste(
					text,
					retryCount + 1,
					triggerKeyCodes,
					keyReleaseDeadline,
				)
				return
			self._lastSpokenPasteInProgress = False
			# Translators: Error shown when the clipboard keeps changing before a last-spoken paste.
			ui.message(_("The clipboard kept changing, so the last spoken text could not be pasted"))
			return
		except _LastSpokenTemporaryApplyError as error:
			state = _LastSpokenPasteState(
				originalSnapshot,
				error.sequenceNumber,
				text,
				originalNavigationOffset=originalNavigationOffset,
			)
			self._lastSpokenPasteState = state
			self._restoreLastSpokenClipboard(state, reportFailure=False)
			log.exception("Failed to apply temporary last-spoken text locally.")
			# Translators: Error shown when temporary last-spoken text cannot be prepared for pasting.
			ui.message(_("Could not paste the last spoken text"))
			return
		except _ClipboardWriteFailedError as error:
			state = _LastSpokenPasteState(
				originalSnapshot,
				error.sequenceNumber,
				text,
				originalNavigationOffset=originalNavigationOffset,
				allowsPartialWrite=True,
			)
			self._lastSpokenPasteState = state
			self._restoreLastSpokenClipboard(state, reportFailure=False)
			log.debugWarning("Failed to write temporary last-spoken text.", exc_info=True)
			# Translators: Error shown when temporary last-spoken text cannot be written.
			ui.message(_("Could not paste the last spoken text"))
			return
		except Exception:
			self._lastSpokenPasteInProgress = False
			log.exception("Failed to write temporary last-spoken text.")
			# Translators: Error shown when temporary last-spoken text cannot be written.
			ui.message(_("Could not paste the last spoken text"))
			return
		state = _LastSpokenPasteState(
			originalSnapshot,
			temporarySequenceNumber,
			text,
			originalNavigationOffset=originalNavigationOffset,
		)
		self._lastSpokenPasteState = state
		verifiedSequenceNumber = self._getVerifiedLastSpokenTemporarySequenceNumber(state)
		if verifiedSequenceNumber is None:
			self._pendingWrites.clear()
			self._restoreLastSpokenClipboard(state, reportFailure=False)
			self.monitor.handleClipboardUpdate()
			# Translators: Error shown when the clipboard keeps changing before a last-spoken paste.
			ui.message(_("The clipboard kept changing, so the last spoken text could not be pasted"))
			return
		if verifiedSequenceNumber != temporarySequenceNumber:
			state = replace(state, temporarySequenceNumber=verifiedSequenceNumber)
			self._lastSpokenPasteState = state
		try:
			KeyboardInputGesture.fromName("control+v").send()
		except Exception:
			log.exception("Failed to send the last-spoken paste gesture.")
			self._restoreLastSpokenClipboard(state, reportFailure=False)
			# Translators: Error shown when the last-spoken paste gesture cannot be sent.
			ui.message(_("Could not paste the last spoken text"))
			return
		try:
			callLater(
				_LAST_SPOKEN_CLIPBOARD_RESTORE_DELAY,
				self._restoreLastSpokenClipboard,
				state,
			)
		except Exception:
			log.debugWarning("Could not schedule last-spoken clipboard restoration.", exc_info=True)
			self._lastSpokenPasteState = None
			self._lastSpokenPasteInProgress = False
			# Translators: Error shown after text was pasted but clipboard restoration could not be scheduled.
			ui.message(_("The text was pasted, but the clipboard could not be restored"))
			return
		try:
			ui.delayedMessage(text)
		except Exception:
			log.debugWarning("Could not delay last-spoken paste feedback.", exc_info=True)
			ui.message(text)

	def _captureLastSpokenOriginalClipboard(self) -> tuple[ClipboardSnapshot | None, int]:
		"""Capture the best-effort restorable clipboard state and sequence number."""
		snapshot = self.monitor.readNow(decodePng=True)
		sequenceNumber = snapshot.sequenceNumber or self.monitor.getSequenceNumber()
		imageNormalizationFailed = False
		if (
			snapshot.contentType in (ClipboardContentType.IMAGE, ClipboardContentType.TEXT_AND_IMAGE)
			and snapshot.imageData is None
		):
			try:
				snapshot, _imageWasTooLarge = self._normalizeImageSnapshot(snapshot)
			except Exception:
				imageNormalizationFailed = True
				log.debugWarning(
					"Could not retain the original clipboard image before pasting.",
					exc_info=True,
				)
		if (
			not imageNormalizationFailed
			and snapshot.contentType != ClipboardContentType.ERROR
			and snapshot.sequenceNumber
			and snapshot.sequenceNumber == self.monitor.getSequenceNumber()
			and snapshot.sequenceNumber != self._lastAppliedSequenceNumber
		):
			self._pendingWrites.clear()
			self._awaitingInitialSnapshot = False
			try:
				self._applySnapshot(snapshot, _ClipboardChangeSource.EXTERNAL)
			except Exception:
				log.debugWarning("Could not apply the original clipboard before pasting.", exc_info=True)
		# Text-bearing snapshots remain useful when rich formats or a mixed image were omitted.
		if snapshot.contentType in (
			ClipboardContentType.ERROR,
			ClipboardContentType.PROTECTED,
			ClipboardContentType.UNSUPPORTED,
		) or (snapshot.contentType == ClipboardContentType.IMAGE and snapshot.imageData is None):
			return None, sequenceNumber
		return snapshot, sequenceNumber

	def _getVerifiedLastSpokenTemporarySequenceNumber(
		self,
		state: _LastSpokenPasteState,
	) -> int | None:
		"""Return a stable sequence only while the temporary clipboard text remains current."""
		temporarySnapshot = self.monitor.readNow()
		try:
			expectedOwnerHandle = self._getClipboardOwnerHandle()
		except RuntimeError:
			return None
		if (
			not _isLastSpokenTemporarySnapshot(temporarySnapshot, state)
			or self.monitor.getOwnerHandle() != expectedOwnerHandle
			or self.monitor.getSequenceNumber() != temporarySnapshot.sequenceNumber
		):
			return None
		return temporarySnapshot.sequenceNumber

	def _restoreLastSpokenClipboard(
		self,
		state: _LastSpokenPasteState,
		*,
		reportFailure: bool = True,
	) -> None:
		"""Restore the original clipboard, then fall back to newest history."""
		if self._lastSpokenPasteState is not state:
			return
		ownedSequenceNumber = state.temporarySequenceNumber
		try:
			verifiedSequenceNumber = self._getVerifiedLastSpokenTemporarySequenceNumber(state)
			if verifiedSequenceNumber is not None:
				ownedSequenceNumber = verifiedSequenceNumber
			elif (
				not state.allowsPartialWrite
				or not ownedSequenceNumber
				or self.monitor.getSequenceNumber() != ownedSequenceNumber
			):
				return
			try:
				if self._restoreOriginalLastSpokenClipboard(state, ownedSequenceNumber):
					return
			except _ClipboardWriteFailedError as error:
				ownedSequenceNumber = error.sequenceNumber
			if self.monitor.getSequenceNumber() != ownedSequenceNumber:
				return
			try:
				latestItem = self.storage.getLatestHistoryItem()
			except StorageError:
				latestItem = None
				log.debugWarning("Could not load the newest clipboard history item.", exc_info=True)
			if latestItem is not None:
				try:
					self._writeSnapshot(
						self._snapshotFromItem(latestItem),
						_ClipboardChangeSource.LAST_SPOKEN_RESTORE,
						expectedSequenceNumber=ownedSequenceNumber,
					)
					return
				except ClipboardSequenceChangedError:
					return
				except _ClipboardWriteFailedError as error:
					ownedSequenceNumber = error.sequenceNumber
					log.debugWarning("Could not restore the newest clipboard history item.", exc_info=True)
				except Exception:
					log.debugWarning("Could not restore the newest clipboard history item.", exc_info=True)
			if reportFailure and self.monitor.getSequenceNumber() == ownedSequenceNumber:
				# Translators: Error shown after text was pasted but no clipboard content could be restored.
				ui.message(_("The text was pasted, but the clipboard could not be restored"))
		finally:
			if self._lastSpokenPasteState is state:
				self._lastSpokenPasteState = None
				self._lastSpokenPasteInProgress = False

	def _restoreOriginalLastSpokenClipboard(
		self,
		state: _LastSpokenPasteState,
		expectedSequenceNumber: int,
	) -> bool:
		"""Try to restore every retained format captured before a last-spoken paste."""
		snapshot = state.originalSnapshot
		if snapshot is None:
			return False
		try:
			if snapshot.contentType == ClipboardContentType.EMPTY:
				restoredSequenceNumber = self._clearClipboard(
					source=_ClipboardChangeSource.LAST_SPOKEN_RESTORE,
					expectedSequenceNumber=expectedSequenceNumber,
				)
			else:
				restoredSequenceNumber = self._writeSnapshot(
					snapshot,
					_ClipboardChangeSource.LAST_SPOKEN_RESTORE,
					expectedSequenceNumber=expectedSequenceNumber,
				)
			if (
				state.originalNavigationOffset is not None
				and self._lastAppliedSequenceNumber
				== restoredSequenceNumber
				== self.monitor.getSequenceNumber()
			):
				self.navigator.setPosition(state.originalNavigationOffset)
			return True
		except ClipboardSequenceChangedError:
			return False
		except _ClipboardWriteFailedError:
			raise
		except Exception:
			log.debugWarning("Could not restore the original clipboard snapshot.", exc_info=True)
			return False

	def _writeClipboardText(
		self,
		text: str,
		source: _ClipboardChangeSource,
		expectedSequenceNumber: int | None = None,
		*,
		canIncludeInHistory: bool = True,
		canUpload: bool = True,
	) -> int:
		"""Write non-empty text and synchronously apply its source semantics."""
		text = _normalizeUnicodeText(text)
		if not text:
			raise ValueError(text)
		try:
			return self._writeSnapshot(
				ClipboardSnapshot(
					ClipboardContentType.TEXT,
					text=text,
					canIncludeInHistory=canIncludeInHistory,
					canUpload=canUpload,
				),
				source,
				expectedSequenceNumber=expectedSequenceNumber,
			)
		except ClipboardSequenceChangedError as error:
			# Translators: Error shown when another application changes the clipboard during an append action.
			raise RuntimeError(_("The clipboard changed before it could be updated. Try again.")) from error

	def _writeSnapshot(
		self,
		snapshot: ClipboardSnapshot,
		source: _ClipboardChangeSource,
		*,
		expectedSequenceNumber: int | None = None,
		preparedPngDib: bytes | None = None,
	) -> int:
		"""Replace the system clipboard through the monitor and apply local semantics."""
		ownerHandle = self._getClipboardOwnerHandle()
		self.monitor.invalidatePendingSnapshots()
		try:
			sequenceNumber = self.monitor.writeSnapshot(
				snapshot,
				ownerHandle,
				expectedSequenceNumber=expectedSequenceNumber,
				preparedPngDib=preparedPngDib,
			)
		except ClipboardSequenceChangedError as error:
			self._pendingWrites.clear()
			self.monitor.handleClipboardUpdate()
			if expectedSequenceNumber is None:
				# Translators: Error shown when another application changes the clipboard during a write.
				raise RuntimeError(
					_("The clipboard changed before it could be updated. Try again."),
				) from error
			raise
		except ClipboardWriteError as error:
			self._pendingWrites = {error.sequenceNumber} if error.sequenceNumber else set()
			self._applyFailedLocalWrite(error.sequenceNumber)
			self.monitor.handleClipboardUpdate()
			raise _ClipboardWriteFailedError(error.sequenceNumber) from error
		except (OSError, ValueError) as error:
			self.monitor.handleClipboardUpdate()
			# Translators: Error shown when supported clipboard formats cannot be restored.
			raise RuntimeError(_("Could not write to the system clipboard")) from error
		self._awaitingInitialSnapshot = False
		appliedSnapshot = replace(snapshot, sequenceNumber=sequenceNumber)
		self._pendingWrites = {sequenceNumber}
		try:
			self._applySnapshot(
				appliedSnapshot,
				source,
				pngIsDecodable=snapshot.imageFormat == "PNG" and snapshot.imageData is not None,
			)
		except Exception as error:
			if source == _ClipboardChangeSource.LAST_SPOKEN_PASTE:
				raise _LastSpokenTemporaryApplyError(sequenceNumber) from error
			raise
		return sequenceNumber

	def _applyFailedLocalWrite(self, sequenceNumber: int) -> None:
		"""Reflect clipboard content left behind by a failed partial local write."""
		if not sequenceNumber or self.monitor.getSequenceNumber() != sequenceNumber:
			return
		try:
			snapshot = self.monitor.readNow()
			if (
				snapshot.contentType != ClipboardContentType.ERROR
				and snapshot.sequenceNumber == sequenceNumber
				and self.monitor.getSequenceNumber() == sequenceNumber
			):
				self._applySnapshot(snapshot, _ClipboardChangeSource.LOCAL_WRITE_FAILURE)
		except Exception:
			log.debugWarning("Could not apply clipboard content left by a failed write.", exc_info=True)

	def _clearClipboard(
		self,
		*,
		source: _ClipboardChangeSource = _ClipboardChangeSource.CLEAR,
		expectedSequenceNumber: int | None = None,
	) -> int:
		"""Clear the clipboard through the monitor and apply local semantics."""
		ownerHandle = self._getClipboardOwnerHandle()
		try:
			self.monitor.invalidatePendingSnapshots()
			sequenceNumber = self.monitor.clearClipboard(
				ownerHandle,
				expectedSequenceNumber=expectedSequenceNumber,
			)
		except ClipboardSequenceChangedError as error:
			self._pendingWrites.clear()
			self.monitor.handleClipboardUpdate()
			if expectedSequenceNumber is None:
				# Translators: Error shown when another application changes the clipboard during a clear.
				raise RuntimeError(
					_("The clipboard changed before it could be updated. Try again."),
				) from error
			raise
		except OSError as error:
			self.monitor.handleClipboardUpdate()
			# Translators: Error shown when the system clipboard cannot be cleared.
			raise RuntimeError(_("Could not clear the system clipboard")) from error
		self._awaitingInitialSnapshot = False
		self._pendingWrites = {sequenceNumber}
		self._applySnapshot(
			ClipboardSnapshot(ClipboardContentType.EMPTY, sequenceNumber=sequenceNumber),
			source,
		)
		return sequenceNumber

	def _getClipboardOwnerHandle(self) -> int:
		mainFrame = gui.mainFrame
		if mainFrame is None:
			raise RuntimeError(_MAIN_FRAME_UNAVAILABLE)
		return int(mainFrame.GetHandle())

	def _snapshotFromItem(self, item: ClipboardItem) -> ClipboardSnapshot:
		"""Convert one stored entry after validating its immutable PNG payload."""
		if item.imageData is not None:
			imageInfo = getPngImageInfo(item.imageData) if len(item.imageData) <= MAX_IMAGE_BYTES else None
			if (
				imageInfo is None
				or imageInfo != (item.imageWidth, item.imageHeight, item.imageBitDepth)
				or not isImageSizeSafe(imageInfo[0], imageInfo[1], max(32, imageInfo[2]))
			):
				self._raiseUserStorageError(StorageFormatError())
		contentType = {
			ClipboardItemType.PLAIN_TEXT: ClipboardContentType.TEXT,
			ClipboardItemType.FORMATTED_TEXT: ClipboardContentType.FORMATTED_TEXT,
			ClipboardItemType.TEXT_AND_IMAGE: ClipboardContentType.TEXT_AND_IMAGE,
			ClipboardItemType.IMAGE: ClipboardContentType.IMAGE,
			ClipboardItemType.FILES: ClipboardContentType.FILES,
		}[item.contentType]
		return ClipboardSnapshot(
			contentType,
			text=item.text,
			html=item.html,
			rtf=item.rtf,
			imageFormat="PNG" if item.imageData is not None else None,
			imageData=item.imageData,
			imageWidth=item.imageWidth or 0,
			imageHeight=item.imageHeight or 0,
			imageBitDepth=item.imageBitDepth or 0,
			files=item.files,
			canUpload=item.canUpload,
		)

	def _confirmClearClipboard(self) -> bool:
		"""Ask whether empty manager text should clear the system clipboard."""
		dialog = wx.MessageDialog(
			self.manager or gui.mainFrame,
			# Translators: Confirmation before clearing the system clipboard from an empty editor.
			_("The editor is empty. Clear the system clipboard?"),
			# Translators: Title of the clear clipboard confirmation dialog.
			_("Clear Clipboard"),
			wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
		)
		try:
			return gui.displayDialogAsModal(dialog) == wx.ID_YES
		finally:
			dialog.Destroy()

	def _playLineNavigationCue(self, atBoundary: bool) -> None:
		if atBoundary:
			playBoundary()
		elif self._contentType == ClipboardContentType.FILES:
			playNonPlainText()

	def _playInlineNavigationCue(self, result: NavigationResult) -> None:
		if result.isAtBoundary:
			playBoundary()
		elif result.hasCrossedLine:
			playLineBoundary()

	def _ensureCurrentContentIsNavigable(self) -> bool:
		"""Report why line, word, or character navigation is unavailable."""
		if self._contentType in (
			ClipboardContentType.TEXT,
			ClipboardContentType.FORMATTED_TEXT,
			ClipboardContentType.TEXT_AND_IMAGE,
			ClipboardContentType.FILES,
		):
			return True
		playBoundary()
		if self._contentType == ClipboardContentType.IMAGE:
			# Translators: Message shown when text navigation is attempted on an image.
			ui.message(_("Image content cannot be browsed as text"))
		else:
			ui.message(self._summary)
		return False

	def _speakTextInfo(self, info: textInfos.TextInfo, unit: str) -> None:
		braille.handler.message(info.text)
		speech.speakTextInfo(info, unit=unit, reason=controlTypes.OutputReason.CARET)

	def _reportTextInfo(self, info: textInfos.TextInfo, unit: str, repeatCount: int) -> None:
		"""Apply NVDA review-command repeat semantics to a text range."""
		if repeatCount == 0:
			self._speakTextInfo(info, unit)
		else:
			braille.handler.message(info.text)
			speech.spellTextInfo(info, useCharacterDescriptions=repeatCount > 1)

	def _reportCharacterCodePoints(self, text: str) -> None:
		"""Speak decimal and hexadecimal values for current characters."""
		codePoints = [ord(character) for character in text]
		if not codePoints:
			self._speakTextInfo(self.navigator.getCurrentCharacter(), textInfos.UNIT_CHARACTER)
			return
		for codePoint in codePoints:
			speech.speakMessage(f"{codePoint},")
			if len(codePoints) == 1:
				speech.speakSpelling(hex(codePoint))
		braille.handler.message("; ".join(f"{codePoint}, {hex(codePoint)}" for codePoint in codePoints))

	def _moveHistoryItem(self, direction: int) -> None:
		"""Move the global history position and report one concise summary."""
		if direction not in (-1, 1):
			raise ValueError(direction)
		try:
			item, newIndex, itemCount = self.storage.getHistorySummaryAt(self._historyIndex, direction)
		except StorageError as error:
			self._raiseUserStorageError(error)
		if item is None:
			self._reportEmptyHistory()
			return
		self._historyIndex = newIndex
		if item.contentType != ClipboardItemType.PLAIN_TEXT:
			playNonPlainText()
		elif newIndex in (0, itemCount - 1):
			playBoundary()
		# Translators: Numbered clipboard history summary reported by the global navigation commands.
		ui.message(
			_("{index}. {summary}").format(
				index=newIndex + 1,
				summary=self._formatItemSummary(item),
			),
		)

	def _reportEmptyHistory(self) -> None:
		playBoundary()
		# Translators: Message shown when global clipboard history contains no entries.
		ui.message(_("Clipboard history is empty"))

	def _getCategorySummaries(self, category: CategoryId) -> tuple[ClipboardItemSummary, ...]:
		"""Return metadata-only entries for one manager category."""
		try:
			if self.isHistoryCategory(category):
				return self.storage.history
			assert isinstance(category, str)
			return self.storage.getCategoryItems(category)
		except StorageError as error:
			self._raiseUserStorageError(error)

	def _getStoredItemById(self, category: CategoryId, itemId: int) -> ClipboardItem:
		"""Load one complete entry after validating its stable category membership."""
		try:
			if self.isHistoryCategory(category):
				return self.storage.getHistoryItemById(itemId)
			assert isinstance(category, str)
			return self.storage.getCategoryItemById(category, itemId)
		except StorageError as error:
			self._raiseUserStorageError(error)

	def _getStoredItemContentById(self, category: CategoryId, itemId: int) -> ClipboardItemContent:
		"""Load displayable content by stable identifier without payload BLOBs."""
		try:
			if self.isHistoryCategory(category):
				return self.storage.getHistoryItemContentById(itemId)
			assert isinstance(category, str)
			return self.storage.getCategoryItemContentById(category, itemId)
		except ItemNotFoundError:
			raise
		except StorageError as error:
			self._raiseUserStorageError(error)

	def _formatItemSummary(self, item: ClipboardItemSummary) -> str:
		"""Return a concise content summary without a type prefix."""
		if item.contentType in (
			ClipboardItemType.PLAIN_TEXT,
			ClipboardItemType.FORMATTED_TEXT,
			ClipboardItemType.TEXT_AND_IMAGE,
		):
			if item.textPreview:
				return item.textPreview
			# Translators: Clipboard list summary when text contains no visible characters.
			return _("No visible text")
		if item.contentType == ClipboardItemType.IMAGE:
			return self._formatImageSummary(item)
		names = ", ".join(item.filesPreview)
		if item.fileCount > len(item.filesPreview):
			# Translators: Clipboard file summary when additional file names are omitted.
			return _("{names}, …").format(names=names)
		return names

	def _formatImageSummary(self, item: ClipboardItemSummary) -> str:
		# Translators: Concise image metadata used inside clipboard list labels.
		return _("{width} by {height} pixels, {size}").format(
			width=item.imageWidth or 0,
			height=item.imageHeight or 0,
			size=self._formatDataSize(item.imageByteCount),
		)

	def _formatImageDetails(self, item: ClipboardItemContent) -> str:
		# Translators: Multiline details shown for a stored clipboard image.
		return _(
			"Image\nDimensions: {width} by {height} pixels\nColor depth: {depth} bits\nStored size: {size}",
		).format(
			width=item.imageWidth or 0,
			height=item.imageHeight or 0,
			depth=item.imageBitDepth or 0,
			size=self._formatDataSize(item.imageByteCount),
		)

	def _formatDataSize(self, byteCount: int) -> str:
		"""Return a compact localized binary-data size."""
		if byteCount >= 1024 * 1024 * 1024 * 1024:
			# Translators: Binary data size in terabytes.
			return _("{size:.1f} TB").format(size=byteCount / (1024 * 1024 * 1024 * 1024))
		if byteCount >= 1024 * 1024 * 1024:
			# Translators: Binary data size in gigabytes.
			return _("{size:.1f} GB").format(size=byteCount / (1024 * 1024 * 1024))
		if byteCount >= 1024 * 1024:
			# Translators: Binary data size in megabytes.
			return _("{size:.1f} MB").format(size=byteCount / (1024 * 1024))
		if byteCount >= 1024:
			# Translators: Binary data size in kilobytes.
			return _("{size:.1f} KB").format(size=byteCount / 1024)
		return ngettext(
			# Translators: Binary data size in bytes.
			"{count} byte",
			"{count} bytes",
			byteCount,
		).format(count=byteCount)

	def _formatTextStatisticsSummary(self, summary: str, statistics: TextStatistics) -> str:
		"""Append exact, non-zero Unicode statistics to a positioned text summary."""
		details = [
			ngettext(
				# Translators: Number of user-perceived Unicode characters including whitespace; line breaks are excluded.
				"{count} character (including whitespace)",
				"{count} characters (including whitespace)",
				statistics.characterCount,
			).format(count=statistics.characterCount),
			ngettext(
				# Translators: Number of user-perceived Unicode characters excluding whitespace and line breaks.
				"{count} character (excluding whitespace)",
				"{count} characters (excluding whitespace)",
				statistics.nonWhitespaceCharacterCount,
			).format(count=statistics.nonWhitespaceCharacterCount),
		]
		attributes = []
		if statistics.hanCharacterCount:
			attributes.append(
				ngettext(
					# Translators: Number of Han ideographic code points in clipboard text.
					"{count} Han character",
					"{count} Han characters",
					statistics.hanCharacterCount,
				).format(count=statistics.hanCharacterCount),
			)
		if statistics.punctuationCount:
			attributes.append(
				ngettext(
					# Translators: Number of Unicode punctuation grapheme clusters in clipboard text.
					"{count} punctuation mark",
					"{count} punctuation marks",
					statistics.punctuationCount,
				).format(count=statistics.punctuationCount),
			)
		if statistics.symbolCount:
			attributes.append(
				ngettext(
					# Translators: Number of Unicode symbol grapheme clusters in clipboard text.
					"{count} symbol",
					"{count} symbols",
					statistics.symbolCount,
				).format(count=statistics.symbolCount),
			)
		# Translators: Separator between exact clipboard text statistics.
		detailsText = _(", ").join(details)
		if attributes:
			# Translators: Selected character attributes follow the main, additive text statistics.
			detailsText = _("{details}; including {attributes}").format(
				details=detailsText,
				attributes=_(", ").join(attributes),
			)
		return ngettext(
			# Translators: Total explicit lines and exact statistics after the clipboard type and current position.
			"{summary}, {count} line in total; {details}",
			"{summary}, {count} lines in total; {details}",
			statistics.lineCount,
		).format(
			summary=summary,
			count=statistics.lineCount,
			details=detailsText,
		)

	def _formatCurrentSummary(self, snapshot: ClipboardSnapshot) -> str:
		"""Return a concise description of the current system clipboard."""
		if snapshot.contentType == ClipboardContentType.TEXT:
			# Translators: Type-only summary of plain text before exact statistics are available.
			return _("Plain text")
		if snapshot.contentType == ClipboardContentType.FORMATTED_TEXT:
			# Translators: Type-only summary of formatted text before exact statistics are available.
			return _("Formatted text")
		if snapshot.contentType == ClipboardContentType.TEXT_AND_IMAGE:
			# Translators: Type of clipboard content containing both text and an image.
			return self._formatCurrentImageSummary(snapshot, _("Text and image"))
		if snapshot.contentType == ClipboardContentType.FILES:
			itemCount = len(snapshot.files)
			if snapshot.filesWereCut:
				return ngettext(
					# Translators: Number of top-level file system items cut to the clipboard.
					"Cut files: {count} item",
					"Cut files: {count} items",
					itemCount,
				).format(count=itemCount)
			if snapshot.filesWereLinked:
				return ngettext(
					# Translators: Number of top-level file system items linked from the clipboard.
					"Linked files: {count} item",
					"Linked files: {count} items",
					itemCount,
				).format(count=itemCount)
			return ngettext(
				# Translators: Number of top-level file system items copied to the clipboard.
				"Copied files: {count} item",
				"Copied files: {count} items",
				itemCount,
			).format(count=itemCount)
		if snapshot.contentType == ClipboardContentType.IMAGE:
			# Translators: Type of image-only clipboard content.
			return self._formatCurrentImageSummary(snapshot, _("Image"))
		if snapshot.contentType == ClipboardContentType.PROTECTED:
			# Translators: Summary for clipboard content excluded from monitoring by its source application.
			return _("Protected clipboard content is not available")
		if snapshot.contentType == ClipboardContentType.EMPTY:
			# Translators: Summary used when the system clipboard is empty.
			return _("Clipboard is empty")
		# Translators: Summary used for clipboard data this add-on cannot navigate.
		return _("Unsupported clipboard content")

	def _formatCurrentImageSummary(self, snapshot: ClipboardSnapshot, contentType: str) -> str:
		"""Return image metadata and trustworthy pixel properties for the current clipboard."""
		if not snapshot.imageWidth or not snapshot.imageHeight:
			return contentType
		if snapshot.imageWidth == snapshot.imageHeight:
			# Translators: Orientation of an image whose width and height are equal.
			orientation = _("square")
		elif snapshot.imageWidth > snapshot.imageHeight:
			# Translators: Orientation of an image wider than it is tall.
			orientation = _("landscape")
		else:
			# Translators: Orientation of an image taller than it is wide.
			orientation = _("portrait")
		# Translators: Clipboard image type, dimensions, and orientation.
		summary = _("{summary}: {width} by {height} pixels, {orientation}").format(
			summary=contentType,
			width=snapshot.imageWidth,
			height=snapshot.imageHeight,
			orientation=orientation,
		)
		if snapshot.imageTransparentPercentage == 100:
			# Translators: Exact image property indicating that every pixel is fully transparent.
			return _("{summary}; fully transparent").format(summary=summary)
		if snapshot.imageColor is not None and snapshot.imageColorPercentage == 100:
			# Translators: Exact image property indicating that every pixel has the same color.
			return _("{summary}; solid {color}").format(
				summary=summary,
				color=_CANONICAL_SOLID_COLOR_NAMES.get(snapshot.imageColor)
				or colors.RGB(*snapshot.imageColor).name,
			)
		if snapshot.imageTransparentPercentage:
			# Translators: Image summary followed by the percentage of fully transparent pixels.
			return _("{summary}; fully transparent pixels: {percentage}%").format(
				summary=summary,
				percentage=f"{snapshot.imageTransparentPercentage:g}",
			)
		if snapshot.imageColor is not None and snapshot.imageColorPercentage:
			# Translators: Image summary followed by the percentage of black or white pixels.
			return _("{summary}; {color} pixels: {percentage}%").format(
				summary=summary,
				color=_CANONICAL_SOLID_COLOR_NAMES.get(snapshot.imageColor)
				or colors.RGB(*snapshot.imageColor).name,
				percentage=f"{snapshot.imageColorPercentage:g}",
			)
		return summary

	def _formatRestoreConfirmation(self, item: ClipboardItem) -> str:
		"""Return a type-specific confirmation after restoring a stored entry."""
		if item.contentType == ClipboardItemType.FILES:
			return ngettext(
				# Translators: Confirmation after restoring one or multiple files to the clipboard.
				"{count} file restored to the clipboard",
				"{count} files restored to the clipboard",
				len(item.files),
			).format(count=len(item.files))
		if item.contentType == ClipboardItemType.IMAGE:
			# Translators: Confirmation after restoring an image to the clipboard.
			return _("Image restored to the clipboard")
		if item.contentType == ClipboardItemType.TEXT_AND_IMAGE:
			# Translators: Confirmation after restoring mixed text and image content.
			return _("Text and image restored to the clipboard")
		# Translators: Confirmation after restoring plain or formatted text.
		return _("Text restored to the clipboard")

	def _formatFileNavigationText(self, files: tuple[str, ...]) -> str:
		"""Return one navigable line for each file path on the clipboard."""
		lines: list[str] = []
		for index, filePath in enumerate(files, start=1):
			name = Path(filePath).name or filePath
			# Translators: Navigable clipboard file entry with name, position, and full path.
			line = _("{name}, item {index} of {count}, {path}").format(
				name=name,
				index=index,
				count=len(files),
				path=filePath,
			)
			lines.append(line)
		# Translators: Summary used when a file-list clipboard format contains no paths.
		return "\n".join(lines) or _("No files in clipboard")

	def _formatItemError(self, error: Exception) -> str:
		"""Return a translated description for a storage operation error."""
		if isinstance(error, CategoryNameError):
			# Translators: Error shown when a clipboard category name is empty.
			return _("The category name cannot be empty")
		if isinstance(error, ReservedCategoryNameError):
			# Translators: Error shown when the built-in history name is used for a category.
			return _("That category name is reserved")
		if isinstance(error, CategoryExistsError):
			# Translators: Error shown when a clipboard category already exists.
			return _("A category with that name already exists")
		if isinstance(error, CategoryNotFoundError):
			# Translators: Error shown when a clipboard category no longer exists.
			return _("The selected category no longer exists")
		if isinstance(error, CategoryNotEmptyError):
			# Translators: Error shown when deletion of a non-empty category is attempted.
			return _("Delete all entries before deleting this category")
		if isinstance(error, ImageStorageLimitError):
			# Translators: Error shown when a collected image cannot fit in clipboard storage.
			return _("The image cannot fit in clipboard history storage")
		if isinstance(error, (InvalidItemError, ItemNotFoundError, StorageFormatError)):
			# Translators: Error shown when stored clipboard entry data is invalid or missing.
			return _("The selected clipboard entry is no longer available")
		if isinstance(error, ValueError):
			# Translators: Error shown for an invalid clipboard category operation.
			return _("The requested category operation is not valid")
		# Translators: Generic clipboard storage operation error.
		return _("Could not update clipboard storage")

	def _raiseUserStorageError(self, error: Exception) -> Never:
		raise ValueError(self._formatItemError(error)) from error

	def _refreshManager(self) -> None:
		manager = self.manager
		if manager is not None:
			try:
				wx.CallAfter(self._refreshManagerIfCurrent, manager)
			except RuntimeError:
				if self._isStarted:
					log.debugWarning("Could not schedule a clipboard manager refresh.", exc_info=True)

	def _refreshManagerIfCurrent(self, manager: ClipboardManagerFrame) -> None:
		"""Refresh a queued manager only while it still belongs to this controller."""
		if not self._isStarted or self.manager is not manager:
			return
		try:
			isBeingDeleted = manager.IsBeingDeleted()
		except RuntimeError:
			isBeingDeleted = True
		if isBeingDeleted:
			self.manager = None
		elif manager.IsShown():
			manager.refreshFromController()

	def _clearCloudDialog(self) -> None:
		self.cloudDialog = None

	def _clearOneDriveDialog(self, dialog: OneDriveSyncDialog) -> None:
		"""Clear the controller's OneDrive dialog reference after destruction."""
		if self.oneDriveDialog is dialog:
			self.oneDriveDialog = None

	def _getCurrentCloudUploadText(self) -> str:
		"""Return current allowed clipboard text for an explicit cloud upload."""
		text = self._text
		canUpload = self._canUpload
		if self.monitor.getSequenceNumber() != self._lastAppliedSequenceNumber:
			snapshot = self.monitor.readNow()
			if snapshot.contentType == ClipboardContentType.ERROR:
				# Translators: Error shown when current clipboard upload policy cannot be checked.
				raise RuntimeError(_("Could not verify the current clipboard for upload"))
			text = snapshot.text
			canUpload = snapshot.canUpload
		if not canUpload:
			# Translators: Message shown when an application forbids cloud upload of clipboard content.
			raise ValueError(_("The current clipboard content is protected from cloud upload"))
		return text

	def _onCloudStateChanged(self) -> None:
		if self.cloudSync is None:
			return
		if self.cloudDialog is not None:
			self.cloudDialog.refreshFromManager()
		if self.manager is not None:
			self.manager.refreshCloudMenuState()

	def _onOneDriveStateChanged(self) -> None:
		"""Refresh the OneDrive dialog after account or synchronization changes."""
		if self.manager is not None:
			self.manager.refreshCloudMenuState()
		dialog = self.oneDriveDialog
		if dialog is None:
			return
		try:
			dialog.refreshFromManager()
		except RuntimeError:
			self._clearOneDriveDialog(dialog)

	def _onOneDriveDataChanged(self) -> None:
		"""Reset history navigation and refresh the manager after a remote merge."""
		if not self._isStarted:
			return
		self._historyIndex = 0
		self._refreshManager()

	def _getFocusObject(self) -> object | None:
		try:
			return api.getFocusObject()
		except Exception:
			log.debugWarning("Failed to identify keyboard focus.", exc_info=True)
			return None

	def _isSameFocus(self, expectedFocus: object) -> bool:
		"""Return whether focus still refers to the same NVDA object with no pending focus event."""
		if eventHandler.isPendingEvents("gainFocus"):
			return False
		try:
			currentFocus = api.getFocusObject()
			return currentFocus is not None and currentFocus == expectedFocus
		except Exception:
			log.debugWarning("Failed to compare keyboard focus.", exc_info=True)
			return False

	def _onCloudFetchDone(self, success: bool, message: str) -> None:
		if success:
			return
		self._cloudFetchInProgress = False
		if message:
			ui.message(message)

	def _abandonCloudFetch(self) -> None:
		"""Release the receive guard when wx rejects a worker completion."""
		self._cloudFetchInProgress = False

	def _onTiantanReceiveDone(self, success: bool, message: str) -> None:
		"""Finish a menu-initiated Tiantan Cloud Clipboard receive and announce its result."""
		self._cloudFetchInProgress = False
		if message:
			ui.message(message)

	def _sendCloudPasteGesture(
		self,
		expectedFocus: object,
		expectedSequenceNumber: int,
		expectedText: str,
		triggerKeyCodes: frozenset[int],
		keyReleaseDeadline: float,
	) -> None:
		"""Send Control+V after cloud text has been left on the system clipboard."""
		retryScheduled = False
		try:
			if not self._isStarted:
				return
			if not self._isSameFocus(expectedFocus):
				# Translators: Cloud paste cancellation because focus changed before pasting.
				ui.message(_("Focus changed. Tiantan Cloud Clipboard paste was cancelled."))
				return
			if not expectedSequenceNumber or self.monitor.getSequenceNumber() != expectedSequenceNumber:
				# Translators: Cloud paste cancellation because another application changed the clipboard.
				ui.message(_("Clipboard changed. Tiantan Cloud Clipboard paste was cancelled."))
				return
			keyReleaseState = _waitForTriggerKeysReleased(
				triggerKeyCodes,
				keyReleaseDeadline,
				lambda: self._sendCloudPasteGesture(
					expectedFocus,
					expectedSequenceNumber,
					expectedText,
					triggerKeyCodes,
					keyReleaseDeadline,
				),
			)
			if keyReleaseState is None:
				retryScheduled = True
				return
			if not keyReleaseState:
				# Translators: Message shown when a Tiantan Cloud Clipboard paste shortcut remains held until timeout.
				ui.message(
					_(
						"The keyboard shortcut was not released. Tiantan Cloud Clipboard text was left on the clipboard without pasting.",
					),
				)
				return
			snapshot = self.monitor.readNow()
			if (
				snapshot.contentType != ClipboardContentType.TEXT
				or snapshot.sequenceNumber != expectedSequenceNumber
				or snapshot.text != expectedText
				or self.monitor.getSequenceNumber() != expectedSequenceNumber
			):
				# Translators: Cloud paste cancellation because another application changed the clipboard.
				ui.message(_("Clipboard changed. Tiantan Cloud Clipboard paste was cancelled."))
				return
			KeyboardInputGesture.fromName("control+v").send()
			# Translators: Confirmation after downloaded cloud text is pasted.
			ui.message(_("Pasted from Tiantan Cloud Clipboard"))
		except Exception:
			log.exception("Failed to send the cloud paste gesture.")
			# Translators: Error shown when the cloud paste keyboard gesture fails.
			ui.message(_("Tiantan Cloud Clipboard paste failed"))
		finally:
			if not retryScheduled:
				self._cloudFetchInProgress = False

	def _getSelectionText(self) -> str | None:
		"""Return selected text from the focused object or tree interceptor."""
		obj = api.getFocusObject()
		treeInterceptor = getattr(obj, "treeInterceptor", None)
		if (
			treeInterceptor is not None
			and hasattr(treeInterceptor, "TextInfo")
			and not treeInterceptor.passThrough
		):
			obj = treeInterceptor
		try:
			info = obj.makeTextInfo(textInfos.POSITION_SELECTION)
		except (RuntimeError, NotImplementedError):
			return None
		if info is None or info.isCollapsed:
			return None
		return info.text
