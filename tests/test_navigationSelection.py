# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for clipboard navigation selection state without loading NVDA."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock


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


def _loadSelectionControllerMethods() -> tuple[object, object, object, object]:
	"""Load the selection controller path without importing NVDA dependencies."""
	tree = ast.parse(_CONTROLLER_PATH.read_text(encoding="utf-8"))
	controllerClass = next(
		node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ClipboardController"
	)
	nodes = [
		node
		for node in tree.body
		if isinstance(node, ast.FunctionDef)
		and node.name in {"_getTextOrCharacterCount", "_normalizeUnicodeText"}
	]
	nodes.extend(
		node
		for node in controllerClass.body
		if isinstance(node, ast.FunctionDef)
		and node.name
		in {
			"_beginTemporaryTextPaste",
			"_pasteTextTemporarily",
			"markClipboardSelectionEnd",
			"pasteClipboardSelection",
		}
	)
	namespace = {
		"_": lambda message: message,
		"_PASTE_KEY_RELEASE_TIMEOUT_SECONDS": 3.0,
		"_waitForTriggerKeysReleased": lambda _keyCodes, _deadline, _retry: True,
		"braille": _BRAILLE,
		"monotonic": monotonic,
		"ngettext": lambda singular, plural, count: singular if count == 1 else plural,
		"speech": _SPEECH,
		"ui": _UI,
	}
	exec(compile(ast.Module(body=nodes, type_ignores=[]), _CONTROLLER_PATH, "exec"), namespace)
	return (
		namespace["markClipboardSelectionEnd"],
		namespace["pasteClipboardSelection"],
		namespace["_pasteTextTemporarily"],
		namespace["_beginTemporaryTextPaste"],
	)


(
	_markClipboardSelectionEnd,
	_pasteClipboardSelection,
	_pasteTextTemporarily,
	_beginTemporaryTextPaste,
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
		"""Revalidate the source sequence before replacing the clipboard for a paste."""
		writeSnapshot = Mock()
		controller = SimpleNamespace(
			_ensureCurrentContentHasText=Mock(return_value=True),
			navigator=SimpleNamespace(getSelectedText=Mock(return_value="selected")),
			_lastAppliedSequenceNumber=7,
			_temporaryTextPasteInProgress=False,
			_isStarted=True,
			_captureOriginalClipboardForTemporaryPaste=Mock(return_value=(None, 8)),
			_writeSnapshot=writeSnapshot,
		)
		controller._pasteTextTemporarily = MethodType(_pasteTextTemporarily, controller)
		controller._beginTemporaryTextPaste = MethodType(_beginTemporaryTextPaste, controller)

		_pasteClipboardSelection(controller, frozenset({1}))

		writeSnapshot.assert_not_called()
		self.assertFalse(controller._temporaryTextPasteInProgress)
		_UI.message.assert_called_once_with("The clipboard changed, so the selected text was not pasted")


if __name__ == "__main__":
	unittest.main()
