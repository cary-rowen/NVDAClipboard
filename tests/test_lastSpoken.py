# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for reading NVDA's last speech record."""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tests._module_loader import loadAddonModule


class LastSpokenTests(unittest.TestCase):
	def testReadsCurrentNativeRecordAndHandlesMissingRecord(self) -> None:
		speech = SimpleNamespace(_lastSpeech=None)
		log = Mock()
		lastSpoken = loadAddonModule(
			"nvdaClipboardLastSpoken",
			Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "lastSpoken.py",
			injectedModules={
				"speech": SimpleNamespace(CHUNK_SEPARATOR="  ", speech=speech),
				"logHandler": SimpleNamespace(log=log),
			},
		)
		self.assertIsNone(lastSpoken.getText())
		speech._lastSpeech = (["first", object(), "second"], None)
		self.assertEqual("first  second", lastSpoken.getText())
		speech._lastSpeech = (["new speech"], None)
		self.assertEqual("new speech", lastSpoken.getText())
		log.debugWarning.assert_not_called()
		del speech._lastSpeech
		self.assertIsNone(lastSpoken.getText())
		log.debugWarning.assert_called_once()


if __name__ == "__main__":
	unittest.main()
