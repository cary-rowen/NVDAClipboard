"""Tests for standalone clipboard navigation unit boundaries."""

from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from tests._module_loader import loadAddonModule


_MODULE_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "navigationUnits.py"
navigationUnits = loadAddonModule("nvdaClipboardNavigationUnits", _MODULE_PATH)


class NavigationUnitOffsetTests(unittest.TestCase):
	"""Verify configurable navigation unit boundaries without loading NVDA."""

	def testCamelCaseBoundaries(self) -> None:
		"""Split common camel-case forms while preserving surrounding content."""
		cases = {
			"getWord,": ((0, 3), (3, 8)),
			"call getWord now.": ((0, 5), (5, 8), (8, 13), (13, 17)),
			"call cafe\u0301Value now.": ((0, 5), (5, 10), (10, 16), (16, 20)),
			"getǅuro": ((0, 3), (3, 7)),
			"XMLHttpRequest": ((0, 3), (3, 7), (7, 14)),
			"HTTP": ((0, 4),),
			"version2Value": ((0, 8), (8, 13)),
			"überValue": ((0, 4), (4, 9)),
		}
		for text, expectedUnits in cases.items():
			with self.subTest(text=text):
				for expected in expectedUnits:
					for offset in range(*expected):
						self.assertEqual(
							expected,
							navigationUnits.getCamelCaseUnitOffsets(text, offset),
						)

	def testPunctuationRunsAttachToPrecedingText(self) -> None:
		"""Keep consecutive punctuation with the preceding text by default."""
		text = "你好，世界？！"
		for offset in (0, 1, 2):
			self.assertEqual(
				(0, 3),
				navigationUnits.getPunctuationUnitOffsets(text, offset, separatePunctuation=False),
			)
		for offset in (3, 4, 5, 6):
			self.assertEqual(
				(3, 7),
				navigationUnits.getPunctuationUnitOffsets(text, offset, separatePunctuation=False),
			)

	def testEachPunctuationCharacterCanBeSeparate(self) -> None:
		"""Expose every punctuation character as an individual unit when requested."""
		text = "你好，世界？！"
		expectedOffsets = {
			0: (0, 2),
			1: (0, 2),
			2: (2, 3),
			3: (3, 5),
			4: (3, 5),
			5: (5, 6),
			6: (6, 7),
		}
		for offset, expected in expectedOffsets.items():
			self.assertEqual(
				expected,
				navigationUnits.getPunctuationUnitOffsets(text, offset, separatePunctuation=True),
			)

	def testUnicodeSymbolsDoNotSplitPunctuationUnits(self) -> None:
		"""Distinguish Unicode punctuation from mathematical symbols."""
		text = "a+b。c"
		self.assertEqual(
			(0, 4),
			navigationUnits.getPunctuationUnitOffsets(text, 1, separatePunctuation=False),
		)
		self.assertEqual(
			(4, 5),
			navigationUnits.getPunctuationUnitOffsets(text, 4, separatePunctuation=False),
		)

	def testWhitespaceStaysWithAdjacentText(self) -> None:
		"""Avoid creating a separate whitespace unit around punctuation."""
		text = "first, second"
		self.assertEqual(
			(5, 7),
			navigationUnits.getPunctuationUnitOffsets(text, 6, separatePunctuation=True),
		)
		self.assertEqual(
			(7, len(text)),
			navigationUnits.getPunctuationUnitOffsets(text, 7, separatePunctuation=True),
		)

	def testAttachedModeAvoidsPunctuationOnlyUnits(self) -> None:
		"""Attach leading and trailing delimiter clusters when text is available."""
		text = "“你好”， 世界！  "
		self.assertEqual(
			(0, 6),
			navigationUnits.getPunctuationUnitOffsets(text, 0, separatePunctuation=False),
		)
		self.assertEqual(
			(6, len(text)),
			navigationUnits.getPunctuationUnitOffsets(text, 9, separatePunctuation=False),
		)

	def testOpeningPunctuationAfterWhitespaceStartsNextUnit(self) -> None:
		"""Keep opening punctuation with the text after delimiter whitespace."""
		text = 'first. "Second"'
		self.assertEqual(
			(0, 7),
			navigationUnits.getPunctuationUnitOffsets(text, 0, separatePunctuation=False),
		)
		self.assertEqual(
			(7, len(text)),
			navigationUnits.getPunctuationUnitOffsets(text, 7, separatePunctuation=False),
		)

	def testLookupNearEndDoesNotScanFromLineStart(self) -> None:
		"""Limit punctuation checks to the unit surrounding a late offset."""
		text = "a," * 1000
		for separatePunctuation, expectedStart in ((False, len(text) - 2), (True, len(text) - 1)):
			with self.subTest(separatePunctuation=separatePunctuation):
				with patch.object(
					navigationUnits,
					"_isPunctuation",
					wraps=navigationUnits._isPunctuation,
				) as isPunctuation:
					self.assertEqual(
						(expectedStart, len(text)),
						navigationUnits.getPunctuationUnitOffsets(
							text,
							len(text) - 1,
							separatePunctuation=separatePunctuation,
						),
					)
				self.assertLess(isPunctuation.call_count, 20)

	def testPastEndOffsetAlwaysAdvances(self) -> None:
		"""Return an empty advancing range at or beyond the text end."""
		self.assertEqual((3, 4), navigationUnits.getCamelCaseUnitOffsets("abc", 3))
		self.assertEqual(
			(3, 4),
			navigationUnits.getPunctuationUnitOffsets("abc", 3, separatePunctuation=False),
		)

	def testNegativeOffsetIsRejected(self) -> None:
		"""Reject an invalid negative offset explicitly."""
		with self.assertRaises(ValueError):
			navigationUnits.getCamelCaseUnitOffsets("abc", -1)
		with self.assertRaises(ValueError):
			navigationUnits.getPunctuationUnitOffsets("abc", -1, separatePunctuation=False)
