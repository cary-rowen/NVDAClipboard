# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Provide read-only navigation over clipboard text."""

from dataclasses import dataclass
from typing import override
import unicodedata

import textInfos
from textInfos.offsets import Offsets, OffsetsTextInfo

from .configuration import NavigationSplitMode, getNavigationSplitSettings


_SUPPORTED_UNITS = frozenset(
	(
		textInfos.UNIT_LINE,
		textInfos.UNIT_WORD,
		textInfos.UNIT_CHARACTER,
	),
)


@dataclass(frozen=True, slots=True)
class NavigationResult:
	"""Describe the position and boundaries after a navigation attempt."""

	textInfo: textInfos.TextInfo
	isAtBoundary: bool
	hasCrossedLine: bool
	isAtStoryBoundary: bool


class _ClipboardTextOwner:
	def __init__(self, text: str) -> None:
		self.text = text


class _ClipboardTextInfo(OffsetsTextInfo):
	"""Expose clipboard text through NVDA's offset-based TextInfo implementation."""

	encoding = None

	def __init__(self, owner: _ClipboardTextOwner, position: object) -> None:
		self._owner = owner
		super().__init__(owner, position)

	@override
	def _getStoryText(self) -> str:
		return self._owner.text

	@override
	def _getStoryLength(self) -> int:
		return len(self._owner.text)

	@override
	def _getWordOffsets(self, offset: int) -> tuple[int, int]:
		"""Return offsets for the configured navigation unit."""
		splitMode, splitCamelCase, separatePunctuation = getNavigationSplitSettings()
		if splitMode is NavigationSplitMode.WINDOWS_WORD:
			start, end = super()._getWordOffsets(offset)
		else:
			lineStart, lineEnd = self._getLineOffsets(offset)
			lineText = self._getTextRange(lineStart, lineEnd)
			start, end = getPunctuationUnitOffsets(
				lineText,
				offset - lineStart,
				separatePunctuation=separatePunctuation,
			)
			start += lineStart
			end += lineStart
		if not splitCamelCase:
			return start, end
		camelStart, camelEnd = getCamelCaseUnitOffsets(
			self._getTextRange(start, end),
			offset - start,
		)
		return camelStart + start, camelEnd + start


