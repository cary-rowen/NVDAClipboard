"""Tests for last-spoken temporary paste helpers without loading NVDA."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import unittest

from tests.test_clipboardMonitor import clipboardMonitor


_CONTROLLER_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "controller.py"


def _loadLastSpokenPasteHelpers() -> tuple[type, object]:
	"""Load last-spoken paste helpers without importing controller dependencies."""
	tree = ast.parse(_CONTROLLER_PATH.read_text(encoding="utf-8"))
	nodes = [
		node
		for node in tree.body
		if (
			isinstance(node, ast.ClassDef)
			and node.name == "_LastSpokenPasteState"
			or isinstance(node, ast.FunctionDef)
			and node.name in {"_normalizeClipboardLineEndings", "_isLastSpokenTemporarySnapshot"}
		)
	]
	namespace = {
		"dataclass": dataclass,
		"ClipboardContentType": clipboardMonitor.ClipboardContentType,
		"ClipboardSnapshot": clipboardMonitor.ClipboardSnapshot,
	}
	exec(compile(ast.Module(body=nodes, type_ignores=[]), _CONTROLLER_PATH, "exec"), namespace)
	return namespace["_LastSpokenPasteState"], namespace["_isLastSpokenTemporarySnapshot"]


_LastSpokenPasteState, _isLastSpokenTemporarySnapshot = _loadLastSpokenPasteHelpers()


class LastSpokenPasteTests(unittest.TestCase):
	"""Verify temporary last-spoken paste matching."""

	def testTemporaryTextMatchesWin32ClipboardLineEndings(self) -> None:
		"""Accept text after CF_UNICODETEXT normalizes line endings to CRLF."""
		state = _LastSpokenPasteState(
			SimpleNamespace(),
			1,
			"first\nsecond",
		)
		snapshot = clipboardMonitor.ClipboardSnapshot(
			clipboardMonitor.ClipboardContentType.TEXT,
			sequenceNumber=1,
			text="first\r\nsecond",
			canIncludeInHistory=False,
			canUpload=False,
		)

		self.assertTrue(_isLastSpokenTemporarySnapshot(snapshot, state))


if __name__ == "__main__":
	unittest.main()
