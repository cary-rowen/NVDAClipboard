# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for clipboard manager find and replace commands without loading NVDA."""

from __future__ import annotations

import ast
from pathlib import Path
import re
from time import perf_counter
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests._module_loader import loadAddonModule


class _WideStringOffsetConverter:
	"""Convert valid test offsets between Python strings and Windows UTF-16."""

	def __init__(self, text: str) -> None:
		self.text = text

	def encodedToStrOffsets(self, *offsets: int) -> tuple[int, ...]:
		encoded = self.text.encode("utf-16-le", errors="surrogatepass")
		return tuple(
			len(encoded[: offset * 2].decode("utf-16-le", errors="surrogatepass")) for offset in offsets
		)

	def strToEncodedOffsets(self, *offsets: int) -> tuple[int, ...]:
		return tuple(
			len(self.text[:offset].encode("utf-16-le", errors="surrogatepass")) // 2 for offset in offsets
		)


_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "managerEditor.py"
_SEARCH = loadAddonModule("nvdaClipboardEditorSearchTests", _PATH.with_name("search.py"))
_NAMESPACE = {
	"re": re,
	"findPreviousLiteralMatch": _SEARCH.findPreviousLiteralMatch,
	"matchRegexSelection": _SEARCH.matchRegexSelection,
	"textUtils": SimpleNamespace(WideStringOffsetConverter=_WideStringOffsetConverter),
	"wx": SimpleNamespace(ID_APPLY=1, ID_OK=2, ID_CANCEL=3),
	"_": lambda text: text,
	"ngettext": lambda singular, plural, count: singular if count == 1 else plural,
}
_CLASSES = [
	node
	for node in ast.parse(_PATH.read_text(encoding="utf-8")).body
	if isinstance(node, ast.ClassDef) and node.name == "_ManagerEditorCommands"
]
exec(compile(ast.Module(body=_CLASSES, type_ignores=[]), str(_PATH), "exec"), _NAMESPACE)
ManagerEditorCommands = _NAMESPACE["_ManagerEditorCommands"]


