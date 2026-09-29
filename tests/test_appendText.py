# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for shared clipboard append behavior without loading NVDA."""

import ast
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock

from tests.test_clipboardMonitor import clipboardMonitor


class AppendTextTests(unittest.TestCase):
	def testBothCommandsAppendTextOrReplaceNonText(self) -> None:
		path = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "controller.py"
		tree = ast.parse(path.read_text(encoding="utf-8"))
		controllerClass = next(
			node
			for node in tree.body
			if isinstance(node, ast.ClassDef) and node.name == "ClipboardController"
		)
		commands = ("appendSelectedText", "appendLastSpokenText")
		methods = [
			node
			for node in controllerClass.body
			if isinstance(node, ast.FunctionDef) and node.name in (*commands, "_appendTextToClipboard")
		]
		contentTypes = clipboardMonitor.ClipboardContentType
		source = SimpleNamespace(APPEND_TEXT=object())
		namespace = {
			"ClipboardContentType": contentTypes,
			"_ClipboardChangeSource": source,
			"_getTextOrCharacterCount": lambda text: text,
			"_": lambda text: text,
			"ui": Mock(),
		}
		exec(compile(ast.Module(body=methods, type_ignores=[]), str(path), "exec"), namespace)
		cases = (
			(contentTypes.TEXT, "old", "old\nnew"),
			(contentTypes.TEXT, "old\n", "old\nnew"),
			(contentTypes.TEXT, "old\r\n", "old\r\nnew"),
			(contentTypes.FORMATTED_TEXT, "old", "old\nnew"),
			(contentTypes.TEXT_AND_IMAGE, "old", "old\nnew"),
			(contentTypes.EMPTY, "", "new"),
			(contentTypes.FILES, "", "new"),
			(contentTypes.IMAGE, "", "new"),
			(contentTypes.PROTECTED, "", "new"),
			(contentTypes.UNSUPPORTED, "", "new"),
			(contentTypes.ERROR, "", None),
		)
		for command in commands:
			for contentType, oldText, expectedText in cases:
				with self.subTest(command=command, contentType=contentType, oldText=oldText):
					snapshot = clipboardMonitor.ClipboardSnapshot(
						contentType,
						text=oldText,
						sequenceNumber=7,
						canIncludeInHistory=False,
						canUpload=False,
					)
					controller = SimpleNamespace(
						monitor=SimpleNamespace(readNow=Mock(return_value=snapshot)),
						_lastAppliedSequenceNumber=7,
						_requireLastSpokenText=lambda: "new",
						_getSelectionText=lambda: "new",
						_writeClipboardText=Mock(),
					)
					controller._appendTextToClipboard = MethodType(
						namespace["_appendTextToClipboard"], controller
					)
					if expectedText is None:
						with self.assertRaisesRegex(RuntimeError, "Could not read the current clipboard"):
							namespace[command](controller)
						controller._writeClipboardText.assert_not_called()
					else:
						namespace[command](controller)
						controller._writeClipboardText.assert_called_once_with(
							expectedText,
							source.APPEND_TEXT,
							expectedSequenceNumber=7,
							canIncludeInHistory=False,
							canUpload=False,
						)


if __name__ == "__main__":
	unittest.main()
