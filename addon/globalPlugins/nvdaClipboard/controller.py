# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Coordinate clipboard monitoring, navigation, history, categories, and cloud actions."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from typing import TYPE_CHECKING, Literal, Never

import addonHandler
import api
import braille
import controlTypes
from core import callLater
import eventHandler
import gui
from keyboardHandler import KeyboardInputGesture
from logHandler import log
import speech
from speech import CHUNK_SEPARATOR
import textInfos
import ui
import wx

from .clipboardMonitor import (
	ClipboardContentType,
	ClipboardMonitor,
	ClipboardSequenceChangedError,
	ClipboardSnapshot,
	ClipboardWriteError,
)
from .configuration import getShowClipboardContentAsPlainText
from .clipboardData import (
	MAX_IMAGE_BYTES,
	getPngImageInfo,
	isImageSizeSafe,
	normalizeUnicodeText,
)
from .fileSize import FileSizeCalculation
from .cloudSync import TiantanSyncManager, TiantanWriteError, getTiantanSupportState
from .navigation import ClipboardNavigator, NavigationResult
from .oneDriveSync import OneDriveSyncManager
from .paste import (
	PASTE_KEY_RELEASE_POLL_INTERVAL_MS,
	PASTE_KEY_RELEASE_TIMEOUT_SECONDS,
	ClipboardChangeSource,
	ClipboardWriteFailedError,
	TemporaryPaster,
	preparePngDib,
	waitForTriggerKeysReleased,
)
from .search import matchesSearchKeywords, normalizeSearchText
from .cues import playBoundary, playCopy, playLineBoundary, playNonPlainText
from .historyWriter import (
	IMAGE_CONTENT_TYPES,
	HistoryWriter,
	HistoryWriteResult,
	clipboardItemFromSnapshot,
	normalizeImageSnapshot,
)
from .images import (
	ScreenCurtainCaptureUnavailableError,
	captureNavigatorObjectPng,
	savePngImage,
)
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
from .storageModels import (
	TEXT_ITEM_TYPES,
	ClipboardItem,
	ClipboardItemContent,
	ClipboardItemSummary,
	ClipboardItemType,
)
from .summary.formatting import (
	formatCurrentSummary,
	formatFileNavigationText,
	formatFileSizeSummary,
	formatImageDetails,
	formatItemSummary,
	formatPutOnClipboardConfirmation,
	formatTextStatisticsReport,
	getTextOrCharacterCount,
)
from .textStats import TextStatisticsCalculation

if TYPE_CHECKING:
	from .cloudDialog import TiantanDialog
	from .manager import ClipboardManagerFrame
	from .oneDriveDialog import OneDriveSyncDialog


addonHandler.initTranslation()

_SUMMARY_REPORT_TIMEOUT_MS = 2000

# Translators: Error shown when an add-on window cannot open because NVDA's main window is unavailable.
_MAIN_FRAME_UNAVAILABLE = _("NVDA's main window is unavailable")

# Translators: Name of the built-in category containing recent clipboard text.
_HISTORY_CATEGORY_NAME = _("Clipboard history")

# Translators: Message shown when Tiantan Cloud Clipboard is disabled.
_TIANTAN_DISABLED_MESSAGE = _(
	"Tiantan Cloud Clipboard is disabled. Enable it in settings, then restart NVDA.",
)


class _BuiltinCategory(Enum):
	"""Identify built-in categories without using translated display text as data."""

	HISTORY = auto()


type CategoryId = str | _BuiltinCategory
# Background calculation backing the summary of the current clipboard content.
type _SummaryCalculation = FileSizeCalculation | TextStatisticsCalculation

_HISTORY_CATEGORY_ID = _BuiltinCategory.HISTORY

# Content types whose snapshots or stored items carry navigable clipboard text.
_TEXT_CONTENT_TYPES = frozenset(
	{ClipboardContentType.TEXT, ClipboardContentType.FORMATTED_TEXT, ClipboardContentType.TEXT_AND_IMAGE},
)


@contextmanager
def _popup(mainFrame: gui.MainFrame | None) -> Iterator[None]:
	"""Bracket showing an add-on window with NVDA's main frame popup notifications, if available."""
	if mainFrame is not None:
		mainFrame.prePopup()
	try:
		yield
	finally:
		if mainFrame is not None:
			mainFrame.postPopup()


def _getLastSpokenText() -> str | None:
	"""Return the text from NVDA's most recent speech sequence."""
	# NVDA 2026.3 private record: (speechSequence, symbolLevel).
	try:
		lastSpeech = getattr(speech.speech, "_lastSpeech")
	except AttributeError:
		log.debugWarning("NVDA's last speech record is unavailable.", exc_info=True)
		return None
	if lastSpeech is None:
		return None
	return CHUNK_SEPARATOR.join(item for item in lastSpeech[0] if isinstance(item, str))


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
	removedCount: int
	replacementIds: dict[int, int]


@dataclass(frozen=True, slots=True)
class _PreparedPngClipboardWrite:
	"""Retain one worker-prepared PNG clipboard write until its main-thread commit."""

	snapshot: ClipboardSnapshot
	source: ClipboardChangeSource
	expectedSequenceNumber: int
	confirmation: str
	dibData: bytes


def _isMissingFilePath(filePath: str) -> bool:
	"""Return whether a stored file path cannot currently be found."""
	try:
		return not Path(filePath).exists()
	except OSError:
		return True


