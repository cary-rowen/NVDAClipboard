# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Provide common text transforms for the clipboard manager editor."""

from __future__ import annotations

from collections.abc import Callable
import ctypes

import textUtils

_EM_SETSEL = 0x00B1
_EM_REPLACESEL = 0x00C2


def _applyLineTransform(text: str, transform: Callable[[list[str]], list[str]]) -> str:
	"""Apply one line-based transform while preserving a trailing newline."""
	if not text:
		return text
	lines = text.splitlines()
	if not lines:
		return text
	result = "\n".join(transform(lines))
	if text.endswith(("\n", "\r")):
		result += "\n"
	return result


def trimTrailingSpaces(text: str) -> str:
	"""Remove trailing spaces and tabs from each line."""
	return _applyLineTransform(text, lambda lines: [line.rstrip(" \t") for line in lines])


def trimLeadingSpaces(text: str) -> str:
	"""Remove leading spaces and tabs from each line."""
	return _applyLineTransform(text, lambda lines: [line.lstrip(" \t") for line in lines])


def trimLeadingAndTrailingSpaces(text: str) -> str:
	"""Remove leading and trailing spaces and tabs from each line."""
	return _applyLineTransform(text, lambda lines: [line.strip(" \t") for line in lines])


def replaceLineBreaksWithSpaces(text: str) -> str:
	"""Replace all line breaks with single spaces."""
	return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def segmentChineseWords(text: str) -> str:
	"""Insert spaces at Chinese word boundaries using NVDA's word segmenter."""
	if not text:
		return text
	from textUtils._wordSeg import wordSegStrategy
	from textUtils._wordSeg.wordSegmenter import WordSegmenter
	from textUtils.segFlag import WordSegFlag

	wordSegStrategy.ChineseWordSegmentationStrategy._initCppJieba(forceInit=True)
	return WordSegmenter(text, wordSegFlag=WordSegFlag.CHINESE).segmentedText(sep=" ")


def removeBlankLines(text: str) -> str:
	"""Remove lines containing only spaces or tabs."""
	if not text:
		return text
	lines = [line for line in text.splitlines() if line.strip(" \t")]
	if not lines:
		return ""
	result = "\n".join(lines)
	if text.endswith(("\n", "\r")):
		result += "\n"
	return result


def removeConsecutiveBlankLines(text: str) -> str:
	"""Collapse consecutive blank lines to a single blank line."""
	return _applyLineTransform(text, _removeConsecutiveBlankLines)


def removeConsecutiveDuplicateLines(text: str) -> str:
	"""Remove immediately repeated lines while keeping the first occurrence."""
	return _applyLineTransform(text, _removeConsecutiveDuplicateLines)


def removeDuplicateLines(text: str) -> str:
	"""Remove repeated lines while keeping the first occurrence."""
	return _applyLineTransform(text, _removeDuplicateLines)


def sortLinesByLengthAscending(text: str) -> str:
	"""Sort lines by length from shortest to longest."""
	return _applyLineTransform(text, _sortLinesByLengthAscending)


def sortLinesByLengthDescending(text: str) -> str:
	"""Sort lines by length from longest to shortest."""
	return _applyLineTransform(text, _sortLinesByLengthDescending)


def applyEditorTextTransform(
	editor: object,
	transform: Callable[[str], str],
	*,
	lineWise: bool = False,
) -> bool:
	"""Apply one text transform to the editor selection or full content."""
	text = editor.GetValue()
	selectionStart, selectionEnd = editor.GetSelection()
	hasSelection = selectionStart != selectionEnd
	offsetConverter = textUtils.WideStringOffsetConverter(text)
	if hasSelection:
		textStart, textEnd = offsetConverter.encodedToStrOffsets(selectionStart, selectionEnd)
		if lineWise:
			textStart, textEnd = _expandToWholeLines(text, textStart, textEnd)
		encodedStart, encodedEnd = offsetConverter.strToEncodedOffsets(textStart, textEnd)
	else:
		textStart = 0
		textEnd = len(text)
		encodedStart = 0
		encodedEnd = editor.GetLastPosition()
	originalText = text[textStart:textEnd]
	replacementText = transform(originalText)
	if replacementText == originalText:
		return False
	_replaceEditorRange(editor, encodedStart, encodedEnd, replacementText)
	if hasSelection:
		editor.SetSelection(encodedStart, editor.GetInsertionPoint())
	else:
		editor.SetInsertionPoint(encodedStart)
	editor.ShowPosition(encodedStart)
	editor.SetFocus()
	return True


def _replaceEditorRange(editor: object, start: int, end: int, replacementText: str) -> None:
	"""Replace editor text as a single native undoable action when possible."""
	if _replaceEditorRangeWithWin32(editor, start, end, replacementText):
		return
	editor.Replace(start, end, replacementText)


def _replaceEditorRangeWithWin32(editor: object, start: int, end: int, replacementText: str) -> bool:
	"""Replace text via the Windows edit control API when a handle is available."""
	getHandle = getattr(editor, "GetHandle", None)
	if getHandle is None:
		return False
	try:
		handle = int(getHandle())
	except (TypeError, ValueError):
		return False
	if not handle or not hasattr(ctypes, "windll"):
		return False
	user32 = ctypes.windll.user32
	if not user32.IsWindow(handle):
		return False
	user32.SendMessageW(handle, _EM_SETSEL, start, end)
	user32.SendMessageW(
		handle,
		_EM_REPLACESEL,
		True,
		ctypes.c_wchar_p(_normalizeNewlinesForWin32(replacementText)),
	)
	return True


def _normalizeNewlinesForWin32(text: str) -> str:
	"""Convert normalized Python newlines to Win32 edit control line breaks."""
	return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def _expandToWholeLines(text: str, start: int, end: int) -> tuple[int, int]:
	"""Expand a selected span to the lines it touches."""
	lineStart = text.rfind("\n", 0, start) + 1
	lastSelected = max(start, end - 1)
	lineEnd = text.find("\n", lastSelected)
	if lineEnd < 0:
		lineEnd = len(text)
	else:
		lineEnd += 1
	return lineStart, lineEnd


def _removeConsecutiveDuplicateLines(lines: list[str]) -> list[str]:
	"""Return the first line from each run of consecutive duplicates."""
	result = [lines[0]]
	for line in lines[1:]:
		if line != result[-1]:
			result.append(line)
	return result


def _removeConsecutiveBlankLines(lines: list[str]) -> list[str]:
	"""Return lines with each run of blank lines collapsed to one line."""
	result: list[str] = []
	previousBlank = False
	for line in lines:
		isBlank = not line.strip(" \t")
		if isBlank and previousBlank:
			continue
		result.append("" if isBlank else line)
		previousBlank = isBlank
	return result


def _removeDuplicateLines(lines: list[str]) -> list[str]:
	"""Return each line once while preserving the original order."""
	return list(dict.fromkeys(lines))


def _sortLinesByLengthAscending(lines: list[str]) -> list[str]:
	"""Return the lines sorted from shortest to longest."""
	return sorted(lines, key=len)


def _sortLinesByLengthDescending(lines: list[str]) -> list[str]:
	"""Return the lines sorted from longest to shortest."""
	return sorted(lines, key=len, reverse=True)
