# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for clipboard navigation selection state without loading NVDA."""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
from enum import Enum, auto
from pathlib import Path
from time import monotonic
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.test_clipboardMonitor import clipboardMonitor


_NAVIGATION_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "navigation.py"
_CONTROLLER_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "controller.py"

_UI = SimpleNamespace(message=Mock())
_SPEECH = SimpleNamespace(speakTextSelected=Mock())
_BRAILLE = SimpleNamespace(handler=SimpleNamespace(message=Mock()))


@dataclass(frozen=True)
class _Offsets:
	"""Provide the bookmark fields used by the navigator."""

	startOffset: int
	endOffset: int


class _ClipboardTextInfo:
	"""Provide enough TextInfo behavior to exercise selection state."""

	def __init__(self, owner: object, position: object) -> None:
		"""Create a collapsed range from a named position or explicit offsets."""
		self._owner = owner
		offset = position.startOffset if isinstance(position, _Offsets) else 0
		self.bookmark = _Offsets(offset, offset)

	def copy(self) -> _ClipboardTextInfo:
		"""Return an independent copy of this range."""
		return _ClipboardTextInfo(self._owner, self.bookmark)

	def expand(self, _unit: str) -> None:
		"""Expand the range to one character for these ASCII tests."""
		self.bookmark = _Offsets(self.bookmark.startOffset, self.bookmark.startOffset + 1)


def _loadClipboardNavigator() -> type:
	"""Load the navigator class with small TextInfo substitutes."""
	tree = ast.parse(_NAVIGATION_PATH.read_text(encoding="utf-8"))
	nodes = [
		node
		for node in tree.body
		if isinstance(node, ast.ClassDef) and node.name in {"_ClipboardTextOwner", "ClipboardNavigator"}
	]
	namespace = {
		"NavigationResult": object,
		"Offsets": _Offsets,
		"_ClipboardTextInfo": _ClipboardTextInfo,
		"textInfos": SimpleNamespace(POSITION_FIRST="first", UNIT_CHARACTER="character", TextInfo=object),
	}
	exec(compile(ast.Module(body=nodes, type_ignores=[]), _NAVIGATION_PATH, "exec"), namespace)
	return namespace["ClipboardNavigator"]


ClipboardNavigator = _loadClipboardNavigator()


def _loadSelectionControllerMethods() -> tuple[object, ...]:
	"""Load the selection controller path without importing NVDA dependencies."""
	tree = ast.parse(_CONTROLLER_PATH.read_text(encoding="utf-8"))
	controllerClass = next(
		node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ClipboardController"
	)
	nodes = [
		node
		for node in tree.body
		if (
			isinstance(node, ast.ClassDef)
			and node.name
			in {
				"_TemporaryPasteRequest",
				"_TemporaryPasteState",
				"_ClipboardChangeSource",
				"_ClipboardWriteFailedError",
				"_TemporaryPasteApplyError",
			}
			or isinstance(node, ast.FunctionDef)
			and node.name
			in {
				"_getTextOrCharacterCount",
				"_normalizeUnicodeText",
				"_normalizeClipboardLineEndings",
				"_mapClipboardSelectionOffsets",
				"_getPlainTextPasteRequest",
			}
		)
	]
	nodes.extend(
		node
		for node in controllerClass.body
		if isinstance(node, ast.FunctionDef)
		and node.name
		in {
			"_beginTemporaryPaste",
			"_pasteTextTemporarily",
			"_pasteSnapshotTemporarily",
			"_restoreClipboardAfterTemporaryPaste",
			"_restoreOriginalClipboardAfterTemporaryPaste",
			"_moveStoredItem",
			"cycleStoredItemCategory",
			"markClipboardSelectionStart",
			"markClipboardSelectionEnd",
			"pasteClipboardSelection",
			"pasteCurrentNavigationTarget",
			"pasteCurrentStoredItem",
		}
	)
	namespace = {
		"_": lambda message: message,
		"_PASTE_KEY_RELEASE_TIMEOUT_SECONDS": 3.0,
		"_TEMPORARY_CLIPBOARD_RESTORE_DELAY": 300,
		"ClipboardSnapshot": clipboardMonitor.ClipboardSnapshot,
		"ClipboardContentType": clipboardMonitor.ClipboardContentType,
		"ClipboardSequenceChangedError": clipboardMonitor.ClipboardSequenceChangedError,
		"ClipboardItemType": SimpleNamespace(PLAIN_TEXT="text"),
		"Enum": Enum,
		"auto": auto,
		"braille": _BRAILLE,
		"dataclass": dataclass,
		"monotonic": monotonic,
		"log": Mock(),
		"ngettext": lambda singular, plural, count: singular if count == 1 else plural,
		"replace": replace,
		"speech": _SPEECH,
		"ui": _UI,
	}
	exec(compile(ast.Module(body=nodes, type_ignores=[]), _CONTROLLER_PATH, "exec"), namespace)
	return (
		namespace["_beginTemporaryPaste"],
		namespace["_pasteTextTemporarily"],
		namespace["_pasteSnapshotTemporarily"],
		namespace["markClipboardSelectionEnd"],
		namespace["pasteClipboardSelection"],
		namespace["pasteCurrentNavigationTarget"],
		namespace["pasteCurrentStoredItem"],
		namespace["_restoreClipboardAfterTemporaryPaste"],
		namespace["_restoreOriginalClipboardAfterTemporaryPaste"],
		namespace["_moveStoredItem"],
		namespace["cycleStoredItemCategory"],
		namespace["markClipboardSelectionStart"],
	)