class ManagerEditorTests(unittest.TestCase):
	def testSingleReplaceUsesFullTextContext(self) -> None:
		cases = (
			("abc", (0, 3), r".+?", "X", (0, 3, "X")),
			("abcd", (0, 4), r"abc|abcd", "X", (0, 4, "X")),
			("abc abc suffix", (4, 7), r".+?", "X", (4, 7, "X")),
			("abc abc suffix", (4, 7), r".+", "X", (4, 7, "X")),
			("abc abc suffix", (4, 7), r"(?i)ABC|ABC ABC", "X", (4, 7, "X")),
			("abc abc suffix", (4, 7), r"(?<= )(?P<word>.+?)(?= suffix)", r"\g<word>!", (4, 7, "abc!")),
			("abc suffix", (0, 3), r"(?i).+?", "X", (0, 3, "X")),
			("abc suffix", (0, 3), r"(?x) .+? # trailing comment", "X", (0, 3, "X")),
			("abc suffix", (0, 3), "(?x) # leading\n (?i) .+? # trailing", "X", (0, 3, "X")),
			("abc suffix", (0, 3), "(?x)# note " + chr(92) + "\nignored\n.+?", "X", (0, 3, "X")),
			("abc suffix", (0, 3), "(?x)# note " + chr(92) + "\n[\n(?i).+?", "X", (0, 3, "X")),
			("abc suffix", (0, 3), r"(?#escaped\))(?#more)(?i).+?", "X", (0, 3, "X")),
			("abc suffix", (0, 3), r"(?a)(?i).+?", "X", (0, 3, "X")),
			("abc suffix", (0, 3), r"(?x)(?-x:.+?)", "X", (0, 3, "X")),
			("abc suffix", (0, 3), r"(?x)([a-z]+?)", r"\1!", (0, 3, "abc!")),
			("abcabc suffix", (0, 6), r"([a-z]+?)\1", r"\1!", (0, 6, "abc!")),
			("prefix abc suffix", (7, 10), r"^abc$", "X", None),
			("abc suffix", (0, 3), r"abc$", "X", None),
			("abc suffix", (0, 3), r"abc\Z", "X", None),
			("xabc", (1, 4), r"\Aabc", "X", None),
			("xabcy", (1, 4), r"\babc\b", "X", None),
			("xabcy", (1, 4), r"\Babc\B", "X", (1, 4, "X")),
			("xabc", (1, 4), r"(?<!x)abc", "X", None),
			("prefix abc suffix", (7, 10), r"abc(?= suffix)", "X", (7, 10, "X")),
			("abc suffix", (0, 3), r"abc(?! suffix)", "X", None),
			("x\nabc\ny", (2, 5), r"(?m)^abc$", "X", (2, 5, "X")),
			("abc\n", (0, 3), r"^abc$", "X", (0, 3, "X")),
			("xabcy", (1, 4), r"(?<=(x))(abc)(?=(y))", r"\1-\2-\3", (1, 4, "x-abc-y")),
			("a\nb suffix", (0, 3), r"(?s).+?", "X", (0, 3, "X")),
			("abc suffix", (0, 3), r"(?>.+)", "X", (0, 3, "X")),
			("abc suffix", (0, 3), r".++(?= suffix)", "X", (0, 3, "X")),
			("a" * 28, (0, 3), r"(a+)+", r"\1!", (0, 3, "aaa!")),
			("a" * 28, (0, 3), r"(a+)+(?=a)", r"\1!", (0, 3, "aaa!")),
			("a" * 28 + " suffix", (0, 3), r"(a+)+(?= suffix)", "X", (3, 28, "X")),
			("abc suffix", (3, 3), r"abc$", "X", None),
			("abc suffix", (3, 3), r"abc(?= suffix)", "X", (0, 3, "X")),
			("abc", (1, 1), r"abc", "X", (0, 3, "X")),
			("abc", (3, 3), r"(?=abc)", "X", (0, 0, "X")),
			("", (0, 0), r"^$", "X", (0, 0, "X")),
			("😀abc", (2, 5), r"(?<=😀)(abc)", r"\1!", (2, 5, "abc!")),
			("abc abc", (4, 7), r"abc", "X", (4, 7, "X")),
		)
		for text, selection, pattern, replacement, expected in cases:
			with self.subTest(text=text, selection=selection, pattern=pattern):
				start, end = _WideStringOffsetConverter(text).encodedToStrOffsets(*selection)
				editor = Mock()
				editor.GetValue.return_value = text
				editor.GetSelection.return_value = selection
				editor.GetStringSelection.return_value = text[start:end]
				editor.GetInsertionPoint.return_value = selection[1]
				commands = ManagerEditorCommands(None, editor, Mock(), Mock())
				count = commands._replaceOne(re.compile(pattern), replacement, True)
				self.assertEqual(int(expected is not None), count)
				if expected is None:
					editor.Replace.assert_not_called()
				else:
					editor.Replace.assert_called_once_with(*expected)

	def testSelectedRegexOnLongText(self) -> None:
		text = "a" * 40000
		for selection in ((0, 20000), (10000, 30000)):
			for expression in (r".+?", r".+", r"(a+)+", r"(a+)+(?=a|$)"):
				with self.subTest(selection=selection, expression=expression):
					editor = Mock()
					editor.GetValue.return_value = text
					editor.GetSelection.return_value = selection
					editor.GetInsertionPoint.return_value = selection[1]
					commands = ManagerEditorCommands(None, editor, Mock(), Mock())
					started = perf_counter()
					count = commands._replaceOne(re.compile(expression), "X", True)
					elapsed = perf_counter() - started
					self.assertEqual(1, count)
					editor.Replace.assert_called_once_with(*selection, "X")
					# 正常耗时为毫秒级；宽松限时用于捕获逐次回溯重扫长后缀的回归。
					self.assertLess(elapsed, 1.0)

	def testFindWrapsAcrossCaret(self) -> None:
		for backwards in (False, True):
			for matchCase in (False, True):
				with self.subTest(backwards=backwards, matchCase=matchCase):
					editor = Mock()
					editor.GetValue.return_value = "abc"
					editor.GetSelection.return_value = (1, 1)
					editor.GetStringSelection.return_value = ""
					editor.GetInsertionPoint.return_value = 1
					showInfo = Mock()
					commands = ManagerEditorCommands(None, editor, showInfo, Mock())
					commands._findText = "abc"
					commands._findMatchCase = matchCase
					commands.findNext(backwards=backwards)
					editor.SetSelection.assert_called_once_with(0, 3)
					showInfo.assert_not_called()

	def testFindNavigation(self) -> None:
		cases = (
			("abc abc", "abc", (0, 3), 0, False, True, (4, 7)),
			("abc abc", "abc", (4, 7), 7, False, True, (0, 3)),
			("abc abc", "abc", (4, 7), 7, True, True, (0, 3)),
			("abc abc", "abc", (0, 3), 0, True, True, (4, 7)),
			("aaa", "aa", (1, 3), 3, True, True, (0, 2)),
			("aaa", "aa", (0, 2), 2, False, True, (0, 2)),
			("abc", "abc", (0, 3), 3, True, False, (0, 3)),
			("abc", "abc", (0, 3), 3, False, False, (0, 3)),
			("abc", "abc", (0, 2), 1, False, True, (0, 3)),
			("abc", "abc", (0, 2), 1, True, True, (0, 3)),
			("ABC abc", "abc", (0, 0), 0, False, True, (4, 7)),
			("ABC abc", "abc", (0, 0), 0, False, False, (0, 3)),
			("x a.b", "a.b", (0, 0), 0, False, True, (2, 5)),
			("x a.b", "a.b", (5, 5), 5, True, True, (2, 5)),
			("\U0001f600abc\U0001f600ABC", "abc", (2, 5), 5, False, False, (7, 10)),
			("\U0001f600abc\U0001f600ABC", "abc", (7, 10), 10, True, False, (2, 5)),
			("first\r\nNeedle\nlast", "\nneedle", (0, 0), 0, False, False, (6, 13)),
			("k \u212a", "k", (0, 1), 1, False, False, (2, 3)),
			("abc", "absent", (1, 1), 1, False, False, None),
			("abc", "absent", (1, 1), 1, True, False, None),
		)
		for text, query, selection, caret, backwards, matchCase, expected in cases:
			with self.subTest(text=text, query=query, selection=selection, backwards=backwards):
				start, end = _WideStringOffsetConverter(text).encodedToStrOffsets(*selection)
				editor = Mock()
				editor.GetValue.return_value = text
				editor.GetSelection.return_value = selection
				editor.GetStringSelection.return_value = text[start:end]
				editor.GetInsertionPoint.return_value = caret
				showInfo = Mock()
				commands = ManagerEditorCommands(None, editor, showInfo, Mock())
				commands._findText = query
				commands._findMatchCase = matchCase
				commands.findNext(backwards=backwards)
				if expected is None:
					editor.SetSelection.assert_not_called()
					showInfo.assert_called_once_with("Text not found.")
				else:
					editor.SetSelection.assert_called_once_with(*expected)
					editor.ShowPosition.assert_called_once_with(expected[0])
					editor.SetFocus.assert_called_once()
					showInfo.assert_not_called()

	def testReplacementErrorsUseDialogWithoutChangingText(self) -> None:
		cases = (
			(r"(abc)", r"\g<missing>", True, IndexError, None),
			(r"(abc)", r"\2", True, re.error, None),
			(r"(", "X", True, re.error, None),
			(r"a{4294967296}", "X", True, OverflowError, None),
			("a{" + "9" * 4301 + "}", "X", True, (ValueError, OverflowError), None),
			("(" * 1000 + "a" + ")" * 1000, "X", True, RecursionError, None),
			(r"abc", r"\g<missing>", False, None, r"\g<missing>"),
			(r"(?P<word>abc)", r"\g<word>!", True, None, "abc!"),
		)
		for result in (_NAMESPACE["wx"].ID_APPLY, _NAMESPACE["wx"].ID_OK):
			for pattern, replacement, isRegex, errorType, expected in cases:
				with self.subTest(
					result=result,
					pattern=pattern[:80],
					replacement=replacement,
					isRegex=isRegex,
				):
					editor = Mock()
					editor.GetValue.return_value = "abc"
					editor.GetStringSelection.return_value = "abc"
					editor.GetSelection.return_value = (0, 3)
					editor.GetInsertionPoint.return_value = 3
					dialog = Mock()
					dialog.getValues.return_value = (pattern, replacement, True, isRegex)
					showInfo, showError = Mock(), Mock()
					commands = ManagerEditorCommands(None, editor, showInfo, showError)
					with patch.dict(
						_NAMESPACE,
						_ReplaceDialog=Mock(return_value=dialog),
						displayDialogAsModal=Mock(return_value=result),
					):
						commands.showReplace()
					dialog.Destroy.assert_called_once()
					if errorType is not None:
						showError.assert_called_once()
						self.assertIsInstance(showError.call_args.args[0], errorType)
						showInfo.assert_not_called()
						editor.Replace.assert_not_called()
						editor.SetValue.assert_not_called()
					else:
						showError.assert_not_called()
						showInfo.assert_called_once_with("1 replacement made.")
						if result == _NAMESPACE["wx"].ID_APPLY:
							editor.Replace.assert_called_once_with(0, 3, expected)
						else:
							editor.SetValue.assert_called_once_with(expected)

	def testReplaceAllOptions(self) -> None:
		cases = (
			("abc abc", "abc", "X", True, False, "X X", 2),
			("abc abc", "abc", "", True, False, " ", 2),
			("ABC abc", "abc", "X", True, False, "ABC X", 1),
			("ABC abc", "abc", "X", False, False, "X X", 2),
			("a.b axb", "a.b", r"\1", True, False, r"\1 axb", 1),
			("abc abc", r"(?P<word>abc)", r"\g<word>!", True, True, "abc! abc!", 2),
			("abc", r"(?=.)", "X", True, True, "XaXbXc", 3),
			("", r"^$", "X", True, True, "X", 1),
			("abc", "absent", "X", True, False, None, 0),
		)
		for text, query, replacement, matchCase, isRegex, expected, count in cases:
			with self.subTest(text=text, query=query, isRegex=isRegex, matchCase=matchCase):
				editor = Mock()
				editor.GetValue.return_value = text
				editor.GetStringSelection.return_value = ""
				dialog = Mock()
				dialog.getValues.return_value = (query, replacement, matchCase, isRegex)
				showInfo, showError = Mock(), Mock()
				commands = ManagerEditorCommands(None, editor, showInfo, showError)
				with patch.dict(
					_NAMESPACE,
					_ReplaceDialog=Mock(return_value=dialog),
					displayDialogAsModal=Mock(return_value=_NAMESPACE["wx"].ID_OK),
				):
					commands.showReplace()
				showError.assert_not_called()
				editor.Replace.assert_not_called()
				if count:
					editor.SetValue.assert_called_once_with(expected)
					showInfo.assert_called_once_with(
						f"{count} replacement made." if count == 1 else f"{count} replacements made.",
					)
				else:
					editor.SetValue.assert_not_called()
					showInfo.assert_called_once_with("Text not found.")
				self.assertEqual(query if count and not isRegex else "", commands._findText)

	def testCancelledAndEmptyDialogsPreserveEditor(self) -> None:
		for action in ("showFind", "showReplace"):
			for result in (_NAMESPACE["wx"].ID_CANCEL, _NAMESPACE["wx"].ID_OK):
				with self.subTest(action=action, result=result):
					editor = Mock()
					editor.GetStringSelection.return_value = ""
					dialog = Mock()
					dialog.getValues.return_value = (
						("", False, False) if action == "showFind" else ("", "X", False, False)
					)
					showInfo, showError = Mock(), Mock()
					commands = ManagerEditorCommands(None, editor, showInfo, showError)
					commands._findText = "previous"
					with patch.dict(
						_NAMESPACE,
						_FindDialog=Mock(return_value=dialog),
						_ReplaceDialog=Mock(return_value=dialog),
						displayDialogAsModal=Mock(return_value=result),
					):
						getattr(commands, action)()
					dialog.Destroy.assert_called_once()
					editor.Replace.assert_not_called()
					editor.SetValue.assert_not_called()
					editor.SetSelection.assert_not_called()
					showError.assert_not_called()
					self.assertEqual("previous", commands._findText)
					if result == _NAMESPACE["wx"].ID_CANCEL:
						showInfo.assert_not_called()
					else:
						showInfo.assert_called_once_with(
							"Enter text to find." if action == "showFind" else "Enter text to replace.",
						)


if __name__ == "__main__":
	unittest.main()
