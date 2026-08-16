# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Calculate configurable clipboard navigation unit boundaries."""

import unicodedata


def _isPunctuation(character: str) -> bool:
	"""Return whether a character has a Unicode punctuation category."""
	return unicodedata.category(character)[0] == "P"


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