(
	_beginTemporaryPaste,
	_pasteTextTemporarily,
	_pasteSnapshotTemporarily,
	_markClipboardSelectionEnd,
	_pasteClipboardSelection,
	_pasteCurrentNavigationTarget,
	_pasteCurrentStoredItem,
	_restoreClipboardAfterTemporaryPaste,
	_restoreOriginalClipboardAfterTemporaryPaste,
	_moveStoredItem,
	_cycleStoredItemCategory,
	_markClipboardSelectionStart,
) = _loadSelectionControllerMethods()


class ClipboardNavigationSelectionTests(unittest.TestCase):
	"""Verify inclusive selection markers and their lifecycle."""

	def testSelectionIncludesBothMarkersInEitherDirection(self) -> None:
		"""Include both endpoint characters for forward and backward ranges."""
		navigator = ClipboardNavigator("abcdef")
		for startOffset, endOffset, expectedText in ((1, 4, "bcde"), (4, 1, "bcde"), (2, 2, "c")):
			with self.subTest(startOffset=startOffset, endOffset=endOffset):
				navigator.setPosition(startOffset)
				navigator.markSelectionStart()
				self.assertIsNone(navigator.getSelectedText())
				navigator.setPosition(endOffset)
				self.assertEqual(expectedText, navigator.markSelectionEnd())
				self.assertEqual(expectedText, navigator.getSelectedText())

	def testNewTextClearsSelection(self) -> None:
		"""Discard marked offsets when the clipboard navigation text changes."""
		navigator = ClipboardNavigator("old")
		navigator.markSelectionStart()
		navigator.setPosition(2)
		navigator.markSelectionEnd()

		navigator.setText("new")

		self.assertIsNone(navigator.getSelectedText())
		self.assertIsNone(navigator.markSelectionEnd())

	def testSelectionOffsetsCanBeRestoredAndCleared(self) -> None:
		"""Round-trip a backward range and clear both markers together."""
		navigator = ClipboardNavigator("abcdef")
		navigator.setPosition(4)
		navigator.markSelectionStart()
		navigator.setPosition(1)
		navigator.markSelectionEnd()
		offsets = navigator.getSelectionOffsets()

		navigator.setText("abcdef")
		navigator.setSelectionOffsets(offsets)
		self.assertEqual((4, 1), navigator.getSelectionOffsets())
		self.assertEqual("bcde", navigator.getSelectedText())

		navigator.setSelectionOffsets(None)
		self.assertIsNone(navigator.getSelectionOffsets())
		self.assertIsNone(navigator.getSelectedText())
		self.assertIsNone(navigator.markSelectionEnd())

	def testSelectionOffsetsRejectPositionsOutsideCurrentText(self) -> None:
		"""Reject stale ranges that cannot refer to the current navigation text."""
		navigator = ClipboardNavigator("abc")
		for offsets in ((-1, 1), (0, 3), (3, 0)):
			with self.subTest(offsets=offsets), self.assertRaises(ValueError):
				navigator.setSelectionOffsets(offsets)


