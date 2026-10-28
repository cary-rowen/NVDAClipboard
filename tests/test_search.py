# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for standalone clipboard manager search matching."""

from __future__ import annotations

from importlib import import_module
import inspect
import re
import tracemalloc
from types import ModuleType, SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock

from tests._module_loader import createAddonPackage, loadAddonModule


_PACKAGE_NAME = "nvdaClipboardSearchTests"
createAddonPackage(_PACKAGE_NAME)
search = loadAddonModule(
	f"{_PACKAGE_NAME}.textSearch",
	injectedModules={"logHandler": SimpleNamespace(log=Mock())},
)


def _loadSearchWithoutPrivateRegexApi(
	packageName: str, privateModules: dict[str, object]
) -> tuple[Any, Mock]:
	"""Load a fresh search module while the given ``re`` private modules are replaced, plus its log."""
	createAddonPackage(packageName)
	log = Mock()
	module = loadAddonModule(
		f"{packageName}.textSearch",
		injectedModules={"logHandler": SimpleNamespace(log=log), **privateModules},
	)
	return module, log


class SearchTests(unittest.TestCase):
	"""Verify precise Unicode matching without loading NVDA."""

	def testRegexSelectionPreservesContextAndGroups(self) -> None:
		cases = (
			("abc suffix", (0, 3), r"(?=(abc suffix))abc", ("abc suffix",)),
			("abab suffix", (0, 3), r"(?=a)(ab)\1", None),
			("abab suffix", (0, 4), r"(?=(ab))(?:\1)+?", ("ab",)),
			("abc", (0, 2), r"(?=a)(a)?(?(1)b|c)", ("a",)),
			("c suffix", (0, 1), r"(?=c)(a)?(?(1)b|c)", (None,)),
			("a suffix", (0, 1), r"(?=a)(a)?(?(1)b)", None),
			("ab suffix", (0, 2), r"(?=a)(?:a|ab)", ()),
			("abc suffix", (0, 3), r"(?=a)(?>.+)", ()),
			("abc suffix", (0, 3), r"(?=a).++", ()),
			("a" * 28, (0, 3), r"(a+)+(?=a)", ("aaa",)),
			("a" * 28, (0, 3), r"a{1,9}+(?=a)", ()),
			("xxabcZZ", (2, 5), r"(?<=([a-z]{2}))([a-z]+?)", ("xx", "abc")),
			("aAx", (0, 2), r"(?i)(a)\1(?=x)", ("a",)),
			("ABC suffix", (0, 3), r"(?i:(abc))(?= suffix)", ("ABC",)),
			("abc suffix", (0, 3), r"(?=abc)", None),
			("a" * 10, (0, 5), r"(?=a)(a+)(a+)", ("aaaa", "a")),
			("a" * 10, (0, 5), r"(?=a)(a+?)(a+?)", ("a", "aaaa")),
			("zzaaaaa suffix", (0, 5), r"(?=z)zz(a+)", ("aaa",)),
			("zzaaaaa suffix", (0, 5), r"(?=z)zza++", ()),
			("zzaaaaa suffix", (0, 5), r"(?=z)zz(a{4,10})", None),
			("zzaaaaa suffix", (0, 5), r"(?=z)zz(a{7,10})", None),
		)
		for text, (start, end), expression, groups in cases:
			with self.subTest(text=text, start=start, end=end, expression=expression):
				match = search.matchRegexSelection(re.compile(expression), text, start, end)
				if groups is None:
					self.assertIsNone(match)
				else:
					self.assertIsNotNone(match)
					self.assertEqual((start, end), match.span())
					self.assertEqual(groups, match.groups())

	def testSelectionAtDocumentEndUsesNativePattern(self) -> None:
		text = "a" * 3000000
		for value, start, expression in ((text, 0, r"^a+$"), ("x" + text, 1, r"(?<=x)a+$")):
			with self.subTest(start=start, expression=expression):
				pattern = re.compile(expression)
				match = search.matchRegexSelection(pattern, value, start, len(value))
				self.assertIsNotNone(match)
				self.assertIs(pattern, match.re)
				self.assertEqual((start, len(value)), match.span())

	def testLargePartialSelectionKeepsRepeatMemoryBounded(self) -> None:
		text = "a" * 1000000 + "\n"
		for expression in (r"^a+$", r"^a+?$", r"^a?a+$", r"^a?a+?$", r"^(?i:a)+$"):
			with self.subTest(expression=expression):
				pattern = re.compile(expression)
				tracemalloc.start()
				try:
					match = search.matchRegexSelection(pattern, text, 0, len(text) - 1)
					peakMemory = tracemalloc.get_traced_memory()[1]
				finally:
					tracemalloc.stop()
				self.assertIsNotNone(match)
				self.assertEqual((0, len(text) - 1), match.span())
				self.assertLess(peakMemory, 8 * 1024 * 1024)

	def testRegexSelectionMatchesFullmatchBeforeFinalNewline(self) -> None:
		patterns = (
			r".*?",
			r"(a+)+",
			r"(?m)^.*$",
			r"(?s).+",
			r"\b\w+\b",
			r"\B.*\B",
			r"(?<=a).*",
			r"(?<!b).*",
			r"(?!b).*",
			r"(?=(.*)).*",
			r"(?=a)(a|ab)*",
			r"(?=a)(?>a+)",
			r"(?=a)a++",
			r"(?=a)(a)?(?(1)b|c)",
			r"(?=a)(a)?(?(1)b)",
			r"(?i)(a)\1$",
			r"(?=a)[^b]+",
			r"(?=a)(?:a{1,2})+",
		)
		for text in ("a", "b", "aa", "aA", "ab", "aba", "abc", "a\na", "x a", "😀abc"):
			for expression in patterns:
				pattern = re.compile(expression)
				for start in range(len(text) + 1):
					with self.subTest(text=text, expression=expression, start=start):
						expected = pattern.fullmatch(text, start)
						actual = search.matchRegexSelection(pattern, text + "\n", start, len(text))
						self.assertEqual(expected is None, actual is None)
						if expected is not None:
							self.assertEqual(expected.span(), actual.span())
							self.assertEqual(expected.groups(), actual.groups())

	def testPrivateRegexApiIsAvailableOnRunningPython(self) -> None:
		"""Fail loudly when a Python upgrade removes a private ``re`` name used by selection matching."""
		usedNames = {
			moduleName: set(
				re.findall(rf"\b{moduleName}\.(\w+)", inspect.getsource(search._matchWithPrivateRegexApi))
			)
			for moduleName in ("_parser", "_compiler")
		}
		self.assertTrue(usedNames["_parser"])
		self.assertTrue(usedNames["_compiler"])
		self.assertLessEqual(usedNames["_parser"], set(search.PRIVATE_PARSER_NAMES))
		self.assertLessEqual(usedNames["_compiler"], set(search.PRIVATE_COMPILER_NAMES))
		for moduleName, names in (
			("re._parser", search.PRIVATE_PARSER_NAMES),
			("re._compiler", search.PRIVATE_COMPILER_NAMES),
		):
			module = import_module(moduleName)
			for name in names:
				with self.subTest(module=module.__name__, name=name):
					self.assertTrue(hasattr(module, name))
		self.assertIsNone(search._privateRegexError)
		self.assertIsNotNone(search._privateRegexModules)

	def testRegexSelectionFallsBackWithoutPrivateRegexApi(self) -> None:
		"""Match the bare selection and log once when a private ``re`` module or name is missing."""
		compilerWithoutSimple = ModuleType("re._compiler")
		setattr(compilerWithoutSimple, "compile", Mock())
		for packageName, privateModules, errorType in (
			("nvdaClipboardSearchNoParserTests", {"re._parser": None}, ImportError),
			("nvdaClipboardSearchOldCompilerTests", {"re._compiler": compilerWithoutSimple}, AttributeError),
		):
			with self.subTest(errorType=errorType.__name__):
				fallbackSearch, log = _loadSearchWithoutPrivateRegexApi(packageName, privateModules)
				self.assertIsNone(fallbackSearch._privateRegexModules)
				self.assertIsInstance(fallbackSearch._privateRegexError, errorType)
				self._assertFallbackMatches(fallbackSearch, log)

	def _assertFallbackMatches(self, module: Any, log: Mock) -> None:
		"""Check that ``module`` matches selections with ``fullmatch`` and logs the fallback exactly once."""
		text = "abc suffix"
		match = module.matchRegexSelection(re.compile(r"a(b)c"), text, 0, 3)
		self.assertIsNotNone(match)
		self.assertEqual((0, 3), match.span())
		self.assertEqual(("b",), match.groups())
		self.assertIsNone(module.matchRegexSelection(re.compile(r"(?=abc suffix)abc"), text, 0, 3))
		self.assertIsNotNone(module.matchRegexSelection(re.compile(r"abc suffix"), text, 0, len(text)))
		log.debugWarning.assert_called_once()

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
		for matchCase in (False, True):
			self.assertEqual(
				(0, 3),
				search.findPreviousLiteralMatch("abc", "abc", 1, matchCase=matchCase),
			)

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
