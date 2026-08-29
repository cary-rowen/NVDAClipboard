"""Tests for Tiantan Cloud Clipboard support probing."""

from __future__ import annotations

from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from tests._module_loader import loadAddonModule


_MODULE_DIRECTORY = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard"
_PACKAGE_NAME = "nvdaClipboardTiantanSupportTests"
_PACKAGE = ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_MODULE_DIRECTORY)]
sys.modules[_PACKAGE_NAME] = _PACKAGE


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
	_MODULE_DIRECTORY / "tiantanSupport.py",
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

	def testUnsupportedProcessBitnessDoesNotLoadSdk(self) -> None:
		"""Reject 32-bit NVDA before trying to load the SDK."""
		with (
			patch.object(tiantanSupport.sys, "maxsize", 2**32),
			patch.object(tiantanSupport.cloudClipboard, "CloudClipboardSdk") as sdkClass,
		):
			state = tiantanSupport.getTiantanSupportState()

		self.assertFalse(state.isAvailable)
		self.assertEqual(state.statusMessage, "Tiantan Cloud Clipboard requires 64-bit NVDA.")
		sdkClass.assert_not_called()
		tiantanSupport.log.debugWarning.assert_called_once()

	def testSdkLoadFailureReportsUnavailable(self) -> None:
		"""Return an unavailable state when the native SDK cannot be loaded."""
		error = tiantanSupport.cloudClipboard.CloudClipboardError(None, "load failed")
		with (
			patch.object(tiantanSupport.sys, "maxsize", 2**63 - 1),
			patch.object(tiantanSupport.cloudClipboard, "CloudClipboardSdk", side_effect=error),
		):
			state = tiantanSupport.getTiantanSupportState()

		self.assertFalse(state.isAvailable)
		self.assertEqual(
			state.statusMessage,
			"Tiantan Cloud Clipboard is unavailable. Check the ClipDataCloud SDK.",
		)
		tiantanSupport.log.debugWarning.assert_called_once_with(
			"ClipDataCloud SDK is unavailable.",
			exc_info=error,
		)


if __name__ == "__main__":
	unittest.main()
