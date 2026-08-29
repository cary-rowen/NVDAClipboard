"""Tests for clipboard manager text transforms without loading NVDA."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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
		self.assertEqual("a\nb\n", textTransforms.removeBlankLines("a\n\n \n\t\nb\n"))
		self.assertEqual("", textTransforms.removeBlankLines("\n \n\t\n"))
		self.assertEqual("a\n\nb\n\n", textTransforms.removeConsecutiveBlankLines("a\n\n \n\t\nb\n\n"))
		self.assertEqual("a\nb\na\n", textTransforms.removeConsecutiveDuplicateLines("a\na\nb\nb\na\n"))
		self.assertEqual("a\nb\nc\n", textTransforms.removeDuplicateLines("a\nb\na\nc\nb\n"))
		self.assertEqual("c\nbb\naa\n", textTransforms.sortLinesByLengthAscending("bb\naa\nc\n"))
		self.assertEqual("bb\naa\nc\n", textTransforms.sortLinesByLengthDescending("bb\naa\nc\n"))

	def testApplyEditorTextTransformExpandsTheSelectionToWholeLines(self) -> None:
		"""Expand a partial selection to full lines before applying a cleanup."""
		editor = Mock()
		editor.GetValue.side_effect = ["alpha\n  beta  \ngamma\n", "alpha\nbeta\ngamma\n"]
		editor.GetSelection.return_value = (8, 10)
		editor.Replace = Mock()
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
		editor.Replace.assert_called_once_with(6, 15, "beta\n")
		editor.SetValue.assert_not_called()
		editor.SetSelection.assert_called_once()
		editor.SetInsertionPoint.assert_not_called()
		editor.ShowPosition.assert_called_once_with(6)
		editor.SetFocus.assert_called_once_with()

	def testApplyEditorTextTransformUsesControlEndForFullBuffer(self) -> None:
		"""Use wx control positions when replacing the whole editor content."""
		editor = Mock()
		editor.GetValue.side_effect = ["a\nb\nc", "a b c"]
		editor.GetSelection.return_value = (0, 0)
		editor.GetLastPosition.return_value = 7
		editor.SetSelection = Mock()
		editor.SetInsertionPoint = Mock()
		editor.ShowPosition = Mock()
		editor.SetFocus = Mock()

		with patch.object(textTransforms, "_replaceEditorRange") as replaceEditorRange:
			changed = textTransforms.applyEditorTextTransform(
				editor,
				textTransforms.replaceLineBreaksWithSpaces,
				lineWise=True,
			)

		self.assertTrue(changed)
		replaceEditorRange.assert_called_once_with(editor, 0, 7, "a b c")
		editor.SetInsertionPoint.assert_called_once_with(0)
		editor.SetSelection.assert_not_called()


if __name__ == "__main__":
	unittest.main()
