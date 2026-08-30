"""Tests for clipboard manager source-to-editor offset conversion."""

from __future__ import annotations

from collections.abc import Callable
from itertools import product
from typing import cast
import unittest

from _manager_method_loader import loadManagerTopLevelFunctions


def _loadOffsetFunction() -> Callable[[str, int], int]:
	"""Load the pure helper without importing manager GUI dependencies."""
	namespace = loadManagerTopLevelFunctions({"_sourceToEditorOffset"})
	return cast(Callable[[str, int], int], namespace["_sourceToEditorOffset"])


_sourceToEditorOffset = _loadOffsetFunction()


class ManagerOffsetTests(unittest.TestCase):
	"""Verify source offsets map to wx's normalized editor offsets."""

	def testSourceOffsetsMatchDirectNormalization(self) -> None:
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


if __name__ == "__main__":
	unittest.main()
