# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for Tiantan Cloud Clipboard support probing."""

from __future__ import annotations

import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from tests._module_loader import createAddonPackage, loadAddonModule


_PACKAGE_NAME = "nvdaClipboardTiantanSupportTests"
createAddonPackage(_PACKAGE_NAME)


def _translate(message: str) -> str:
	"""Return an untranslated message for standalone validation."""
	return message


def _initTranslation() -> None:
	"""Install the standalone translator in the importing module globals."""
	sys._getframe(1).f_globals["_"] = _translate


_ADDON_HANDLER = ModuleType("addonHandler")
_ADDON_HANDLER.initTranslation = _initTranslation
_LOG_HANDLER = ModuleType("logHandler")
_LOG_HANDLER.log = Mock()
tiantanSupport = loadAddonModule(
	f"{_PACKAGE_NAME}.tiantanSupport",
	injectedModules={
		"addonHandler": _ADDON_HANDLER,
		"logHandler": _LOG_HANDLER,
	},
)


class TiantanSupportTests(unittest.TestCase):
	"""Verify Tiantan support probing without loading the native SDK unexpectedly."""

	def setUp(self) -> None:
		"""Clear the cached probe state before each test."""
		tiantanSupport.getTiantanSupportState.cache_clear()
		tiantanSupport.log.reset_mock()

	def testSdkLoadFailureReportsUnavailable(self) -> None:
		"""Return an unavailable state when the native SDK cannot be loaded."""
		error = tiantanSupport.cloudClipboard.CloudClipboardError(None, "load failed")
		with patch.object(tiantanSupport.cloudClipboard, "CloudClipboardSdk", side_effect=error):
			state = tiantanSupport.getTiantanSupportState()

		self.assertFalse(state.isAvailable)
		self.assertIsNone(state.sdk)
		self.assertEqual(
			state.statusMessage,
			"Tiantan Cloud Clipboard is unavailable. Check the ClipDataCloud SDK.",
		)
		tiantanSupport.log.debugWarning.assert_called_once_with(
			"ClipDataCloud SDK is unavailable.",
			exc_info=error,
		)

	def testSdkIsLoadedOnceAndReturned(self) -> None:
		"""Load the native SDK only once and expose the loaded instance."""
		with patch.object(tiantanSupport.cloudClipboard, "CloudClipboardSdk") as sdkClass:
			state = tiantanSupport.getTiantanSupportState()
			self.assertIs(tiantanSupport.getTiantanSupportState(), state)

		self.assertTrue(state.isAvailable)
		self.assertIs(state.sdk, sdkClass.return_value)
		self.assertEqual(state.statusMessage, "")
		sdkClass.assert_called_once_with()


if __name__ == "__main__":
	unittest.main()
