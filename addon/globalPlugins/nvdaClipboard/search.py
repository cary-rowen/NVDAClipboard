# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Provide Unicode-aware literal matching for clipboard manager search."""

from __future__ import annotations

from collections.abc import Iterable
import re
import unicodedata


_BACKWARD_SEARCH_CHUNK_SIZE = 64 * 1024


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
		else ((0, start), (start, len(text)))
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
