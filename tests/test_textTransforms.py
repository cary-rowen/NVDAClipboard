# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for clipboard manager text transforms without loading NVDA."""

from __future__ import annotations

from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
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


class _WordSegFlag:
	"""Provide the Chinese word segmentation flag used by the transform."""

	CHINESE = object()


class _WordSegmenter:
	"""Record NVDA word segmenter calls and return predictable segmented text."""

	def __init__(self, text: str, *, wordSegFlag: object) -> None:
		"""Store constructor arguments for later assertions."""
		if wordSegFlag is not _WordSegFlag.CHINESE:
			raise AssertionError("Chinese word segmentation must use WordSegFlag.CHINESE")
		self.text = text
		self.wordSegFlag = wordSegFlag

	def segmentedText(self, sep: str = " ", newSepIndex: list[int] | None = None) -> str:
		"""Return text separated with the requested separator."""
		return sep.join((self.text, "segmented"))


class _ChineseWordSegmentationStrategy:
	"""Record forced initialization of NVDA's Chinese segmenter."""

	forceInitCalls: list[bool] = []

	@classmethod
	def _initCppJieba(cls, forceInit: bool = False) -> None:
		"""Record whether cppjieba was force-initialized."""
		cls.forceInitCalls.append(forceInit)


def _makeTextUtilsPackage() -> dict[str, ModuleType]:
	"""Create the subset of NVDA textUtils modules needed for segmentation tests."""
	_ChineseWordSegmentationStrategy.forceInitCalls = []
	textUtilsModule = ModuleType("textUtils")
	textUtilsModule.__path__ = []
	textUtilsModule.WideStringOffsetConverter = _IdentityOffsetConverter
	wordSegPackage = ModuleType("textUtils._wordSeg")
	wordSegPackage.__path__ = []
	wordSegStrategyModule = ModuleType("textUtils._wordSeg.wordSegStrategy")
	wordSegStrategyModule.ChineseWordSegmentationStrategy = _ChineseWordSegmentationStrategy
	wordSegPackage.wordSegStrategy = wordSegStrategyModule
	wordSegmenterModule = ModuleType("textUtils._wordSeg.wordSegmenter")
	wordSegmenterModule.WordSegmenter = _WordSegmenter
	segFlagModule = ModuleType("textUtils.segFlag")
	segFlagModule.WordSegFlag = _WordSegFlag
	return {
		"textUtils": textUtilsModule,
		"textUtils._wordSeg": wordSegPackage,
		"textUtils._wordSeg.wordSegStrategy": wordSegStrategyModule,
		"textUtils._wordSeg.wordSegmenter": wordSegmenterModule,
		"textUtils.segFlag": segFlagModule,
	}


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
		self.assertEqual("a\n\nb", textTransforms.removeConsecutiveBlankLines("a\n \n\t\nb"))
		self.assertEqual("a\nb\na\n", textTransforms.removeConsecutiveDuplicateLines("a\na\nb\nb\na\n"))
		self.assertEqual("a\nb\nc\n", textTransforms.removeDuplicateLines("a\nb\na\nc\nb\n"))
		self.assertEqual("c\nbb\naa\n", textTransforms.sortLinesByLengthAscending("bb\naa\nc\n"))
		self.assertEqual("bb\naa\nc\n", textTransforms.sortLinesByLengthDescending("bb\naa\nc\n"))

	def testSegmentChineseWordsUsesNvdaWordSegmenter(self) -> None:
		"""Segment Chinese text through NVDA's word segmentation API."""
		with patch.dict(sys.modules, _makeTextUtilsPackage()):
			self.assertEqual("中文文本 segmented", textTransforms.segmentChineseWords("中文文本"))
		self.assertEqual([True], _ChineseWordSegmentationStrategy.forceInitCalls)

	def testApplyEditorTextTransformExpandsTheSelectionToWholeLines(self) -> None:
		"""Expand a partial selection to full lines before applying a cleanup."""
		editor = Mock()
		editor.GetValue.return_value = "alpha\n  beta  \ngamma\n"
		editor.GetSelection.return_value = (8, 10)
		editor.GetInsertionPoint.return_value = 11
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
		editor.SetSelection.assert_called_once_with(6, 11)
		editor.SetInsertionPoint.assert_not_called()
		editor.ShowPosition.assert_called_once_with(6)
		editor.SetFocus.assert_called_once_with()

	def testApplyEditorTextTransformUsesControlEndForFullBuffer(self) -> None:
		"""Use wx control positions when replacing the whole editor content."""
		editor = Mock()
		editor.GetValue.return_value = "a\nb\nc"
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

	def testWin32ReplacementUsesNativeLineBreaks(self) -> None:
		"""Send CRLF line breaks to the native Windows edit control."""
		editor = Mock()
		editor.GetHandle.return_value = 100
		user32 = Mock()
		user32.IsWindow.return_value = True

		with patch.object(textTransforms.ctypes, "windll", SimpleNamespace(user32=user32), create=True):
			replaced = textTransforms._replaceEditorRangeWithWin32(editor, 0, 7, "a\nb\n")

		self.assertTrue(replaced)
		user32.SendMessageW.assert_any_call(100, textTransforms._EM_SETSEL, 0, 7)
		replaceCall = user32.SendMessageW.call_args_list[-1]
		self.assertEqual((100, textTransforms._EM_REPLACESEL, True), replaceCall.args[:3])
		self.assertEqual("a\r\nb\r\n", replaceCall.args[3].value)


if __name__ == "__main__":
	unittest.main()