class ClipboardSelectionControllerTests(unittest.TestCase):
	"""Verify selection feedback and clipboard ownership checks."""

	def setUp(self) -> None:
		"""Reset feedback calls shared by the extracted production methods."""
		_UI.message.reset_mock()
		_SPEECH.speakTextSelected.reset_mock()
		_BRAILLE.handler.message.reset_mock()

	def testLongSelectionUsesBoundedBrailleFeedback(self) -> None:
		"""Send long text through bounded selection feedback without changing speech semantics."""
		selectedText = "x" * 512
		controller = SimpleNamespace(
			_ensureCurrentContentHasText=Mock(return_value=True),
			navigator=SimpleNamespace(markSelectionEnd=Mock(return_value=selectedText)),
		)

		_markClipboardSelectionEnd(controller)

		_SPEECH.speakTextSelected.assert_called_once_with(selectedText)
		_BRAILLE.handler.message.assert_called_once_with("512 characters selected")

	def testClipboardChangeCancelsDeferredSelectionPaste(self) -> None:
		"""Preserve marked offsets but never write a selection invalidated while keys were held."""
		navigator = ClipboardNavigator("abcdef")
		navigator.setSelectionOffsets((4, 1))
		controller = SimpleNamespace(
			_ensureCurrentContentHasText=Mock(return_value=True),
			navigator=navigator,
			_lastAppliedSequenceNumber=7,
			_text="abcdef",
			_temporaryPasteInProgress=False,
			_isStarted=True,
			_captureOriginalClipboardForTemporaryPaste=Mock(return_value=(None, 7)),
			_writeSnapshot=Mock(),
		)
		controller._pasteTextTemporarily = MethodType(_pasteTextTemporarily, controller)
		controller._pasteSnapshotTemporarily = MethodType(_pasteSnapshotTemporarily, controller)
		controller._beginTemporaryPaste = Mock(wraps=MethodType(_beginTemporaryPaste, controller))
		waitForKeys = Mock(side_effect=(None, True))
		with patch.dict(_beginTemporaryPaste.__globals__, _waitForTriggerKeysReleased=waitForKeys):
			_pasteClipboardSelection(controller, frozenset({1}))

			self.assertTrue(controller._temporaryPasteInProgress)
			controller._captureOriginalClipboardForTemporaryPaste.assert_not_called()
			controller._writeSnapshot.assert_not_called()
			request = controller._beginTemporaryPaste.call_args.args[0]
			self.assertEqual("bcde", request.snapshot.text)
			self.assertEqual((4, 1), request.originalSelectionOffsets)

			controller._captureOriginalClipboardForTemporaryPaste.return_value = (None, 8)
			waitForKeys.call_args.args[2]()

		controller._captureOriginalClipboardForTemporaryPaste.assert_called_once_with()
		controller._writeSnapshot.assert_not_called()
		self.assertFalse(controller._temporaryPasteInProgress)
		_UI.message.assert_called_once_with("The clipboard changed, so the selected text was not pasted")

	def testChangedFocusCancelsBeforeTemporaryClipboardWrite(self) -> None:
		"""Cancel a prepared paste before it can replace the clipboard for another control."""
		expectedFocus = object()
		controller = SimpleNamespace(
			_isStarted=True,
			_isSameFocus=Mock(return_value=False),
			_temporaryPasteInProgress=True,
		)

		_beginTemporaryPaste(
			controller,
			SimpleNamespace(expectedFocus=expectedFocus),
			0,
		)

		controller._isSameFocus.assert_called_once_with(expectedFocus)
		self.assertFalse(controller._temporaryPasteInProgress)
		_UI.message.assert_called_once_with("Focus changed. Clipboard paste was cancelled.")

	def testDeferredRestoreRespectsSelectionInvalidation(self) -> None:
		"""Restore clipboard content without reviving a selection cleared during a pending paste."""
		for navigation in (None, "item", "category", "startBeforePaste", "startAfterPaste"):
			with self.subTest(navigation=navigation):
				originalSnapshot = clipboardMonitor.ClipboardSnapshot(
					clipboardMonitor.ClipboardContentType.TEXT,
					text="abcdef",
					sequenceNumber=1,
				)
				navigator = ClipboardNavigator(originalSnapshot.text)
				navigator.setSelectionOffsets((4, 1))
				controller = SimpleNamespace(
					navigator=navigator,
					_text=originalSnapshot.text,
					_lastAppliedSequenceNumber=1,
					_isStarted=True,
					_temporaryPasteInProgress=False,
					_shouldRestoreTemporaryPasteSelection=False,
					_temporaryPasteState=None,
					_ensureCurrentContentHasText=Mock(return_value=True),
					_captureOriginalClipboardForTemporaryPaste=Mock(return_value=(originalSnapshot, 1)),
					_getVerifiedTemporarySequenceNumber=Mock(return_value=2),
					_storedItemCategory="history",
					_storedItemIndex=0,
					getCategories=Mock(return_value=["history", "saved"]),
					_resolveStoredItemCategory=Mock(return_value="history"),
					_getStoredItemSummaryAt=Mock(
						side_effect=lambda _category, direction=0: (
							SimpleNamespace(contentType="text"),
							direction,
							3,
						),
					),
					_formatItemSummary=Mock(return_value="stored text"),
					_reportStoredItemCategory=Mock(),
					pasteClipboardSelection=Mock(),
					pasteCurrentStoredItem=Mock(),
				)
				controller.monitor = SimpleNamespace(
					getSequenceNumber=lambda: controller._lastAppliedSequenceNumber,
				)

				def writeSnapshot(
					snapshot: object, _source: object, *, expectedSequenceNumber: int, **_kwargs
				) -> int:
					"""Simulate clipboard writes and their local navigation update at the OS boundary."""
					self.assertEqual(controller._lastAppliedSequenceNumber, expectedSequenceNumber)
					controller._lastAppliedSequenceNumber += 1
					controller._text = snapshot.text
					navigator.setText(snapshot.text)
					return controller._lastAppliedSequenceNumber

				controller._writeSnapshot = Mock(side_effect=writeSnapshot)
				for method in (
					_pasteTextTemporarily,
					_pasteSnapshotTemporarily,
					_beginTemporaryPaste,
					_restoreClipboardAfterTemporaryPaste,
					_restoreOriginalClipboardAfterTemporaryPaste,
				):
					setattr(controller, method.__name__, MethodType(method, controller))
				scheduleRestore = Mock()
				waitForKeys = Mock(side_effect=(None, True) if navigation == "startBeforePaste" else (True,))
				with patch.dict(
					_beginTemporaryPaste.__globals__,
					_waitForTriggerKeysReleased=waitForKeys,
					callLater=scheduleRestore,
					KeyboardInputGesture=Mock(),
					ui=Mock(),
				):
					_pasteClipboardSelection(controller, frozenset({1}))
					if navigation == "startBeforePaste":
						controller._writeSnapshot.assert_not_called()
						scheduleRestore.assert_not_called()
						navigator.setPosition(5)
						_markClipboardSelectionStart(controller)
						self.assertIsNone(navigator.getSelectedText())
						waitForKeys.call_args.args[2]()
					self.assertEqual("bcde", controller._text)
					scheduleRestore.assert_called_once()
					_delay, restore, state = scheduleRestore.call_args.args
					if navigation == "item":
						_moveStoredItem(controller, 1)
					elif navigation == "category":
						_cycleStoredItemCategory(controller)
					elif navigation == "startAfterPaste":
						navigator.setPosition(2)
						_markClipboardSelectionStart(controller)
					storedPosition = controller._storedItemCategory, controller._storedItemIndex
					restore(state)

				self.assertEqual(2, controller._writeSnapshot.call_count)
				self.assertEqual(originalSnapshot, controller._writeSnapshot.call_args.args[0])
				self.assertEqual(originalSnapshot.text, controller._text)
				self.assertFalse(controller._temporaryPasteInProgress)
				self.assertIsNone(controller._temporaryPasteState)
				self.assertEqual(
					storedPosition, (controller._storedItemCategory, controller._storedItemIndex)
				)
				_pasteCurrentNavigationTarget(controller, frozenset({1}))
				if navigation is None:
					self.assertEqual("bcde", navigator.getSelectedText())
					controller.pasteClipboardSelection.assert_called_once_with(frozenset({1}))
					controller.pasteCurrentStoredItem.assert_not_called()
				else:
					self.assertIsNone(navigator.getSelectedText())
					controller.pasteCurrentStoredItem.assert_called_once_with(frozenset({1}))
					controller.pasteClipboardSelection.assert_not_called()

	def testTemporaryPasteFallbackRetainsOriginalClipboardAndPosition(self) -> None:
		"""Keep payload, ownership and restoration correct across one optional text fallback."""
		for failure, expectedWrites in (
			(None, 2),
			("prepare", 3),
			("partial", 3),
			("fallback", 3),
			("fallbackPartial", 3),
			("plainFailure", 1),
			("owner", 1),
			("focus", 2),
			("unmapped", 2),
		):
			with self.subTest(failure=failure):
				contentType = clipboardMonitor.ClipboardContentType
				original = clipboardMonitor.ClipboardSnapshot(
					contentType.TEXT, text="a\r\nb", sequenceNumber=1
				)
				rich = clipboardMonitor.ClipboardSnapshot(
					contentType.TEXT if failure == "plainFailure" else contentType.FORMATTED_TEXT,
					text="full\ntext",
					html=None if failure == "plainFailure" else b"html",
				)
				navigator = ClipboardNavigator("a\nb")
				navigator.setPosition(2)
				controller = SimpleNamespace(
					navigator=navigator,
					_text="a\nb" if failure != "unmapped" else "different",
					_lastAppliedSequenceNumber=1,
					_isStarted=True,
					_temporaryPasteInProgress=False,
					_temporaryPasteState=None,
					_captureOriginalClipboardForTemporaryPaste=Mock(return_value=(original, 1)),
					_getClipboardOwnerHandle=Mock(return_value=10),
					_isSameFocus=Mock(return_value=True),
					monitor=SimpleNamespace(getOwnerHandle=Mock(return_value=10)),
				)
				currentSnapshot = original
				controller.monitor.getSequenceNumber = lambda: currentSnapshot.sequenceNumber
				controller._getVerifiedTemporarySequenceNumber = lambda _state: currentSnapshot.sequenceNumber
				writeError = _beginTemporaryPaste.__globals__["_ClipboardWriteFailedError"]

				def writeSnapshot(
					snapshot: object, _source: object, *, expectedSequenceNumber: int, **_kwargs
				) -> int:
					"""Model format failure before or after replacing the clipboard, without sending real keys."""
					nonlocal currentSnapshot
					self.assertEqual(currentSnapshot.sequenceNumber, expectedSequenceNumber)
					if snapshot.contentType == contentType.FORMATTED_TEXT:
						if failure == "prepare":
							raise RuntimeError("write failed") from ValueError("rich format unavailable")
						if failure in ("partial", "fallback", "fallbackPartial", "owner", "focus"):
							currentSnapshot = clipboardMonitor.ClipboardSnapshot(
								contentType.EMPTY, sequenceNumber=2
							)
							if failure == "owner":
								controller.monitor.getOwnerHandle.return_value = 20
								currentSnapshot = replace(currentSnapshot, sequenceNumber=3)
							if failure == "focus":
								controller._isSameFocus.return_value = False
							raise writeError(2)
					if failure == "fallbackPartial" and snapshot.text == rich.text:
						currentSnapshot = replace(currentSnapshot, sequenceNumber=3)
						raise writeError(3)
					if failure in ("fallback", "plainFailure") and snapshot.text == rich.text:
						raise RuntimeError("write failed") from OSError("clipboard busy")
					currentSnapshot = replace(snapshot, sequenceNumber=currentSnapshot.sequenceNumber + 1)
					controller._lastAppliedSequenceNumber = currentSnapshot.sequenceNumber
					controller._text = snapshot.text
					navigator.setText(snapshot.text)
					return currentSnapshot.sequenceNumber

				controller._writeSnapshot = Mock(side_effect=writeSnapshot)
				for method in (
					_beginTemporaryPaste,
					_restoreClipboardAfterTemporaryPaste,
					_restoreOriginalClipboardAfterTemporaryPaste,
				):
					setattr(controller, method.__name__, MethodType(method, controller))
				# A changed owner must not be treated as a verified temporary payload during recovery.
				if failure in ("owner", "focus", "fallback", "fallbackPartial"):
					controller._getVerifiedTemporarySequenceNumber = Mock(return_value=None)
				schedule = Mock()
				keyboard = Mock()
				with patch.dict(
					_beginTemporaryPaste.__globals__,
					_waitForTriggerKeysReleased=Mock(return_value=True),
					callLater=schedule,
					KeyboardInputGesture=keyboard,
					ui=Mock(),
				):
					_pasteSnapshotTemporarily(controller, rich, "entry", frozenset(), expectedFocus=object())
					if failure in ("owner", "focus", "fallback", "fallbackPartial", "plainFailure"):
						keyboard.fromName.assert_not_called()
						schedule.assert_not_called()
					else:
						keyboard.fromName.assert_called_once_with("control+v")
						keyboard.fromName.return_value.send.assert_called_once_with()
						self.assertEqual(rich.text, currentSnapshot.text)
						self.assertFalse(currentSnapshot.canIncludeInHistory)
						self.assertFalse(currentSnapshot.canUpload)
						self.assertEqual(
							b"html" if failure in (None, "unmapped") else None, currentSnapshot.html
						)
						schedule.assert_called_once()
						_delay, restore, state = schedule.call_args.args
						self.assertEqual(original, state.originalSnapshot)
						restore(state)
				self.assertFalse(controller._temporaryPasteInProgress)
				if failure == "owner":
					self.assertEqual(1, controller._writeSnapshot.call_count)
					self.assertEqual(3, currentSnapshot.sequenceNumber)
				else:
					self.assertEqual(original.text, currentSnapshot.text)
					if failure not in ("unmapped", "plainFailure"):
						self.assertEqual(3, navigator.getPosition())
				self.assertEqual(expectedWrites, controller._writeSnapshot.call_count)

	def testPasteTargetUsesSelectionOnlyWhenTheRangeIsComplete(self) -> None:
		"""Prefer marked text and otherwise route the shared gesture to stored-item navigation."""
		for startOffset, endOffset in ((None, None), (1, None), (1, 4), (4, 1), (2, 2)):
			with self.subTest(startOffset=startOffset, endOffset=endOffset):
				navigator = ClipboardNavigator("abcdef")
				if startOffset is not None:
					navigator.setPosition(startOffset)
					navigator.markSelectionStart()
				if endOffset is not None:
					navigator.setPosition(endOffset)
					navigator.markSelectionEnd()
				controller = SimpleNamespace(
					navigator=navigator,
					pasteClipboardSelection=Mock(),
					pasteCurrentStoredItem=Mock(),
				)
				triggerKeyCodes = frozenset({1, 2})

				_pasteCurrentNavigationTarget(controller, triggerKeyCodes)

				if endOffset is not None:
					controller.pasteClipboardSelection.assert_called_once_with(triggerKeyCodes)
					controller.pasteCurrentStoredItem.assert_not_called()
				else:
					controller.pasteCurrentStoredItem.assert_called_once_with(triggerKeyCodes)
					controller.pasteClipboardSelection.assert_not_called()

	def testStoredItemPasteUsesStableIdWithoutMoving(self) -> None:
		"""Load the current item by stable identifier and retain its navigation index."""
		category = object()
		summary = SimpleNamespace(itemId=42)
		item = object()
		snapshot = SimpleNamespace(imageFormat="PNG")
		focusObject = object()
		controller = SimpleNamespace(
			_storedItemIndex=3,
			_getFocusObject=Mock(return_value=focusObject),
			_resolveStoredItemCategory=Mock(return_value=category),
			_getStoredItemSummaryAt=Mock(return_value=(summary, 3, 5)),
			_getStoredItemById=Mock(return_value=item),
			_snapshotFromItem=Mock(return_value=snapshot),
			_formatItemSummary=Mock(return_value="summary"),
			_pasteSnapshotTemporarily=Mock(),
			_reportEmptyStoredItemCategory=Mock(),
		)
		triggerKeyCodes = frozenset({1, 2})

		_pasteCurrentStoredItem(controller, triggerKeyCodes)

		self.assertEqual(3, controller._storedItemIndex)
		controller._getStoredItemById.assert_called_once_with(category, 42)
		controller._pasteSnapshotTemporarily.assert_called_once_with(
			snapshot,
			"summary",
			triggerKeyCodes,
			expectedFocus=focusObject,
		)


if __name__ == "__main__":
	unittest.main()
