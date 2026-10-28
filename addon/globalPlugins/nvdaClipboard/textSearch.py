# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Provide literal search and regular-expression selection matching."""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module
import re
from types import ModuleType
from typing import Any
import unicodedata

from logHandler import log


_BACKWARD_SEARCH_CHUNK_SIZE = 64 * 1024
PRIVATE_PARSER_NAMES = (
	"parse",
	"MIN_REPEAT",
	"MAX_REPEAT",
	"AT",
	"ASSERT",
	"ASSERT_NOT",
	"SUBPATTERN",
	"_REPEATCODES",
	"ATOMIC_GROUP",
	"BRANCH",
	"GROUPREF_EXISTS",
	"_UNITCODES",
	"GROUPREF",
)
"""Names of the CPython-private ``re._parser`` module used by ``matchRegexSelection``."""
PRIVATE_COMPILER_NAMES = ("_simple", "compile")
"""Names of the CPython-private ``re._compiler`` module used by ``matchRegexSelection``."""


def _loadPrivateRegexModules() -> tuple[ModuleType, ModuleType]:
	"""Import ``re._parser`` and ``re._compiler`` and check every name the selection matcher uses.

	:raises ImportError: If a private module is missing.
	:raises AttributeError: If a private module lacks a used name.
	"""
	parser = import_module("re._parser")
	compiler = import_module("re._compiler")
	for module, names in ((parser, PRIVATE_PARSER_NAMES), (compiler, PRIVATE_COMPILER_NAMES)):
		for name in names:
			getattr(module, name)
	return parser, compiler


_privateRegexModules: tuple[ModuleType, ModuleType] | None
"""The private ``re`` modules, or ``None`` if they lack a name the selection matcher uses."""
_privateRegexError: Exception | None = None
"""Why the private ``re`` modules could not be loaded, if they could not."""
try:
	_privateRegexModules = _loadPrivateRegexModules()
except (ImportError, AttributeError) as error:
	_privateRegexModules = None
	_privateRegexError = error
_privateRegexFallbackLogged = False
"""Whether the fallback to context-free selection matching has been logged."""


def _fullMatchFallback(
	pattern: re.Pattern[str],
	text: str,
	start: int,
	end: int,
	error: Exception | None,
) -> re.Match[str] | None:
	"""Match the selection without surrounding context because the private ``re`` API is missing.

	The reason is logged only once.
	"""
	global _privateRegexFallbackLogged
	if not _privateRegexFallbackLogged:
		_privateRegexFallbackLogged = True
		log.debugWarning(
			"Private re parser API unavailable; regular-expression selections are matched without context.",
			exc_info=error,
		)
	return pattern.fullmatch(text, start, end)


def matchRegexSelection(pattern: re.Pattern[str], text: str, start: int, end: int) -> re.Match[str] | None:
	"""Return a full match of ``pattern`` over ``text[start:end]`` whose assertions see the whole text.

	Falls back to ``pattern.fullmatch(text, start, end)`` if CPython's private ``re``
	parser modules, or any name used from them, are missing.
	"""
	if end == len(text):
		return pattern.fullmatch(text, start)
	modules = _privateRegexModules
	if modules is None:
		return _fullMatchFallback(pattern, text, start, end, _privateRegexError)
	return _matchWithPrivateRegexApi(modules, pattern, text, start, end)


