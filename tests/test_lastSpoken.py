# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for reading NVDA's last speech record."""

import ast
from collections.abc import Callable
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tests._module_loader import addonSourcePath


_CONTROLLER_PATH = addonSourcePath("controller")


def _loadGetLastSpokenText(speech: SimpleNamespace, log: Mock) -> Callable[[], str | None]:
	"""Load the controller's last-spoken helper without importing controller dependencies."""
	tree = ast.parse(_CONTROLLER_PATH.read_text(encoding="utf-8"))
	nodes = [
		node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_getLastSpokenText"
	]
	namespace = {
		"speech": SimpleNamespace(speech=speech),
		"CHUNK_SEPARATOR": "  ",
		"log": log,
	}
	exec(compile(ast.Module(body=nodes, type_ignores=[]), _CONTROLLER_PATH, "exec"), namespace)
	return namespace["_getLastSpokenText"]


class LastSpokenTests(unittest.TestCase):
	def testReadsCurrentNativeRecordAndHandlesMissingRecord(self) -> None:
		speech = SimpleNamespace(_lastSpeech=None)
		log = Mock()
		getLastSpokenText = _loadGetLastSpokenText(speech, log)
		self.assertIsNone(getLastSpokenText())
		speech._lastSpeech = (["first", object(), "second"], None)
		self.assertEqual("first  second", getLastSpokenText())
		speech._lastSpeech = (["new speech"], None)
		self.assertEqual("new speech", getLastSpokenText())
		log.debugWarning.assert_not_called()
		del speech._lastSpeech
		self.assertIsNone(getLastSpokenText())
		log.debugWarning.assert_called_once()


if __name__ == "__main__":
	unittest.main()
