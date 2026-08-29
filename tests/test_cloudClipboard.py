"""Tests for ClipDataCloud SDK selection and loading."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
import sys
import unittest
from unittest.mock import patch

from tests._module_loader import loadAddonModule


_MODULE_DIRECTORY = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard"
_PACKAGE_NAME = "nvdaClipboardCloudClipboardTests"
_PACKAGE = ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_MODULE_DIRECTORY)]
sys.modules[_PACKAGE_NAME] = _PACKAGE


cloudClipboard = loadAddonModule(
	f"{_PACKAGE_NAME}.cloudClipboard",
	_MODULE_DIRECTORY / "cloudClipboard.py",
)


class CloudClipboardTests(unittest.TestCase):
	"""Verify the SDK loader picks the expected architecture-specific DLL."""

	def testArm64SelectsArmSdkDll(self) -> None:
		"""Use the ARM64 SDK DLL when the current machine reports ARM64."""
		with patch.object(cloudClipboard.platform, "machine", return_value="ARM64"):
			self.assertEqual(cloudClipboard._getSdkDllName(), "ClipDataCloud.SDK.ARM.dll")

	def testAmd64KeepsDefaultSdkDll(self) -> None:
		"""Keep the original SDK DLL name on AMD64."""
		with patch.object(cloudClipboard.platform, "machine", return_value="AMD64"):
			self.assertEqual(cloudClipboard._getSdkDllName(), "ClipDataCloud.SDK.dll")


if __name__ == "__main__":
	unittest.main()
