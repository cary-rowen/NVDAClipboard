# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Resolve bundled native runtime dependencies for the NVDA Clipboard add-on."""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import os
import sys
import sysconfig
from types import ModuleType


_PACKAGE_DIR = Path(__file__).resolve().parent
_DEPS_ROOT = _PACKAGE_DIR / "_vendor"
_NATIVE_ROOT = _DEPS_ROOT / "_native"
_AMD64_DIRECTORY = _NATIVE_ROOT / "amd64"
_ARM64_DIRECTORY = _NATIVE_ROOT / "arm64"


def getNativeFilePath(fileName: str) -> Path:
	"""Return one bundled native file path for the current architecture."""
	# platform.machine() reports the host CPU; x64 NVDA runs emulated on ARM64 Windows and needs AMD64 binaries.
	directory = _ARM64_DIRECTORY if sysconfig.get_platform() == "win-arm64" else _AMD64_DIRECTORY
	return directory / fileName


def loadExtensionModule(moduleName: str, fileName: str) -> ModuleType:
	"""Load one bundled CPython extension module from the native tree."""
	extensionPath = getNativeFilePath(fileName)
	if not extensionPath.is_file():
		raise ImportError(f"Bundled native module not found: {extensionPath}")
	with os.add_dll_directory(str(extensionPath.parent)):
		spec = spec_from_file_location(moduleName, extensionPath)
		if spec is None or spec.loader is None:
			raise ImportError(f"Bundled native module could not be loaded: {extensionPath}")
		module = module_from_spec(spec)
		sys.modules[moduleName] = module
		spec.loader.exec_module(module)
	return module
