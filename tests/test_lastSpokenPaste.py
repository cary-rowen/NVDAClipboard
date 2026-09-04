# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for temporary text paste helpers without loading NVDA."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import unittest

from tests.test_clipboardMonitor import clipboardMonitor


_CONTROLLER_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "controller.py"


def _loadTemporaryPasteHelpers() -> tuple[type, object, object]:
	"""Load temporary paste helpers without importing controller dependencies."""
	tree = ast.parse(_CONTROLLER_PATH.read_text(encoding="utf-8"))
	nodes = [
		node
		for node in tree.body
		if (
			isinstance(node, ast.ClassDef)
			and node.name == "_TemporaryTextPasteState"
			or isinstance(node, ast.FunctionDef)
			and node.name
			in {
				"_getTextOrCharacterCount",
				"_normalizeClipboardLineEndings",
				"_isTemporaryTextSnapshot",
			}
		)
	]
	namespace = {
		"dataclass": dataclass,
		"ClipboardContentType": clipboardMonitor.ClipboardContentType,
		"ClipboardSnapshot": clipboardMonitor.ClipboardSnapshot,
		"ngettext": lambda singular, plural, count: singular if count == 1 else plural,
	}
	exec(compile(ast.Module(body=nodes, type_ignores=[]), _CONTROLLER_PATH, "exec"), namespace)
	return (
		namespace["_TemporaryTextPasteState"],
		namespace["_isTemporaryTextSnapshot"],
		namespace["_getTextOrCharacterCount"],
	)


_TemporaryTextPasteState, _isTemporaryTextSnapshot, _getTextOrCharacterCount = _loadTemporaryPasteHelpers()


class TemporaryTextPasteTests(unittest.TestCase):
	"""Verify temporary text paste matching."""

	def testTextFeedbackUsesConfiguredCharacterLimit(self) -> None:
		"""Replace lengthy selection and paste feedback with a character count."""
		for maxLength in (512, 1024):
			with self.subTest(maxLength=maxLength):
				shortText = "x" * (maxLength - 1)
				self.assertEqual(shortText, _getTextOrCharacterCount(shortText, maxLength))
				self.assertEqual(
					f"{maxLength} characters",
					_getTextOrCharacterCount("x" * maxLength, maxLength),
				)

	def testTemporaryTextMatchesWin32ClipboardLineEndings(self) -> None:
		"""Accept text after CF_UNICODETEXT normalizes line endings to CRLF."""
		state = _TemporaryTextPasteState(
			SimpleNamespace(),
			1,
			"first\nsecond",
		)
		snapshot = clipboardMonitor.ClipboardSnapshot(
			clipboardMonitor.ClipboardContentType.TEXT,
			sequenceNumber=1,
			text="first\r\nsecond",
			canIncludeInHistory=False,
			canUpload=False,
		)

		self.assertTrue(_isTemporaryTextSnapshot(snapshot, state))


if __name__ == "__main__":
	unittest.main()
