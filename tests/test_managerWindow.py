# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for manager window placement without loading NVDA or wx."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from _manager_method_loader import loadManagerClassMethods, loadManagerTopLevelFunctions


_clampWindowRect = loadManagerTopLevelFunctions({"_clampWindowRect"})["_clampWindowRect"]


class ManagerWindowTests(unittest.TestCase):
	def testWindowStaysInsideWorkArea(self) -> None:
		for rect, workArea, expected in (
			((100, 80, 900, 600), (0, 0, 1920, 1040), (100, 80, 900, 600)),
			((900, 500, 900, 600), (0, 0, 1280, 720), (380, 120, 900, 600)),
			((3000, 0, 1800, 1200), (0, 40, 1280, 680), (0, 40, 1280, 680)),
			((-2200, -200, 900, 600), (-1920, 0, 1920, 1040), (-1920, 0, 900, 600)),
			((-200, 800, 900, 600), (-1920, 0, 1920, 1040), (-900, 440, 900, 600)),
			((0, 0, 900, 600), (1920, -1080, 1920, 1040), (1920, -640, 900, 600)),
		):
			with self.subTest(rect=rect, workArea=workArea):
				self.assertEqual(expected, _clampWindowRect(rect, workArea))

	def testMaximizingAndMinimizingPreserveNormalBounds(self) -> None:
		state = {}
		saveWindowState = loadManagerClassMethods(
			{"_saveWindowState"},
			{"getManagerWindowState": lambda: state},
		)["_saveWindowState"]
		manager = SimpleNamespace(
			_isBeingDestroyed=False,
			IsIconized=Mock(return_value=False),
			IsMaximized=Mock(return_value=False),
			GetPosition=Mock(return_value=(200, 100)),
			GetSize=Mock(return_value=(1800, 1200)),
			ToDIP=lambda value: value // 2 if isinstance(value, int) else tuple(n // 2 for n in value),
			_splitter=SimpleNamespace(GetSashPosition=Mock(return_value=560)),
		)
		expected = {
			"x": 200,
			"y": 100,
			"width": 900,
			"height": 600,
			"maximized": False,
			"navigationWidth": 280,
		}
		saveWindowState(manager)
		self.assertEqual(expected, state)

		manager.IsMaximized.return_value = True
		manager.GetPosition.return_value = (-8, -8)
		manager.GetSize.return_value = (2560, 1440)
		expected["maximized"] = True
		saveWindowState(manager)
		self.assertEqual(expected, state)

		manager.IsIconized.return_value = True
		manager.IsMaximized.return_value = False
		manager.GetPosition.return_value = (-32000, -32000)
		saveWindowState(manager)
		self.assertEqual(expected, state)

	def testQueuedSaveAfterDestructionDoesNotAccessWindow(self) -> None:
		saveWindowState = loadManagerClassMethods({"_saveWindowState"})["_saveWindowState"]
		saveWindowState(SimpleNamespace(_isBeingDestroyed=True))


if __name__ == "__main__":
	unittest.main()
