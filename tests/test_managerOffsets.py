"""Tests for clipboard manager newline offset conversion without loading NVDA."""

from __future__ import annotations

from collections.abc import Callable
from itertools import product
from typing import cast
import unittest

from _manager_method_loader import loadManagerTopLevelFunctions


def _loadOffsetFunctions() -> tuple[Callable[[str, int], int], Callable[[str, int], int]]:
	"""Load the two pure helpers without importing manager GUI dependencies."""
	namespace = loadManagerTopLevelFunctions({"_sourceToEditorOffset", "_editorToSourceOffset"})
	return (
		cast(Callable[[str, int], int], namespace["_sourceToEditorOffset"]),
		cast(Callable[[str, int], int], namespace["_editorToSourceOffset"]),
	)


_sourceToEditorOffset, _editorToSourceOffset = _loadOffsetFunctions()


class ManagerOffsetTests(unittest.TestCase):
	"""Verify source and normalized editor offsets at every newline boundary."""

	def testOffsetsMatchDirectNormalization(self) -> None:
		"""Preserve clamping and CRLF boundary behavior for mixed line endings."""
		for length in range(7):
			for characters in product("a\r\n", repeat=length):
				text = "".join(characters)
				for offset in range(-2, length * 2 + 3):
					sourceOffset = min(max(offset, 0), len(text))
					if (
						0 < sourceOffset < len(text)
						and text[sourceOffset] == "\n"
						and text[sourceOffset - 1] == "\r"
					):
						sourceOffset -= 1
					expectedEditorOffset = len(
						text[:sourceOffset].replace("\r\n", "\n").replace("\r", "\n"),
					)
					self.assertEqual(expectedEditorOffset, _sourceToEditorOffset(text, offset))

					remaining = max(offset, 0)
					expectedSourceOffset = 0
					while expectedSourceOffset < len(text) and remaining:
						expectedSourceOffset += 2 if text.startswith("\r\n", expectedSourceOffset) else 1
						remaining -= 1
					self.assertEqual(expectedSourceOffset, _editorToSourceOffset(text, offset))