class ClipboardController:
	"""Own all non-script behavior for the NVDA Clipboard global plugin."""

	def __init__(self, *, tiantanEnabled: bool = False) -> None:
		self.storage = ClipboardStorage(reservedCategoryNames=(_HISTORY_CATEGORY_NAME,))
		self._tiantanUnavailableMessage = ""
		try:
			self.navigator = ClipboardNavigator()
			self.tiantanSync = None
			if tiantanEnabled:
				tiantanSupportState = getTiantanSupportState()
				self._tiantanUnavailableMessage = tiantanSupportState.statusMessage
				if tiantanSupportState.sdk is not None:
					self.tiantanSync = TiantanSyncManager(
						tiantanSupportState.sdk,
						onStateChanged=self._onTiantanStateChanged,
					)
			self.oneDriveSync = OneDriveSyncManager(
				self.storage,
				onStateChanged=self._onOneDriveStateChanged,
				onDataChanged=self._onOneDriveDataChanged,
			)
			self.monitor = ClipboardMonitor(onSnapshot=self._onClipboardSnapshot)
			self._historyWriter = HistoryWriter(
				self.storage,
				isStarted=lambda: self._isStarted,
				onItemStored=self._onHistoryItemStored,
			)
		except Exception:
			oneDriveSync = getattr(self, "oneDriveSync", None)
			if oneDriveSync is not None:
				oneDriveSync.terminate()
			tiantanSync = getattr(self, "tiantanSync", None)
			if tiantanSync is not None:
				tiantanSync.terminate()
			self.storage.close()
			raise
		self.manager: ClipboardManagerFrame | None = None
		self.tiantanDialog: TiantanDialog | None = None
		self.oneDriveDialog: OneDriveSyncDialog | None = None
		self._contentType = ClipboardContentType.EMPTY
		self._text = ""
		self._canUpload = False
		self._summaryCalculation: _SummaryCalculation | None = None
		self._pendingSummaryReport: _SummaryCalculation | None = None
		self._missingFileCleanupInProgress = False
		# Translators: Summary used when the system clipboard is empty.
		self._summary = _("Clipboard is empty")
		self.navigator.setText(self._summary)
		self._storedItemCategory: CategoryId = _HISTORY_CATEGORY_ID
		self._storedItemIndex = 0
		self._pendingWrites: set[int] = set()
		# Whether the pending writes were applied locally, rather than only registered as our own.
		self._pendingWritesWereApplied = True
		self._pendingPngClipboardWrite: Future[_PreparedPngClipboardWrite] | None = None
		self._awaitingInitialSnapshot = False
		self._lastAppliedSequenceNumber = 0
		self._tiantanFetchInProgress = False
		self._paster = TemporaryPaster(
			monitor=self.monitor,
			storage=self.storage,
			worker=self._historyWriter,
			isStarted=lambda: self._isStarted,
			getLastAppliedSequenceNumber=lambda: self._lastAppliedSequenceNumber,
			clearPendingWrites=lambda: self._pendingWrites.clear(),
			captureOriginalClipboard=self._captureOriginalClipboardForTemporaryPaste,
			writeSnapshot=self._writeSnapshot,
			clearClipboard=self._clearClipboard,
			applyLeftoverClipboardContent=self._applyLeftoverClipboardContent,
			distrustRegisteredClipboardState=self._distrustRegisteredClipboardState,
			getClipboardOwnerHandle=self._getClipboardOwnerHandle,
			isSameFocus=self._isSameFocus,
			snapshotFromItem=self._snapshotFromItem,
		)
		self._isStarted = False

	def start(self) -> None:
		"""Start cloud state detection and clipboard monitoring."""
		if self._isStarted:
			return
		self._isStarted = True
		try:
			tiantanSync = self.tiantanSync
			if tiantanSync is not None:
				tiantanSync.initialize()
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
		self._cancelPendingPngClipboardWrite()
		self._paster.cancel()
		self._tiantanFetchInProgress = False
		self._pendingSummaryReport = None
		if self._summaryCalculation is not None:
			self._summaryCalculation.cancel()
			self._summaryCalculation = None
		try:
			self.monitor.stop()
		except Exception:
			log.debugWarning("Failed to stop clipboard monitoring.", exc_info=True)
		tiantanSync = self.tiantanSync
		if tiantanSync is not None:
			tiantanSync.terminate()
		if self.tiantanDialog is not None:
			self.tiantanDialog.Destroy()
			self.tiantanDialog = None
		oneDriveDialog = self.oneDriveDialog
		self.oneDriveDialog = None
		if oneDriveDialog is not None:
			oneDriveDialog.Destroy()
		self.oneDriveSync.terminate()
		manager = self.manager
		self.manager = None
		if manager is not None:
			manager.terminate()
		self._historyWriter.shutdown()
		self.storage.close()

	def showManager(self) -> None:
		"""Create or show the single non-modal clipboard manager window."""
		mainFrame = gui.mainFrame
		if mainFrame is None:
			raise RuntimeError(_MAIN_FRAME_UNAVAILABLE)
		with _popup(mainFrame):
			if self.manager is None:
				from .manager import ClipboardManagerFrame

				self.manager = ClipboardManagerFrame(mainFrame, self)
			self.manager.showManager(self._resolveStoredItemCategory(), self._storedItemIndex)

	def _showSingletonDialog(
		self,
		attributeName: Literal["tiantanDialog", "oneDriveDialog"],
		parent: wx.Window | None,
		createDialog: Callable[[wx.Window], wx.Dialog],
	) -> None:
		"""Raise the dialog stored in ``attributeName``, or create, store, and show a new one."""
		existingDialog: wx.Dialog | None = getattr(self, attributeName)
		if existingDialog is not None:
			try:
				existingDialog.Raise()
				existingDialog.SetFocus()
				return
			except RuntimeError:
				setattr(self, attributeName, None)
		mainFrame = gui.mainFrame
		dialogParent = parent if parent is not None else mainFrame
		if dialogParent is None:
			raise RuntimeError(_MAIN_FRAME_UNAVAILABLE)
		dialog = createDialog(dialogParent)
		setattr(self, attributeName, dialog)
		with _popup(mainFrame):
			dialog.Show()
			dialog.Raise()

	def showTiantanDialog(self, parent: wx.Window | None = None) -> None:
		"""Create or raise the Tiantan Cloud Clipboard account dialog."""
		tiantanSync = self._getAvailableTiantanSync()
		if tiantanSync is None:
			return
		from .cloudDialog import TiantanDialog

		self._showSingletonDialog(
			"tiantanDialog",
			parent,
			lambda dialogParent: TiantanDialog(dialogParent, tiantanSync, self._clearTiantanDialog),
		)

	def showOneDriveDialog(self, parent: wx.Window | None = None) -> None:
		"""Create or raise the OneDrive history synchronization dialog."""
		from .oneDriveDialog import OneDriveSyncDialog

		self._showSingletonDialog(
			"oneDriveDialog",
			parent,
			lambda dialogParent: OneDriveSyncDialog(
				dialogParent, self.oneDriveSync, self._clearOneDriveDialog
			),
		)

	def _getAvailableTiantanSync(self) -> TiantanSyncManager | None:
		"""Return the Tiantan Cloud Clipboard manager, or report why it is unavailable."""
		tiantanSync = self.tiantanSync
		if tiantanSync is None:
			ui.message(self._tiantanUnavailableMessage or _TIANTAN_DISABLED_MESSAGE)
		return tiantanSync

	def _getIdleTiantanSync(self) -> TiantanSyncManager | None:
		"""Return the Tiantan Cloud Clipboard manager only when no receive is already running."""
		tiantanSync = self._getAvailableTiantanSync()
		if tiantanSync is not None and self._tiantanFetchInProgress:
			# Translators: Message shown when another Tiantan Cloud Clipboard receive is still running.
			ui.message(_("A Tiantan Cloud Clipboard receive is already in progress"))
			return None
		return tiantanSync

	def pasteTiantanText(self, triggerKeyCodes: frozenset[int]) -> None:
		"""Fetch cloud text, leave it on the clipboard, and send the paste gesture."""
		tiantanSync = self._getIdleTiantanSync()
		if tiantanSync is None:
			return
		focusObject = self._getFocusObject()
		if focusObject is None:
			# Translators: Cloud paste cancellation because keyboard focus could not be identified safely.
			ui.message(
				_("Keyboard focus could not be identified. Tiantan Cloud Clipboard paste was cancelled."),
			)
			return
		self._tiantanFetchInProgress = True

		def writeFetchedText(text: str) -> None:
			"""Write downloaded text only while keyboard focus remains unchanged."""
			if not self._isSameFocus(focusObject):
				# Translators: Cloud paste cancellation because focus changed while downloading.
				raise TiantanWriteError(
					_("Focus changed. Tiantan Cloud Clipboard paste was cancelled."),
				)
			sequenceNumber = self._writeClipboardText(text, ClipboardChangeSource.NO_RECORD)
			callLater(
				PASTE_KEY_RELEASE_POLL_INTERVAL_MS,
				self._sendTiantanPasteGesture,
				focusObject,
				sequenceNumber,
				text,
				triggerKeyCodes,
				monotonic() + PASTE_KEY_RELEASE_TIMEOUT_SECONDS,
			)

		tiantanSync.fetchToClipboard(
			writeFetchedText,
			onDone=self._onTiantanFetchDone,
			onDispatchFailed=self._abandonTiantanFetch,
		)

	def sendToTiantan(self) -> None:
		"""Send the current allowed clipboard text to Tiantan Cloud Clipboard."""
		tiantanSync = self._getAvailableTiantanSync()
		if tiantanSync is None:
			return
		tiantanSync.uploadCurrentText(self._getCurrentTiantanUploadText())

	def receiveFromTiantan(self) -> None:
		"""Receive Tiantan Cloud Clipboard text into the system clipboard without pasting it."""
		tiantanSync = self._getIdleTiantanSync()
		if tiantanSync is None:
			return
		self._tiantanFetchInProgress = True

		def writeFetchedText(text: str) -> None:
			"""Write received Tiantan Cloud Clipboard text without recording or resending it."""
			self._writeClipboardText(text, ClipboardChangeSource.NO_RECORD)

		tiantanSync.fetchToClipboard(
			writeFetchedText,
			onDone=self._onTiantanReceiveDone,
			onDispatchFailed=self._abandonTiantanFetch,
		)

	def syncOneDrive(self) -> None:
		"""Start an immediate OneDrive synchronization and announce its result."""
		self.oneDriveSync.syncNow(
			onDone=lambda _success, message: ui.message(message) if message else None,
		)

	def reportClipboardSummary(self) -> None:
		"""Report a concise summary of the current clipboard content."""
		calculation = self._summaryCalculation
		if calculation is None:
			ui.message(self._summary)
			return
		if calculation.start(onComplete=self._onSummaryCalculationComplete):
			self._pendingSummaryReport = calculation
			try:
				callLater(
					_SUMMARY_REPORT_TIMEOUT_MS,
					self._reportPendingSummary,
					calculation,
				)
			except Exception:
				self._pendingSummaryReport = None
				self._reportCalculatedSummary(calculation)
				return
			if speech.getState().speechMode == speech.SpeechMode.onDemand:
				self._reportCalculatedSummary(calculation)
			return
		self._pendingSummaryReport = None
		self._reportCalculatedSummary(calculation)

	def showClipboardContent(self) -> None:
		"""Show the current clipboard content in the configured browseable format."""
		from .clipboardViewer import showSnapshot

		snapshot = self.monitor.readNow()
		if snapshot.contentType == ClipboardContentType.ERROR:
			# Translators: Error shown when the current clipboard cannot be read for the viewer.
			raise RuntimeError(_("Could not read the clipboard content"))
		showSnapshot(
			snapshot,
			# Translators: Title of the browse mode window showing clipboard content.
			_("Clipboard content"),
			formatCurrentSummary(snapshot),
			showAsPlainText=getShowClipboardContentAsPlainText(),
		)

	def _onSummaryCalculationComplete(self, calculation: _SummaryCalculation) -> None:
		"""Queue the response for a pending summary request on NVDA's main thread."""
		callLater(0, self._reportPendingSummary, calculation)

	def _reportPendingSummary(self, calculation: _SummaryCalculation) -> None:
		"""Report one requested summary while its calculation is still current."""
		if self._pendingSummaryReport is not calculation:
			return
		self._pendingSummaryReport = None
		if not self._isStarted or self._summaryCalculation is not calculation:
			return
		self._reportCalculatedSummary(calculation)

	def _reportCalculatedSummary(self, calculation: _SummaryCalculation) -> None:
		"""Report the summary matching the kind of the current clipboard calculation."""
		if isinstance(calculation, FileSizeCalculation):
			self._reportFileSizeSummary(calculation)
		else:
			self._reportTextStatisticsSummary(calculation)

	def _reportFileSizeSummary(self, calculation: FileSizeCalculation) -> None:
		"""Report the latest size and item counts for the current clipboard files."""
		ui.message(formatFileSizeSummary(self._summary, calculation.getProgress()))

	def _reportTextStatisticsSummary(self, calculation: TextStatisticsCalculation) -> None:
		"""Report exact completed text statistics or the current calculation state."""
		progress = calculation.getProgress()
		lineNumber, columnNumber = self.navigator.getLineAndColumn()
		ui.message(formatTextStatisticsReport(self._summary, lineNumber, columnNumber, progress))

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

	def markClipboardSelectionStart(self) -> None:
		"""Mark the current clipboard navigation position as the selection start."""
		if not self._ensureCurrentContentHasText():
			return
		self.navigator.markSelectionStart()
		# Translators: Indicates the start of clipboard text selected for pasting.
		ui.message(_("Start marked"))

	def markClipboardSelectionEnd(self) -> None:
		"""Mark the selection end and report the inclusive clipboard text range."""
		if not self._ensureCurrentContentHasText():
			return
		selectedText = self.navigator.markSelectionEnd()
		if selectedText is None:
			# Translators: Presented when clipboard selection has no start marker.
			ui.message(_("No start marker set"))
			return
		# Translators: Braille message showing text selected in clipboard navigation.
		selectionMessage = _("%s selected") % getTextOrCharacterCount(selectedText, 512)
		speech.speakTextSelected(selectedText)
		braille.handler.message(selectionMessage)

	def pasteClipboardSelection(self, triggerKeyCodes: frozenset[int]) -> None:
		"""Temporarily paste the marked clipboard text without changing navigation position."""
		if not self._ensureCurrentContentHasText():
			return
		selectedText = self.navigator.getSelectedText()
		selectionOffsets = self.navigator.getSelectionOffsets()
		if not selectedText or selectionOffsets is None:
			# Translators: Message shown when a command requires selected text.
			ui.message(_("No text is selected"))
			return
		self._paster.pasteTextTemporarily(
			selectedText,
			triggerKeyCodes,
			sourceSequenceNumber=self._lastAppliedSequenceNumber,
		)

	def pasteCurrentNavigationTarget(self, triggerKeyCodes: frozenset[int]) -> None:
		"""Paste a complete clipboard selection, or otherwise the current stored item."""
		if self.navigator.getSelectionOffsets() is not None:
			self.pasteClipboardSelection(triggerKeyCodes)
		else:
			self.pasteCurrentStoredItem(triggerKeyCodes)

	def appendLastSpokenText(self) -> None:
		"""Append the most recent NVDA speech text to the clipboard."""
		text = self._requireLastSpokenText()
		self._appendTextToClipboard(text)
		spokenText = getTextOrCharacterCount(text)
		ui.message(
			# Translators: Announced after text is appended to the clipboard.
			_("Appended to clipboard: {text}").format(text=spokenText),
			# Translators: Displayed in braille after text is appended to the clipboard.
			brailleText=_("Appended: {text}").format(text=text),
		)

	def pasteLastSpokenText(self, triggerKeyCodes: frozenset[int]) -> None:
		"""Schedule a temporary paste of the most recent NVDA speech text."""
		self._paster.pasteTextTemporarily(self._requireLastSpokenText(), triggerKeyCodes)

	def appendSelectedText(self) -> None:
		"""Append the focused selection to existing clipboard text."""
		selectedText = self._getSelectionText()
		if not selectedText:
			# Translators: Message shown when append is requested without selected text.
			ui.message(_("No text is selected"))
			return
		self._appendTextToClipboard(selectedText)
		# Translators: Confirmation after selected text is appended to the clipboard.
		ui.message(_("Selected text appended"))

	def copyNavigatorObjectImage(self) -> None:
		"""Copy a screenshot of the current navigator object."""
		try:
			pngData = captureNavigatorObjectPng()
		except ScreenCurtainCaptureUnavailableError:
			# Translators: Message shown when a screenshot is blocked by screen curtain.
			ui.message(_("Disable screen curtain before taking a screenshot"))
			return
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
			ClipboardChangeSource.RECORD,
		)
		# Translators: Confirmation after a navigator object screenshot is copied.
		ui.message(_("Navigator object image copied to the clipboard"))

	def saveCurrentClipboardImage(self) -> None:
		"""Save the current clipboard image using NVDA's main frame as parent."""
		mainFrame = gui.mainFrame
		if mainFrame is None:
			raise RuntimeError(_MAIN_FRAME_UNAVAILABLE)
		with _popup(mainFrame):
			self._saveCurrentClipboardImage(mainFrame)

	def cycleStoredItemCategory(self) -> None:
		"""Select and report the next category of stored clipboard items."""
		self.navigator.setSelectionOffsets(None)
		categories = self.getCategories()
		if self._storedItemCategory in categories:
			categoryIndex = (categories.index(self._storedItemCategory) + 1) % len(categories)
			self._storedItemCategory = categories[categoryIndex]
		else:
			self._storedItemCategory = _HISTORY_CATEGORY_ID
		self._storedItemIndex = 0
		_item, self._storedItemIndex, itemCount = self._getStoredItemSummaryAt(
			self._storedItemCategory,
		)
		self._reportStoredItemCategory(
			self._storedItemCategory,
			itemCount,
		)

	def moveToNextStoredItem(self) -> None:
		"""Move to and report the next stored item in the current category."""
		self._moveStoredItem(1)

	def moveToPreviousStoredItem(self) -> None:
		"""Move to and report the previous stored item in the current category."""
		self._moveStoredItem(-1)

	def putCurrentStoredItemOnClipboard(self) -> None:
		"""Put the globally selected stored item on the system clipboard."""
		category = self._resolveStoredItemCategory()
		item, self._storedItemIndex, _itemCount = self._getStoredItemSummaryAt(category)
		if item is None:
			self._reportEmptyStoredItemCategory(category)
			return
		self.putItemOnClipboard(category, item.itemId)

	def pasteCurrentStoredItem(self, triggerKeyCodes: frozenset[int]) -> None:
		"""Temporarily paste the globally selected stored item without moving from it."""
		focusObject = self._getFocusObject()
		category = self._resolveStoredItemCategory()
		summary, self._storedItemIndex, _itemCount = self._getStoredItemSummaryAt(category)
		if summary is None:
			self._reportEmptyStoredItemCategory(category)
			return
		item = self._getStoredItemById(category, summary.itemId)
		self._paster.pasteSnapshotTemporarily(
			self._snapshotFromItem(item),
			formatItemSummary(summary),
			triggerKeyCodes,
			expectedFocus=focusObject,
		)

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
			(item.itemId, formatItemSummary(item), item.contentType)
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
		with self._userStorageErrors():
			contents = self.storage.iterItemContents(self._storageCategory(category))
			with closing(contents):
				for item in contents:
					if cancelEvent.is_set():
						break
					visibleSummary = visibleSummaries.get(item.itemId)
					if visibleSummary is None:
						continue
					fields = [visibleSummary]
					if item.contentType in TEXT_ITEM_TYPES:
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
		return tuple(matchingKeys)

	def getItemDetails(self, category: CategoryId, itemId: int) -> ClipboardItemDetails:
		"""Load the selected entry only and describe it for the manager content control."""
		item = self._getStoredItemContentById(category, itemId)
		if item.contentType in TEXT_ITEM_TYPES:
			content = item.text
		elif item.contentType == ClipboardItemType.FILES:
			content = "\n".join(item.files)
		else:
			content = formatImageDetails(item)
		return ClipboardItemDetails(
			key=item.itemId,
			kind=item.contentType,
			content=content,
			isEditable=item.contentType in TEXT_ITEM_TYPES,
			hasImage=item.hasImage,
			canUpload=item.canUpload,
		)

	def getCurrentNavigationOffsetForText(self, text: str) -> int | None:
		"""Return the navigation offset only when text is still the current clipboard text."""
		# During a temporary paste, current state keeps the original while the clipboard holds other content.
		if not (
			text
			and not self._paster.hasPendingRestore
			and self._contentType in _TEXT_CONTENT_TYPES
			and text == self._text
			and self._lastAppliedSequenceNumber
			and self.monitor.getSequenceNumber() == self._lastAppliedSequenceNumber
		):
			return None
		return self.navigator.getPosition()

	def replaceClipboardWithText(self, text: str, *, canUpload: bool) -> bool:
		"""Replace or clear the system clipboard from manager editor text."""
		if text:
			self._writeClipboardText(
				text,
				ClipboardChangeSource.MANAGER_TEXT_REPLACEMENT,
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
		text = normalizeUnicodeText(text)
		if self.isHistoryCategory(category):
			# Translators: Error shown when a draft is saved directly to history.
			raise ValueError(_("Clipboard history can only be changed through the system clipboard"))
		if not text:
			# Translators: Error shown when an empty category entry is requested.
			raise ValueError(_("An empty entry cannot be saved"))
		assert isinstance(category, str)
		with self._userStorageErrors():
			self.storage.addCategoryItem(
				category,
				ClipboardItem(ClipboardItemType.PLAIN_TEXT, text=text, canUpload=canUpload),
			)
		if self._storedItemCategory == category:
			self._storedItemIndex = 0
		self.oneDriveSync.notifyLocalChange()
		if notifyManager:
			self._refreshManager()

	def putItemOnClipboard(self, category: CategoryId, itemId: int) -> None:
		"""Put every supported format of one stored item on the clipboard."""
		expectedSequenceNumber = self.monitor.getSequenceNumber()
		self._cancelPendingPngClipboardWrite()
		item = self._getStoredItemById(category, itemId)
		snapshot = self._snapshotFromItem(item)
		source = ClipboardChangeSource.RECORD
		confirmation = formatPutOnClipboardConfirmation(item)
		if snapshot.imageFormat != "PNG":
			try:
				self._writeSnapshot(
					snapshot,
					source,
					expectedSequenceNumber=expectedSequenceNumber,
				)
			except ClipboardSequenceChangedError:
				# Translators: Error shown when another application changes the clipboard before an entry is written.
				ui.message(_("The clipboard changed before it could be updated. Try again."))
				return
			ui.message(confirmation)
			return
		future = self._historyWriter.submit(
			self._preparePngClipboardWrite,
			snapshot,
			source,
			expectedSequenceNumber,
			confirmation,
		)
		self._pendingPngClipboardWrite = future
		future.add_done_callback(self._queuePngClipboardWriteCompletion)

	def _cancelPendingPngClipboardWrite(self) -> None:
		"""Cancel a queued PNG clipboard write and make any running result stale."""
		future = self._pendingPngClipboardWrite
		self._pendingPngClipboardWrite = None
		if future is not None:
			future.cancel()

	@staticmethod
	def _preparePngClipboardWrite(
		snapshot: ClipboardSnapshot,
		source: ClipboardChangeSource,
		expectedSequenceNumber: int,
		confirmation: str,
	) -> _PreparedPngClipboardWrite:
		"""Decode a stored PNG into its DIB fallback on the serial worker."""
		return _PreparedPngClipboardWrite(
			snapshot=snapshot,
			source=source,
			expectedSequenceNumber=expectedSequenceNumber,
			confirmation=confirmation,
			dibData=preparePngDib(snapshot),
		)

	def _queuePngClipboardWriteCompletion(self, future: Future[_PreparedPngClipboardWrite]) -> None:
		"""Queue one prepared PNG clipboard write for main-thread validation and commit."""
		try:
			wx.CallAfter(self._finishPngClipboardWrite, future)
		except RuntimeError:
			if future is self._pendingPngClipboardWrite:
				self._pendingPngClipboardWrite = None
			if self._isStarted:
				log.debugWarning("Could not schedule a prepared PNG clipboard write.", exc_info=True)

	def _finishPngClipboardWrite(self, future: Future[_PreparedPngClipboardWrite]) -> None:
		"""Commit the latest prepared PNG clipboard write without replacing newer clipboard data."""
		if future is not self._pendingPngClipboardWrite:
			return
		self._pendingPngClipboardWrite = None
		if not self._isStarted:
			return
		try:
			prepared = future.result()
		except ValueError as error:
			log.debugWarning(
				"Stored PNG data could not be prepared for writing to the clipboard.",
				exc_info=error,
			)
			# Translators: Error shown when supported formats cannot be written to the clipboard.
			ui.message(_("Could not write to the system clipboard"))
			return
		except Exception as error:
			log.exception("Failed to prepare stored PNG data for writing to the clipboard.", exc_info=error)
			# Translators: Error shown when supported formats cannot be written to the clipboard.
			ui.message(_("Could not write to the system clipboard"))
			return
		if self.monitor.getSequenceNumber() != prepared.expectedSequenceNumber:
			# Translators: Error shown when another application changes the clipboard before an entry is written.
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
			# Translators: Error shown when another application changes the clipboard before an entry is written.
			ui.message(_("The clipboard changed before it could be updated. Try again."))
			return
		except RuntimeError as error:
			log.debugWarning("Could not commit prepared PNG clipboard data.", exc_info=error)
			ui.message(str(error))
			return
		except Exception as error:
			log.exception("Failed to commit prepared PNG clipboard data.", exc_info=error)
			# Translators: Error shown when supported formats cannot be written to the clipboard.
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
		if self._storedItemCategory == targetCategory or (
			isinstance(sourceCategory, str) and self._storedItemCategory == sourceCategory
		):
			self._storedItemIndex = 0
		self.oneDriveSync.notifyLocalChange()

	def deleteItems(self, category: CategoryId, itemIds: tuple[int, ...]) -> None:
		"""Delete stored entries."""
		with self._userStorageErrors():
			self.storage.deleteItemsById(self._storageCategory(category), itemIds)
		self.oneDriveSync.notifyLocalChange()

	def startMissingFileCleanup(
		self,
		category: CategoryId,
		itemIds: tuple[int, ...],
		onComplete: Callable[[MissingFileCleanupResult | Exception], None],
	) -> None:
		"""Start one missing-file cleanup worker unless one is already active."""
		if not self._isStarted or self._missingFileCleanupInProgress:
			return
		self._missingFileCleanupInProgress = True

		def run() -> None:
			try:
				missingPaths = frozenset(
					filePath
					for itemId in dict.fromkeys(itemIds)
					for filePath in self._getStoredItemContentById(category, itemId).files
					if _isMissingFilePath(filePath)
				)
				if not self._isStarted or not missingPaths:
					result: MissingFileCleanupResult | Exception = MissingFileCleanupResult(0, 0, {})
				else:
					with self._userStorageErrors():
						changedCount, removedCount, replacementIds = self.storage.removeMissingFileReferences(
							self._storageCategory(category),
							itemIds,
							missingPaths,
						)
					result = MissingFileCleanupResult(
						changedCount,
						removedCount,
						replacementIds,
					)
			except Exception as error:
				result = error
			try:
				wx.CallAfter(self._finishMissingFileCleanup, onComplete, result)
			except RuntimeError:
				self._missingFileCleanupInProgress = False
				if self._isStarted:
					log.debugWarning("Could not schedule a missing-file cleanup result.", exc_info=True)

		try:
			worker = Thread(
				target=run,
				name="nvdaClipboard.missingFileCleanup",
				daemon=True,
			)
			worker.start()
		except RuntimeError:
			self._missingFileCleanupInProgress = False
			raise

	def _finishMissingFileCleanup(
		self,
		onComplete: Callable[[MissingFileCleanupResult | Exception], None],
		result: MissingFileCleanupResult | Exception,
	) -> None:
		"""Finish one missing-file cleanup on the NVDA thread."""
		self._missingFileCleanupInProgress = False
		if not self._isStarted:
			return
		if isinstance(result, MissingFileCleanupResult) and result.changedCount:
			try:
				self.oneDriveSync.notifyLocalChange()
			except Exception as error:
				result = error
		onComplete(result)

	def createCategory(self, name: str) -> str:
		"""Create a user category and return its stored name."""
		with self._userStorageErrors():
			actualName = self.storage.createCategory(name)
		self.oneDriveSync.notifyLocalChange()
		return actualName

	def renameCategory(self, oldName: str, newName: str) -> str:
		"""Rename a user category and return its stored name."""
		with self._userStorageErrors():
			actualName = self.storage.renameCategory(oldName, newName)
		if self._storedItemCategory == oldName:
			self._storedItemCategory = actualName
		self.oneDriveSync.notifyLocalChange()
		return actualName

	def deleteCategory(self, name: str) -> None:
		"""Delete an empty user category."""
		with self._userStorageErrors():
			self.storage.deleteCategory(name)
		if self._storedItemCategory == name:
			self._storedItemCategory = _HISTORY_CATEGORY_ID
			self._storedItemIndex = 0
		self.oneDriveSync.notifyLocalChange()

	def _saveCurrentClipboardImage(self, parent: wx.Window) -> None:
		"""Save the current clipboard image as a validated PNG file."""
		snapshot = self.monitor.readNow(includeRichFormats=False)
		if snapshot.contentType == ClipboardContentType.ERROR:
			# Translators: Error shown when the current clipboard image cannot be read.
			raise RuntimeError(_("Could not read the clipboard image"))
		if snapshot.contentType not in IMAGE_CONTENT_TYPES:
			# Translators: Message shown when no clipboard image is available to save.
			ui.message(_("There is no clipboard image to save"))
			return
		snapshot, _imageWasTooLarge = normalizeImageSnapshot(snapshot)
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
			if self._pendingWritesWereApplied and snapshot.contentType in IMAGE_CONTENT_TYPES:
				self._summary = formatCurrentSummary(snapshot)
				if snapshot.contentType == ClipboardContentType.IMAGE:
					self.navigator.setText(self._summary)
			return
		if self._paster.matchesTemporaryContent(snapshot):
			self._awaitingInitialSnapshot = False
			self._lastAppliedSequenceNumber = snapshot.sequenceNumber
			return
		source = (
			ClipboardChangeSource.NO_RECORD if self._awaitingInitialSnapshot else ClipboardChangeSource.RECORD
		)
		self._awaitingInitialSnapshot = False
		self._applySnapshot(snapshot, source)

	def _consumePendingWrite(self, snapshot: ClipboardSnapshot) -> bool:
		if snapshot.sequenceNumber in self._pendingWrites:
			self._pendingWrites.discard(snapshot.sequenceNumber)
			return True
		self._pendingWrites.clear()
		return False

	def _applySnapshot(
		self,
		snapshot: ClipboardSnapshot,
		source: ClipboardChangeSource,
		*,
		historyPreviousText: str | None = None,
	) -> None:
		"""Apply current state immediately and queue source-dependent history work."""
		if snapshot.contentType == ClipboardContentType.ERROR:
			return
		shouldRecord = source in {
			ClipboardChangeSource.RECORD,
			ClipboardChangeSource.APPEND_TEXT,
			ClipboardChangeSource.MANAGER_TEXT_REPLACEMENT,
		}
		if snapshot.sequenceNumber:
			self._lastAppliedSequenceNumber = snapshot.sequenceNumber
		self._applyCurrentSnapshot(snapshot)
		if shouldRecord and snapshot.canIncludeInHistory:
			if snapshot.contentType in IMAGE_CONTENT_TYPES:
				self._historyWriter.queueImageSnapshot(snapshot)
			else:
				item = clipboardItemFromSnapshot(snapshot)
				if item is not None:
					self._historyWriter.queueItem(
						item,
						replaceText=historyPreviousText
						if source == ClipboardChangeSource.APPEND_TEXT
						else None,
						expectedText=item.text
						if source == ClipboardChangeSource.MANAGER_TEXT_REPLACEMENT
						else None,
					)
		if shouldRecord and snapshot.canUpload and snapshot.text:
			tiantanSync = self.tiantanSync
			if tiantanSync is not None:
				tiantanSync.onClipboardTextChanged(snapshot.text)
		if shouldRecord and snapshot.canIncludeInHistory and snapshot.richFormatsDropped and snapshot.text:
			# Translators: Message shown when oversized rich formats are omitted from a history entry.
			ui.message(_("Some text formatting was too large and was not saved in history"))
		if shouldRecord:
			playCopy()

	@staticmethod
	def _createSummaryCalculation(snapshot: ClipboardSnapshot) -> _SummaryCalculation | None:
		"""Return the idle background calculation that summarizes a clipboard snapshot, if any."""
		if snapshot.contentType == ClipboardContentType.FILES:
			return FileSizeCalculation(snapshot.files) if snapshot.files else None
		if snapshot.text and snapshot.contentType in _TEXT_CONTENT_TYPES:
			return TextStatisticsCalculation(snapshot.text)
		return None

	def _applyCurrentSnapshot(self, snapshot: ClipboardSnapshot) -> None:
		"""Update current clipboard navigation without reading or writing history."""
		self._pendingSummaryReport = None
		if self._summaryCalculation is not None:
			self._summaryCalculation.cancel()
		self._summaryCalculation = self._createSummaryCalculation(snapshot)
		self._contentType = snapshot.contentType
		self._text = snapshot.text
		self._canUpload = snapshot.canUpload
		self._summary = formatCurrentSummary(snapshot)
		if snapshot.contentType in _TEXT_CONTENT_TYPES and snapshot.text:
			navigationText = snapshot.text
		elif snapshot.contentType == ClipboardContentType.FILES:
			navigationText = formatFileNavigationText(snapshot.files)
		else:
			navigationText = self._summary
		self.navigator.setText(navigationText)

	def _onHistoryItemStored(self, result: HistoryWriteResult, expectedText: str | None) -> None:
		"""Reset the history position and refresh visible metadata after a history record is stored."""
		if self.isHistoryCategory(self._storedItemCategory):
			self._storedItemIndex = 0
		if result.imageWasDropped:
			# Translators: Message shown when mixed clipboard content is retained as text only.
			ui.message(_("The text was saved in history, but its image could not be stored"))
		self.oneDriveSync.notifyLocalChange()
		if expectedText is None:
			self._refreshManager()
		else:
			self._refreshManager(
				preferredHistoryItemId=result.itemId,
				expectedText=expectedText,
			)

	def _requireLastSpokenText(self) -> str:
		text = _getLastSpokenText()
		if not text:
			# Translators: Error shown when a last-spoken command has no captured speech text.
			raise ValueError(_("No spoken text is available"))
		return text

	def _appendTextToClipboard(self, text: str) -> None:
		"""Append to existing text or replace non-text content, preserving clipboard policy flags."""
		snapshot = self.monitor.readNow()
		if snapshot.contentType == ClipboardContentType.ERROR:
			# Translators: Error shown when text cannot be appended because the clipboard is busy.
			raise RuntimeError(_("Could not read the current clipboard"))
		if not snapshot.sequenceNumber or snapshot.sequenceNumber != self._lastAppliedSequenceNumber:
			self._pendingWrites.clear()
			self._awaitingInitialSnapshot = False
			self._applySnapshot(snapshot, ClipboardChangeSource.RECORD)
		clipboardText = snapshot.text if snapshot.contentType in _TEXT_CONTENT_TYPES else ""
		separator = "\n" if clipboardText and not clipboardText.endswith(("\r", "\n")) else ""
		self._writeClipboardText(
			f"{clipboardText}{separator}{text}",
			ClipboardChangeSource.APPEND_TEXT,
			expectedSequenceNumber=snapshot.sequenceNumber,
			canIncludeInHistory=snapshot.canIncludeInHistory,
			canUpload=snapshot.canUpload,
			historyPreviousText=clipboardText or None,
		)

	def _captureOriginalClipboardForTemporaryPaste(self) -> tuple[ClipboardSnapshot | None, int, bool]:
		"""Capture the restorable clipboard, its sequence number, and whether applying it succeeded."""
		snapshot = self.monitor.readNow()
		applied = True
		sequenceNumber = snapshot.sequenceNumber or self.monitor.getSequenceNumber()
		imageNormalizationFailed = False
		if snapshot.contentType in IMAGE_CONTENT_TYPES and snapshot.imageData is None:
			try:
				snapshot, _imageWasTooLarge = normalizeImageSnapshot(snapshot)
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
				self._applySnapshot(snapshot, ClipboardChangeSource.RECORD)
			except Exception:
				applied = False
				log.debugWarning("Could not apply the original clipboard before pasting.", exc_info=True)
		# Text-bearing snapshots remain useful when rich formats or a mixed image were omitted.
		if snapshot.contentType in (
			ClipboardContentType.ERROR,
			ClipboardContentType.PROTECTED,
			ClipboardContentType.UNSUPPORTED,
		) or (snapshot.contentType == ClipboardContentType.IMAGE and snapshot.imageData is None):
			return None, sequenceNumber, applied
		return snapshot, sequenceNumber, applied

	def _writeClipboardText(
		self,
		text: str,
		source: ClipboardChangeSource,
		expectedSequenceNumber: int | None = None,
		*,
		canIncludeInHistory: bool = True,
		canUpload: bool = True,
		historyPreviousText: str | None = None,
	) -> int:
		"""Write non-empty text and synchronously apply its source semantics."""
		text = normalizeUnicodeText(text)
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
				historyPreviousText=historyPreviousText,
			)
		except ClipboardSequenceChangedError as error:
			# Translators: Error shown when another application changes the clipboard during an append action.
			raise RuntimeError(_("The clipboard changed before it could be updated. Try again.")) from error

	def _writeSnapshot(
		self,
		snapshot: ClipboardSnapshot,
		source: ClipboardChangeSource,
		*,
		expectedSequenceNumber: int | None = None,
		preparedPngDib: bytes | None = None,
		historyPreviousText: str | None = None,
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
			if source is ClipboardChangeSource.REGISTER_ONLY:
				self._registerOwnWrite(error.sequenceNumber)
			else:
				self._pendingWrites = {error.sequenceNumber} if error.sequenceNumber else set()
				self._pendingWritesWereApplied = True
				self._applyFailedLocalWrite(error.sequenceNumber)
			self.monitor.handleClipboardUpdate()
			raise ClipboardWriteFailedError(error.sequenceNumber) from error
		except (OSError, ValueError) as error:
			self.monitor.handleClipboardUpdate()
			# Translators: Error shown when supported formats cannot be written to the clipboard.
			raise RuntimeError(_("Could not write to the system clipboard")) from error
		self._awaitingInitialSnapshot = False
		if source is ClipboardChangeSource.REGISTER_ONLY:
			self._registerOwnWrite(sequenceNumber)
			return sequenceNumber
		appliedSnapshot = replace(snapshot, sequenceNumber=sequenceNumber)
		self._pendingWrites = {sequenceNumber}
		self._pendingWritesWereApplied = True
		self._applySnapshot(
			appliedSnapshot,
			source,
			historyPreviousText=historyPreviousText,
		)
		return sequenceNumber

	def _registerOwnWrite(self, sequenceNumber: int) -> None:
		"""Treat a local write as our own without changing current clipboard state."""
		self._pendingWrites = {sequenceNumber} if sequenceNumber else set()
		self._pendingWritesWereApplied = False
		if sequenceNumber:
			self._lastAppliedSequenceNumber = sequenceNumber

	def _applyLeftoverClipboardContent(
		self,
		sequenceNumber: int,
		knownSnapshot: ClipboardSnapshot | None,
	) -> None:
		"""Apply registered-only content left on the clipboard, or stop trusting current state."""
		if not sequenceNumber or self.monitor.getSequenceNumber() != sequenceNumber:
			# A newer clipboard change no longer matches our sequence number and is applied when it arrives.
			return
		try:
			if knownSnapshot is not None:
				self._applySnapshot(
					replace(knownSnapshot, sequenceNumber=sequenceNumber),
					ClipboardChangeSource.NO_RECORD,
				)
				return
			snapshot = self.monitor.readNow()
			if (
				snapshot.contentType != ClipboardContentType.ERROR
				and snapshot.sequenceNumber == sequenceNumber
				and self.monitor.getSequenceNumber() == sequenceNumber
			):
				self._applySnapshot(snapshot, ClipboardChangeSource.NO_RECORD)
				return
		except Exception:
			log.debugWarning("Could not apply clipboard content left by a temporary paste.", exc_info=True)
		self._distrustRegisteredClipboardState(sequenceNumber, reread=False)

	def _distrustRegisteredClipboardState(self, sequenceNumber: int, *, reread: bool) -> None:
		"""Make sequence checks re-read the clipboard after a registered-only write could not be reflected."""
		if not sequenceNumber or self.monitor.getSequenceNumber() != sequenceNumber:
			# A newer clipboard change no longer matches our sequence number and is applied when it arrives.
			return
		if self._lastAppliedSequenceNumber == sequenceNumber:
			self._lastAppliedSequenceNumber = 0
		if reread:
			self._pendingWrites.clear()
			self.monitor.handleClipboardUpdate()

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
				self._applySnapshot(snapshot, ClipboardChangeSource.NO_RECORD)
		except Exception:
			log.debugWarning("Could not apply clipboard content left by a failed write.", exc_info=True)

	def _clearClipboard(
		self,
		*,
		source: ClipboardChangeSource = ClipboardChangeSource.NO_RECORD,
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
		if source is ClipboardChangeSource.REGISTER_ONLY:
			self._registerOwnWrite(sequenceNumber)
			return sequenceNumber
		self._pendingWrites = {sequenceNumber}
		self._pendingWritesWereApplied = True
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

	def _ensureCurrentContentHasText(self) -> bool:
		"""Report when clipboard selection is unavailable because there is no text."""
		if self._text:
			return True
		playBoundary()
		# Translators: Presented when a clipboard text command is used without clipboard text.
		ui.message(_("There is no text on the clipboard"))
		return False

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

	def _moveStoredItem(self, direction: int) -> None:
		"""Move the global stored-item position and report one concise summary."""
		if direction not in (-1, 1):
			raise ValueError(direction)
		self.navigator.setSelectionOffsets(None)
		category = self._resolveStoredItemCategory()
		item, newIndex, itemCount = self._getStoredItemSummaryAt(category, direction)
		if item is None:
			self._reportEmptyStoredItemCategory(category)
			return
		self._storedItemIndex = newIndex
		if item.contentType != ClipboardItemType.PLAIN_TEXT:
			playNonPlainText()
		elif newIndex in (0, itemCount - 1):
			playBoundary()
		# Translators: Numbered clipboard entry summary reported by the global category navigation commands.
		ui.message(
			_("{index}. {summary}").format(
				index=newIndex + 1,
				summary=formatItemSummary(item),
			),
		)

	def _reportEmptyStoredItemCategory(self, category: CategoryId) -> None:
		"""Report that the current stored-item category has no entries."""
		playBoundary()
		self._reportStoredItemCategory(category, 0)

	def _reportStoredItemCategory(self, category: CategoryId, itemCount: int) -> None:
		"""Report a stored-item category and its entry count."""
		ui.message(
			ngettext(
				# Translators: Category and entry count reported by the global category navigation command.
				"{category}, {count} entry",
				"{category}, {count} entries",
				itemCount,
			).format(category=self.getCategoryLabel(category), count=itemCount),
		)

	def _resolveStoredItemCategory(self) -> CategoryId:
		"""Return the current stored-item category, falling back to history if needed."""
		if self._storedItemCategory not in self.getCategories():
			self._storedItemCategory = _HISTORY_CATEGORY_ID
			self._storedItemIndex = 0
		return self._storedItemCategory

	def _getStoredItemSummaryAt(
		self,
		category: CategoryId,
		offset: int = 0,
	) -> tuple[ClipboardItemSummary | None, int, int]:
		"""Return one stored-item summary at the current position plus an offset."""
		with self._userStorageErrors():
			return self.storage.getSummaryAt(self._storageCategory(category), self._storedItemIndex, offset)

	def _getCategorySummaries(self, category: CategoryId) -> tuple[ClipboardItemSummary, ...]:
		"""Return metadata-only entries for one manager category."""
		with self._userStorageErrors():
			return self.storage.getItems(self._storageCategory(category))

	def _getStoredItemById(self, category: CategoryId, itemId: int) -> ClipboardItem:
		"""Load one complete entry after validating its stable category membership."""
		with self._userStorageErrors():
			return self.storage.getItemById(self._storageCategory(category), itemId)

	def _getStoredItemContentById(self, category: CategoryId, itemId: int) -> ClipboardItemContent:
		"""Load displayable content by stable identifier without payload BLOBs."""
		with self._userStorageErrors(reraiseItemNotFound=True):
			return self.storage.getItemContentById(self._storageCategory(category), itemId)

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

	@contextmanager
	def _userStorageErrors(self, *, reraiseItemNotFound: bool = False) -> Iterator[None]:
		"""Translate storage errors raised in the block into user-facing ``ValueError`` messages.

		With ``reraiseItemNotFound``, ``ItemNotFoundError`` propagates unchanged.
		"""
		try:
			yield
		except StorageError as error:
			if reraiseItemNotFound and isinstance(error, ItemNotFoundError):
				raise
			self._raiseUserStorageError(error)

	def _storageCategory(self, category: CategoryId) -> str | None:
		"""Return the storage scope of a category identifier, using ``None`` for history."""
		if self.isHistoryCategory(category):
			return None
		assert isinstance(category, str)
		return category

	def _refreshManager(
		self,
		preferredHistoryItemId: int | None = None,
		expectedText: str | None = None,
	) -> None:
		manager = self.manager
		if manager is not None:
			try:
				wx.CallAfter(
					self._refreshManagerIfCurrent,
					manager,
					preferredHistoryItemId,
					expectedText,
				)
			except RuntimeError:
				if self._isStarted:
					log.debugWarning("Could not schedule a clipboard manager refresh.", exc_info=True)

	def _refreshManagerIfCurrent(
		self,
		manager: ClipboardManagerFrame,
		preferredHistoryItemId: int | None = None,
		expectedText: str | None = None,
	) -> None:
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
			if preferredHistoryItemId is None and expectedText is None:
				manager.refreshFromController()
			else:
				manager.refreshFromController(
					preferredHistoryItemId=preferredHistoryItemId,
					expectedText=expectedText,
				)

	def _clearTiantanDialog(self) -> None:
		self.tiantanDialog = None

	def _clearOneDriveDialog(self, dialog: OneDriveSyncDialog) -> None:
		"""Clear the controller's OneDrive dialog reference after destruction."""
		if self.oneDriveDialog is dialog:
			self.oneDriveDialog = None

	def _getCurrentTiantanUploadText(self) -> str:
		"""Return current allowed clipboard text for an explicit cloud upload."""
		text = self._text
		canUpload = self._canUpload
		# During a temporary paste, current state keeps the original while the clipboard holds other content.
		if (
			self._paster.hasPendingRestore
			or self.monitor.getSequenceNumber() != self._lastAppliedSequenceNumber
		):
			snapshot = self.monitor.readNow(includeRichFormats=False, includeImages=False)
			if snapshot.contentType == ClipboardContentType.ERROR:
				# Translators: Error shown when current clipboard upload policy cannot be checked.
				raise RuntimeError(_("Could not verify the current clipboard for upload"))
			text = snapshot.text
			canUpload = snapshot.canUpload
		if not canUpload:
			# Translators: Message shown when an application forbids cloud upload of clipboard content.
			raise ValueError(_("The current clipboard content is protected from cloud upload"))
		return text

	def _onTiantanStateChanged(self) -> None:
		if self.tiantanSync is None:
			return
		if self.tiantanDialog is not None:
			self.tiantanDialog.refreshFromManager()
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
		"""Reset the stored-item position and refresh the manager after a remote merge."""
		if not self._isStarted:
			return
		self._storedItemCategory = _HISTORY_CATEGORY_ID
		self._storedItemIndex = 0
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

	def _onTiantanFetchDone(self, success: bool, message: str) -> None:
		if success:
			return
		self._tiantanFetchInProgress = False
		if message:
			ui.message(message)

	def _abandonTiantanFetch(self) -> None:
		"""Release the receive guard when wx rejects a worker completion."""
		self._tiantanFetchInProgress = False

	def _onTiantanReceiveDone(self, success: bool, message: str) -> None:
		"""Finish a menu-initiated Tiantan Cloud Clipboard receive and announce its result."""
		self._tiantanFetchInProgress = False
		if message:
			ui.message(message)

	def _sendTiantanPasteGesture(
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
			keyReleaseState = waitForTriggerKeysReleased(
				triggerKeyCodes,
				keyReleaseDeadline,
				lambda: self._sendTiantanPasteGesture(
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
			if (
				self._lastAppliedSequenceNumber != expectedSequenceNumber
				or self._text != expectedText
				or self.monitor.getSequenceNumber() != expectedSequenceNumber
				or self.monitor.getOwnerHandle() != self._getClipboardOwnerHandle()
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
				self._tiantanFetchInProgress = False

	def _getSelectionText(self) -> str | None:
		"""Return selected text from the focused object or NVDA's review copy range."""
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
			info = None
		if info is not None and not info.isCollapsed:
			return info.text
		try:
			import globalCommands

			# ponytail: private NVDA state; use a public review-selection API if one is added.
			reviewSelection = getattr(
				getattr(globalCommands, "commands", None),
				"_reviewSelectThenCopyRange",
				None,
			)
			return reviewSelection.text if reviewSelection is not None else None
		except (AttributeError, LookupError, NotImplementedError, RuntimeError):
			return None
