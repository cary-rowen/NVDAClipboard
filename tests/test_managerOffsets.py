"""Tests for clipboard manager newline offset conversion without loading NVDA."""

from __future__ import annotations

import ast
from collections.abc import Callable
from itertools import product
from pathlib import Path
from typing import cast
import unittest


_MODULE_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "manager.py"


def _loadOffsetFunctions() -> tuple[Callable[[str, int], int], Callable[[str, int], int]]:
	"""Load the two pure helpers without importing manager GUI dependencies."""
	tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
	nodes = [
		node
		for node in tree.body
		if isinstance(node, ast.FunctionDef)
		and node.name in {"_sourceToEditorOffset", "_editorToSourceOffset"}
	]
	namespace: dict[str, object] = {}
	exec(compile(ast.Module(body=nodes, type_ignores=[]), str(_MODULE_PATH), "exec"), namespace)
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
