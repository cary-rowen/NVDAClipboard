# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Calculate configurable clipboard navigation unit boundaries."""

import unicodedata


def _isPunctuation(character: str) -> bool:
	"""Return whether a character has a Unicode punctuation category."""
	return unicodedata.category(character)[0] == "P"


def _isMark(character: str) -> bool:
	"""Return whether a character has a Unicode mark category."""
	return unicodedata.category(character)[0] == "M"


def _isWordCharacter(character: str) -> bool:
	"""Return whether NVDA's fallback word boundaries include a character."""
	return unicodedata.category(character)[0] in "LMN"


def _isCamelCaseBoundary(text: str, index: int) -> bool:
	"""Return whether an index separates camel-case identifier components."""
	right = text[index]
	if not right.istitle():
		return False
	leftIndex = index - 1
	while leftIndex >= 0 and _isMark(text[leftIndex]):
		leftIndex -= 1
	if leftIndex < 0:
		return False
	left = text[leftIndex]
	nextIndex = index + 1
	while nextIndex < len(text) and _isMark(text[nextIndex]):
		nextIndex += 1
	return (
		left.islower()
		or left.isdigit()
		or (left.istitle() and nextIndex < len(text) and text[nextIndex].islower())
	)


def _wordContainsCamelCaseBoundary(text: str, start: int, end: int) -> bool:
	"""Return whether a word contains a camel-case boundary."""
	return any(_isCamelCaseBoundary(text, index) for index in range(start + 1, end))


def _isCamelCaseUnitBoundary(text: str, index: int) -> bool:
	"""Return whether an index separates capitalization-refined navigation units."""
	if _isCamelCaseBoundary(text, index):
		return True
	if not _isWordCharacter(text[index]) or _isWordCharacter(text[index - 1]):
		return False

	previousEnd = index
	while previousEnd > 0 and not _isWordCharacter(text[previousEnd - 1]):
		previousEnd -= 1
	if previousEnd == 0:
		return False

	currentEnd = index + 1
	while currentEnd < len(text) and _isWordCharacter(text[currentEnd]):
		currentEnd += 1
	if _wordContainsCamelCaseBoundary(text, index, currentEnd):
		return True

	previousStart = previousEnd - 1
	while previousStart > 0 and _isWordCharacter(text[previousStart - 1]):
		previousStart -= 1
	return _wordContainsCamelCaseBoundary(text, previousStart, previousEnd)


def getCamelCaseUnitOffsets(text: str, offset: int) -> tuple[int, int]:
	"""Return the capitalization-refined navigation unit containing an offset."""
	if offset < 0:
		raise ValueError(f"Offset must not be negative, got {offset}")
	if offset >= len(text):
		return offset, offset + 1

	start = offset
	while start > 0 and not _isCamelCaseUnitBoundary(text, start):
		start -= 1
	end = offset + 1
	while end < len(text) and not _isCamelCaseUnitBoundary(text, end):
		end += 1
	return start, end


def _getSeparatePunctuationUnitOffsets(text: str, offset: int) -> tuple[int, int]:
	"""Return a unit where each punctuation character is separate."""
	if _isPunctuation(text[offset]):
		start = offset
		end = offset + 1
		while end < len(text) and text[end].isspace():
			end += 1
		return start, end

	if text[offset].isspace():
		start = offset
		while start > 0 and text[start - 1].isspace():
			start -= 1
		if start > 0 and _isPunctuation(text[start - 1]):
			end = offset + 1
			while end < len(text) and text[end].isspace():
				end += 1
			return start - 1, end

	start = offset
	while start > 0 and not _isPunctuation(text[start - 1]):
		start -= 1
	if start > 0:
		while start < offset and text[start].isspace():
			start += 1
	end = offset + 1
	while end < len(text) and not _isPunctuation(text[end]):
		end += 1
	return start, end


def _getAttachedPunctuationUnitOffsets(text: str, offset: int) -> tuple[int, int]:
	"""Return a unit with delimiter clusters attached to preceding text."""
	unitStart = offset
	# Find the closest completed delimiter whose preceding chunk contains content.
	while unitStart > 0:
		punctuationEnd = unitStart
		while punctuationEnd > 0 and not _isPunctuation(text[punctuationEnd - 1]):
			punctuationEnd -= 1
		if punctuationEnd == 0:
			unitStart = 0
			break

		punctuationStart = punctuationEnd - 1
		while punctuationStart > 0 and _isPunctuation(text[punctuationStart - 1]):
			punctuationStart -= 1
		while punctuationEnd < len(text) and _isPunctuation(text[punctuationEnd]):
			punctuationEnd += 1
		boundary = punctuationEnd
		while boundary < len(text) and text[boundary].isspace():
			boundary += 1

		contentStart = punctuationStart
		hasContent = False
		while contentStart > 0 and not _isPunctuation(text[contentStart - 1]):
			contentStart -= 1
			hasContent = hasContent or not text[contentStart].isspace()
		if boundary <= offset and hasContent:
			unitStart = boundary
			break
		unitStart = contentStart

	hasContent = False
	index = unitStart
	while index < len(text):
		if not _isPunctuation(text[index]):
			hasContent = hasContent or not text[index].isspace()
			index += 1
			continue

		while index < len(text) and _isPunctuation(text[index]):
			index += 1
		while index < len(text) and text[index].isspace():
			index += 1
		if hasContent:
			return unitStart, index

	return unitStart, len(text)


def getPunctuationUnitOffsets(
	text: str,
	offset: int,
	*,
	separatePunctuation: bool,
) -> tuple[int, int]:
	"""Return the punctuation-delimited unit containing an offset.

	When punctuation is not separate, a consecutive punctuation run is attached
	to preceding text where possible. Otherwise, each punctuation character and
	any trailing whitespace form one unit, preventing whitespace-only navigation
	stops.
	"""
	if offset < 0:
		raise ValueError(f"Offset must not be negative, got {offset}")
	if offset >= len(text):
		return offset, offset + 1

	if separatePunctuation:
		return _getSeparatePunctuationUnitOffsets(text, offset)
	return _getAttachedPunctuationUnitOffsets(text, offset)
