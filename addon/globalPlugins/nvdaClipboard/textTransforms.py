# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Provide common text transforms for the clipboard manager editor."""

from __future__ import annotations

from collections.abc import Callable

import textUtils


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
	if hasSelection:
		offsetConverter = textUtils.WideStringOffsetConverter(text)
		textStart, textEnd = offsetConverter.encodedToStrOffsets(selectionStart, selectionEnd)
		if lineWise:
			textStart, textEnd = _expandToWholeLines(text, textStart, textEnd)
	else:
		textStart = 0
		textEnd = len(text)
	originalText = text[textStart:textEnd]
	replacementText = transform(originalText)
	if replacementText == originalText:
		return False
	updatedText = text[:textStart] + replacementText + text[textEnd:]
	editor.SetValue(updatedText)
	offsetConverter = textUtils.WideStringOffsetConverter(updatedText)
	replacementStart, replacementEnd = offsetConverter.strToEncodedOffsets(
		textStart,
		textStart + len(replacementText),
	)
	if hasSelection:
		editor.SetSelection(replacementStart, replacementEnd)
	else:
		editor.SetInsertionPoint(replacementStart)
	editor.ShowPosition(replacementStart)
	editor.SetFocus()
	return True


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


def _removeDuplicateLines(lines: list[str]) -> list[str]:
	"""Return each line once while preserving the original order."""
	return list(dict.fromkeys(lines))


def _sortLinesByLengthAscending(lines: list[str]) -> list[str]:
	"""Return the lines sorted from shortest to longest."""
	return sorted(lines, key=len)


def _sortLinesByLengthDescending(lines: list[str]) -> list[str]:
	"""Return the lines sorted from longest to shortest."""
	return sorted(lines, key=len, reverse=True)