def _matchWithPrivateRegexApi(
	modules: tuple[ModuleType, ModuleType],
	pattern: re.Pattern[str],
	text: str,
	start: int,
	end: int,
) -> re.Match[str] | None:
	"""Match the selection by limiting consumption in its parse tree while assertions keep full context."""
	_parser, _compiler = modules
	# CPython 3.13 的 re 解析树：限制消耗文本的节点，保留断言可见的全文上下文。
	parsed = _parser.parse(pattern.pattern, pattern.flags)
	boundary = _parser.parse(rf"(?<!(?s:.{{{end + 1}}}))", 0)[0]

	def limitConsumption(subPattern: Any) -> bool:
		"""Cap consuming nodes below ``subPattern`` in place; return whether it contains assertions."""
		hasContext = False
		limited: list[tuple[Any, Any]] = []
		for operation, argument in subPattern:
			simpleRepeat = operation in (_parser.MIN_REPEAT, _parser.MAX_REPEAT) and _compiler._simple(
				argument[2],
			)
			if simpleRepeat:
				minimum, maximum, child = argument
				argument = (minimum, max(minimum, min(maximum, end - start)), child)
			elif operation in (_parser.AT, _parser.ASSERT, _parser.ASSERT_NOT):
				hasContext = True
			elif operation is _parser.SUBPATTERN or operation in _parser._REPEATCODES:
				hasContext |= limitConsumption(argument[-1])
			elif operation is _parser.ATOMIC_GROUP:
				hasContext |= limitConsumption(argument)
			elif operation is _parser.BRANCH:
				for branch in argument[1]:
					hasContext |= limitConsumption(branch)
			elif operation is _parser.GROUPREF_EXISTS:
				for branch in argument[1:]:
					if branch is not None:
						hasContext |= limitConsumption(branch)
			limited.append((operation, argument))
			if simpleRepeat or operation in _parser._UNITCODES or operation is _parser.GROUPREF:
				limited.append(boundary)
		subPattern.data = limited
		return hasContext

	if not limitConsumption(parsed):
		return pattern.fullmatch(text, start, end)
	parsed.append(_parser.parse(rf"(?<=\A(?s:.{{{end}}}))", 0)[0])
	return _compiler.compile(parsed, pattern.flags).match(text, start)


def findPreviousLiteralMatch(
	text: str,
	query: str,
	start: int,
	*,
	matchCase: bool,
	currentMatch: bool = False,
) -> tuple[int, int] | None:
	"""Return the code point span of the previous literal match, wrapping once."""
	if not query:
		return None
	searchRanges = (
		((0, min(len(text), start + len(query) - 1)), (start + 1, len(text)))
		if currentMatch
		else ((0, start), (0, len(text)))
	)
	if matchCase:
		for searchStart, searchEnd in searchRanges:
			matchStart = text.rfind(query, searchStart, searchEnd)
			if matchStart >= 0:
				return (matchStart, matchStart + len(query))
		return (start, start + len(query)) if currentMatch else None
	escapedQuery = re.escape(query)
	pattern = re.compile(escapedQuery, re.IGNORECASE)
	lastPattern = re.compile(rf".*({escapedQuery})", re.IGNORECASE | re.DOTALL)
	windowSize = max(_BACKWARD_SEARCH_CHUNK_SIZE, len(query))
	for searchStart, searchEnd in searchRanges:
		windowEnd = searchEnd
		while windowEnd - searchStart >= len(query):
			windowStart = max(searchStart, windowEnd - windowSize)
			scanStart = max(searchStart, windowStart - len(query) + 1)
			# Reject empty windows with the optimized literal search before using a greedy match.
			if pattern.search(text, scanStart, windowEnd) is not None:
				match = lastPattern.match(text, scanStart, windowEnd)
				assert match is not None
				return match.span(1)
			windowEnd = windowStart
	return (start, start + len(query)) if currentMatch else None


def normalizeSearchText(text: str) -> str:
	"""Return compatibility-normalized, caseless search text."""
	return unicodedata.normalize("NFKC", text).casefold()


def splitSearchKeywords(query: str) -> tuple[str, ...]:
	"""Split normalized query text on whitespace and remove repeated keywords."""
	return tuple(dict.fromkeys(query.split()))


def matchesSearchKeywords(normalizedTexts: Iterable[str], keywords: tuple[str, ...]) -> bool:
	"""Return whether every keyword occurs literally in at least one normalized field."""
	remainingKeywords = set(keywords)
	for text in normalizedTexts:
		matchedKeywords = {keyword for keyword in remainingKeywords if keyword in text}
		remainingKeywords.difference_update(matchedKeywords)
		if not remainingKeywords:
			return True
	return not remainingKeywords