class ClipboardNavigator:
	"""Maintain an independent read-only position and marked range in clipboard text."""

	def __init__(self, text: str = "") -> None:
		self._owner = _ClipboardTextOwner(text)
		self._position: _ClipboardTextInfo
		self._selectionStartOffset: int | None = None
		self._selectionEndOffset: int | None = None
		self.reset()

	def setText(self, text: str) -> None:
		"""Replace the clipboard snapshot and reset its navigation and marked range."""
		self._owner = _ClipboardTextOwner(text)
		self._selectionStartOffset = None
		self._selectionEndOffset = None
		self.reset()

	def reset(self) -> None:
		"""Reset navigation to the first position of the current clipboard snapshot."""
		self._position = _ClipboardTextInfo(self._owner, textInfos.POSITION_FIRST)

	def getPosition(self) -> int:
		"""Return the current Python string offset."""
		return self._position.bookmark.startOffset

	def getLineAndColumn(self) -> tuple[int, int]:
		"""Return the one-based line and character column at the current position."""
		text = self._owner.text
		if not text:
			return 1, 1
		offset = min(self.getPosition(), len(text) - 1)
		lineStart = self._getExpanded(self._position, textInfos.UNIT_LINE).bookmark.startOffset
		lineNumber = (
			text.count("\n", 0, lineStart)
			+ text.count("\r", 0, lineStart)
			- text.count("\r\n", 0, lineStart)
			+ 1
		)
		return lineNumber, offset - lineStart + 1

	def setPosition(self, offset: int) -> None:
		"""Move to a bounded Python string offset."""
		maxOffset = max(len(self._owner.text) - 1, 0)
		offset = min(max(offset, 0), maxOffset)
		self._position = _ClipboardTextInfo(self._owner, Offsets(offset, offset))

	def markSelectionStart(self) -> None:
		"""Mark the current position as the start and discard any completed range."""
		self._selectionStartOffset = self.getPosition()
		self._selectionEndOffset = None

	def markSelectionEnd(self) -> str | None:
		"""Mark the current position as the end and return the inclusive selected text."""
		if self._selectionStartOffset is None:
			return None
		self._selectionEndOffset = self.getPosition()
		return self.getSelectedText()

	def getSelectionOffsets(self) -> tuple[int, int] | None:
		"""Return both marked offsets, or ``None`` until the range is complete."""
		if self._selectionStartOffset is None or self._selectionEndOffset is None:
			return None
		return self._selectionStartOffset, self._selectionEndOffset

	def setSelectionOffsets(self, offsets: tuple[int, int] | None) -> None:
		"""Restore a completed marked range, or clear all selection markers."""
		if offsets is None:
			self._selectionStartOffset = None
			self._selectionEndOffset = None
			return
		startOffset, endOffset = offsets
		maxOffset = len(self._owner.text) - 1
		if maxOffset < 0 or not 0 <= startOffset <= maxOffset or not 0 <= endOffset <= maxOffset:
			raise ValueError(offsets)
		self._selectionStartOffset = startOffset
		self._selectionEndOffset = endOffset

	def getSelectedText(self) -> str | None:
		"""Return text between both inclusive markers, or ``None`` until both are set."""
		if self._selectionStartOffset is None or self._selectionEndOffset is None:
			return None
		startInfo = self._getExpanded(
			_ClipboardTextInfo(
				self._owner,
				Offsets(self._selectionStartOffset, self._selectionStartOffset),
			),
			textInfos.UNIT_CHARACTER,
		)
		endInfo = self._getExpanded(
			_ClipboardTextInfo(
				self._owner,
				Offsets(self._selectionEndOffset, self._selectionEndOffset),
			),
			textInfos.UNIT_CHARACTER,
		)
		startOffset = min(startInfo.bookmark.startOffset, endInfo.bookmark.startOffset)
		endOffset = max(startInfo.bookmark.endOffset, endInfo.bookmark.endOffset)
		return self._owner.text[startOffset:endOffset]

	def moveToFirstLine(self) -> textInfos.TextInfo:
		"""Move to and return the first line in the clipboard snapshot."""
		self.reset()
		return self.getCurrentLine()

	def moveToLastLine(self) -> textInfos.TextInfo:
		"""Move to and return the last line in the clipboard snapshot."""
		self._position = _ClipboardTextInfo(self._owner, textInfos.POSITION_LAST)
		return self.getCurrentLine()

	def getCurrentLine(self) -> textInfos.TextInfo:
		"""Return a TextInfo expanded to the current line."""
		return self._getCurrent(textInfos.UNIT_LINE)

	def getCurrentNavigationUnit(self) -> textInfos.TextInfo:
		"""Return a TextInfo expanded to the configured navigation unit."""
		return self._getCurrent(textInfos.UNIT_WORD)

	def getCurrentCharacter(self) -> textInfos.TextInfo:
		"""Return a TextInfo expanded to the current character."""
		return self._getCurrent(textInfos.UNIT_CHARACTER)

	def move(self, unit: str, direction: int) -> NavigationResult:
		"""Move by one supported text unit and report boundary transitions.

		:param unit: One of ``UNIT_LINE``, ``UNIT_WORD`` or ``UNIT_CHARACTER``.
		:param direction: ``-1`` to move backward or ``1`` to move forward.
		"""
		if unit not in _SUPPORTED_UNITS:
			raise ValueError(f"Unsupported navigation unit: {unit!r}")
		if direction not in (-1, 1):
			raise ValueError(f"Direction must be -1 or 1, got {direction!r}")

		originalPosition = self._position.copy()
		originalUnit = self._getExpanded(originalPosition, unit)
		originalLine = self._getExpanded(originalPosition, textInfos.UNIT_LINE)
		newPosition = originalPosition.copy()
		if direction < 0:
			newPosition.expand(unit)
			newPosition.collapse()

		moveCount = newPosition.move(unit, direction)
		newUnit = self._getExpanded(newPosition, unit)
		if (
			moveCount == 0
			or not newUnit.text
			or (direction > 0 and newUnit.compareEndPoints(originalUnit, "startToStart") <= 0)
		):
			return NavigationResult(
				originalUnit,
				isAtBoundary=True,
				hasCrossedLine=False,
				isAtStoryBoundary=True,
			)

		self._position = newPosition
		newLine = self._getExpanded(newPosition, textInfos.UNIT_LINE)
		story = _ClipboardTextInfo(self._owner, textInfos.POSITION_ALL)
		boundaryComparison = "startToStart" if direction < 0 else "endToEnd"
		return NavigationResult(
			newUnit,
			isAtBoundary=False,
			hasCrossedLine=newLine.compareEndPoints(originalLine, "startToStart") != 0,
			isAtStoryBoundary=newUnit.compareEndPoints(story, boundaryComparison) == 0,
		)

	def _getCurrent(self, unit: str) -> textInfos.TextInfo:
		return self._getExpanded(self._position, unit)

	@staticmethod
	def _getExpanded(position: _ClipboardTextInfo, unit: str) -> _ClipboardTextInfo:
		info = position.copy()
		info.expand(unit)
		return info


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
