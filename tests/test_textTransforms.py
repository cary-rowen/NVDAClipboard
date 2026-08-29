"""Tests for clipboard manager text transforms without loading NVDA."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tests._module_loader import loadAddonModule


_MODULE_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "textTransforms.py"


class _IdentityOffsetConverter:
	"""Provide the offset conversion API needed by the transform wrapper test."""

	def __init__(self, _text: str) -> None:
		"""Accept the editor text without using it."""

	def encodedToStrOffsets(self, *offsets: int) -> tuple[int, ...]:
		"""Return the encoded offsets unchanged."""
		return offsets

	def strToEncodedOffsets(self, *offsets: int) -> tuple[int, ...]:
		"""Return the string offsets unchanged."""
		return offsets


textTransforms = loadAddonModule(
	"nvdaClipboardTextTransforms",
	_MODULE_PATH,
	injectedModules={"textUtils": SimpleNamespace(WideStringOffsetConverter=_IdentityOffsetConverter)},
)


class TextTransformTests(unittest.TestCase):
	"""Verify the clipboard manager's common text cleanup operations."""

	def testLineTransformsPreserveContentAndTrailingNewlines(self) -> None:
		"""Clean up and sort lines while keeping stable order and trailing breaks."""
		self.assertEqual("a\nb\nc\n", textTransforms.trimTrailingSpaces("a \r\nb\t\nc \n"))
		self.assertEqual("a\nb\nc\n", textTransforms.trimLeadingSpaces(" a\r\n\tb\nc\n"))
		self.assertEqual("a\nb\nc\n", textTransforms.trimLeadingAndTrailingSpaces(" a \r\n\tb\t\n c \n"))
		self.assertEqual("a b c", textTransforms.replaceLineBreaksWithSpaces("a\r\nb\nc"))
		self.assertEqual("a\nb\na\n", textTransforms.removeConsecutiveDuplicateLines("a\na\nb\nb\na\n"))
		self.assertEqual("a\nb\nc\n", textTransforms.removeDuplicateLines("a\nb\na\nc\nb\n"))
		self.assertEqual("c\nbb\naa\n", textTransforms.sortLinesByLengthAscending("bb\naa\nc\n"))
		self.assertEqual("bb\naa\nc\n", textTransforms.sortLinesByLengthDescending("bb\naa\nc\n"))

	def testApplyEditorTextTransformExpandsTheSelectionToWholeLines(self) -> None:
		"""Expand a partial selection to full lines before applying a cleanup."""
		editor = Mock()
		editor.GetValue.return_value = "alpha\n  beta  \ngamma\n"
		editor.GetSelection.return_value = (8, 10)
		editor.SetValue = Mock()
		editor.SetSelection = Mock()
		editor.SetInsertionPoint = Mock()
		editor.ShowPosition = Mock()
		editor.SetFocus = Mock()

		changed = textTransforms.applyEditorTextTransform(
			editor,
			textTransforms.trimLeadingAndTrailingSpaces,
			lineWise=True,
		)

		self.assertTrue(changed)
		editor.SetValue.assert_called_once_with("alpha\nbeta\ngamma\n")
		editor.SetSelection.assert_called_once()
		editor.SetInsertionPoint.assert_not_called()
		editor.ShowPosition.assert_called_once_with(6)
		editor.SetFocus.assert_called_once_with()


if __name__ == "__main__":
	unittest.main()
