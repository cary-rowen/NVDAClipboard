# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for locating bundled native dependencies from the add-on package."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from tests._module_loader import ADDON_PACKAGE_DIRECTORY, createAddonPackage, loadAddonModule


_PACKAGE_NAME = "nvdaClipboardNativeTests"
createAddonPackage(_PACKAGE_NAME)
native = loadAddonModule(f"{_PACKAGE_NAME}._native")


class NativeFilePathTests(unittest.TestCase):
	"""Keep native file lookups anchored to the package's ``_vendor`` tree."""

	def testVendorRootIsBesidePackageModules(self) -> None:
		"""Resolve ``_vendor`` relative to the add-on package directory."""
		self.assertEqual(ADDON_PACKAGE_DIRECTORY.resolve() / "_vendor", native._DEPS_ROOT)

	def testNativeFilesResolveForEachArchitecture(self) -> None:
		"""Return existing bundled files for AMD64 and ARM64 machines."""
		for processPlatform, directory, fileName in (
			("win-amd64", "amd64", "_sqlite3.pyd"),
			("win-arm64", "arm64", "_sqlite3.pyd"),
			("win-amd64", "amd64", "ClipDataCloud.SDK.dll"),
			("win-arm64", "arm64", "ClipDataCloud.SDK.ARM.dll"),
		):
			with self.subTest(processPlatform=processPlatform, fileName=fileName):
				with patch.object(native.sysconfig, "get_platform", return_value=processPlatform):
					path = native.getNativeFilePath(fileName)
				self.assertTrue(path.is_file(), path)
				self.assertEqual(directory, path.parent.name)


if __name__ == "__main__":
	unittest.main()
