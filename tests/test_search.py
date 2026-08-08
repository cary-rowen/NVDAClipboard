"""Tests for standalone clipboard manager search matching."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


_MODULE_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "search.py"
_SPEC = importlib.util.spec_from_file_location("nvdaClipboardSearch", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
search = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(search)


class SearchTests(unittest.TestCase):
	"""Verify precise Unicode matching without loading NVDA."""

	def testPreviousLiteralMatch(self) -> None:
		"""Find the nearest previous literal across case, Unicode, and chunk boundaries."""
		text = "Needle needle NEEDLE"
		self.assertEqual((7, 13), search.findPreviousLiteralMatch(text, "needle", len(text), matchCase=True))
		self.assertEqual(
			(14, 20),
			search.findPreviousLiteralMatch(text, "needle", len(text), matchCase=False),
		)
		self.assertEqual((7, 13), search.findPreviousLiteralMatch(text, "needle", 14, matchCase=False))
		self.assertEqual((14, 20), search.findPreviousLiteralMatch(text, "needle", 0, matchCase=False))
		self.assertEqual((1, 3), search.findPreviousLiteralMatch("aaa", "aa", 3, matchCase=True))
		self.assertEqual((1, 3), search.findPreviousLiteralMatch("aaa", "AA", 3, matchCase=False))
		self.assertEqual(
			(0, 2),
			search.findPreviousLiteralMatch("aaa", "aa", 1, matchCase=True, currentMatch=True),
		)
		self.assertEqual(
			(1, 3),
			search.findPreviousLiteralMatch("aaa", "AA", 0, matchCase=False, currentMatch=True),
		)
		self.assertEqual(
			(6, 13),
			search.findPreviousLiteralMatch("first\r\nNeedle\nlast", "\nneedle", 19, matchCase=False),
		)
		self.assertEqual(
			(8, 14),
			search.findPreviousLiteralMatch("😀needle😀NEEDLE", "needle", 14, matchCase=False),
		)
		self.assertEqual((2, 3), search.findPreviousLiteralMatch("k K", "k", 3, matchCase=False))
		boundaryStart = search._BACKWARD_SEARCH_CHUNK_SIZE - 2
		boundaryText = "x" * boundaryStart + "NeEdLe" + "x" * (search._BACKWARD_SEARCH_CHUNK_SIZE - 3)
		self.assertEqual(
			(boundaryStart, boundaryStart + 6),
			search.findPreviousLiteralMatch(boundaryText, "needle", len(boundaryText), matchCase=False),
		)
		self.assertIsNone(search.findPreviousLiteralMatch(text, "absent", len(text), matchCase=False))

	def testCompatibilityNormalizationAndCaseFolding(self) -> None:
		"""Match Chinese, full-width Latin text, and German sharp s exactly."""
		self.assertEqual("中文 strasse abc", search.normalizeSearchText("中文 Straße ＡＢＣ"))

	def testKeywordsAreWhitespaceSplitAndDeduplicated(self) -> None:
		"""Keep first keyword order while preserving paths and code symbols literally."""
		query = search.normalizeSearchText("  文件\tC:\\Temp\\a.py  文件\n++  ")
		self.assertEqual(("文件", "c:\\temp\\a.py", "++"), search.splitSearchKeywords(query))

	def testLiteralMatchingUsesFullContentAndMultipleFields(self) -> None:
		"""Find late text and file paths across fields without fuzzy matching."""
		records = (
			(7, search.normalizeSearchText("x" * 200 + " 深处 Needle")),
			(3, search.normalizeSearchText(r"C:\one\first.txt C:\two\second.txt C:\three\target.py")),
			(9, search.normalizeSearchText("needle at the front")),
		)
		keywords = search.splitSearchKeywords(search.normalizeSearchText("NEEDLE"))
		self.assertEqual(
			[7, 9],
			[key for key, text in records if search.matchesSearchKeywords((text,), keywords)],
		)
		pathKeywords = search.splitSearchKeywords(search.normalizeSearchText(r"target.py C:\three"))
		self.assertTrue(
			search.matchesSearchKeywords(
				(
					search.normalizeSearchText("third file"),
					records[1][1],
				),
				(*pathKeywords, search.normalizeSearchText("third")),
			),
		)
		pathKeywords = search.splitSearchKeywords(search.normalizeSearchText(r"target.py C:\missing"))
		self.assertFalse(search.matchesSearchKeywords((records[1][1],), pathKeywords))
		self.assertFalse(
			search.matchesSearchKeywords(
				(records[2][1],),
				search.splitSearchKeywords(search.normalizeSearchText("needel")),
			),
		)


if __name__ == "__main__":
	unittest.main()
