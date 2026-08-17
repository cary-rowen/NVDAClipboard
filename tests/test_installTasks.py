"""Tests for the NVDA Clipboard installation tasks."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


_MODULE_PATH = Path(__file__).parents[1] / "addon" / "installTasks.py"
_SPEC = importlib.util.spec_from_file_location("nvdaClipboardInstallTasksTests", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
installTasks = importlib.util.module_from_spec(_SPEC)
_ADDON_HANDLER = ModuleType("addonHandler")
_LOG_HANDLER = ModuleType("logHandler")
_LOG_HANDLER.log = Mock()
with patch.dict(sys.modules, {"addonHandler": _ADDON_HANDLER, "logHandler": _LOG_HANDLER}):
	_SPEC.loader.exec_module(installTasks)


class InstallTasksTests(unittest.TestCase):
	"""Verify silent cleanup of the previous add-on identity."""

	def testOnInstallRequestsRemovalOnlyForLegacyAddon(self) -> None:
		"""Request removal for clipboardEnhancement without touching other add-ons."""
		legacyAddon = SimpleNamespace(name="clipboardEnhancement", requestRemove=Mock())
		otherAddon = SimpleNamespace(name="anotherAddon", requestRemove=Mock())
		installTasks.addonHandler.getAvailableAddons = Mock(return_value=(legacyAddon, otherAddon))

		installTasks.onInstall()

		legacyAddon.requestRemove.assert_called_once_with()
		otherAddon.requestRemove.assert_not_called()

	def testCleanupFailureDoesNotAbortInstallation(self) -> None:
		"""Keep installation successful when NVDA rejects a removal request."""
		legacyAddon = SimpleNamespace(
			name="clipboardEnhancement",
			requestRemove=Mock(side_effect=RuntimeError("state unavailable")),
		)
		installTasks.addonHandler.getAvailableAddons = Mock(return_value=(legacyAddon,))

		installTasks.onInstall()

		legacyAddon.requestRemove.assert_called_once_with()
		installTasks.log.exception.assert_called_once()
