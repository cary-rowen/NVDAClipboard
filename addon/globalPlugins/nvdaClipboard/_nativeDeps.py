"""Resolve bundled native runtime dependencies for the NVDA Clipboard add-on."""

from __future__ import annotations

from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import os
import platform
import sys
from types import ModuleType
from typing import Iterator


_PACKAGE_DIR = Path(__file__).resolve().parent
_DEPS_ROOT = _PACKAGE_DIR / "_vendor"
_NATIVE_ROOT = _DEPS_ROOT / "_native"
_AMD64_DIRECTORY = _NATIVE_ROOT / "amd64"
_ARM64_DIRECTORY = _NATIVE_ROOT / "arm64"
_DLL_DIRECTORY_COOKIE = None


def getNativeDirectory(machine: str | None = None) -> Path:
	"""Return the bundled native directory for one Windows architecture."""
	if machine is None:
		machine = platform.machine()
	if machine.upper() == "ARM64":
		return _ARM64_DIRECTORY
	return _AMD64_DIRECTORY


def getNativeFilePath(fileName: str, machine: str | None = None) -> Path:
	"""Return one bundled native file path for the current architecture."""
	return getNativeDirectory(machine) / fileName


@contextmanager
def _addDllDirectory(directory: Path) -> Iterator[None]:
	"""Temporarily add one DLL search directory when the platform supports it."""
	if not hasattr(os, "add_dll_directory"):
		yield
		return
	global _DLL_DIRECTORY_COOKIE
	_DLL_DIRECTORY_COOKIE = os.add_dll_directory(str(directory))
	try:
		yield
	finally:
		_DLL_DIRECTORY_COOKIE = None


def loadExtensionModule(moduleName: str, fileName: str) -> ModuleType:
	"""Load one bundled CPython extension module from the native tree."""
	extensionPath = getNativeFilePath(fileName)
	if not extensionPath.is_file():
		raise ImportError(f"Bundled native module not found: {extensionPath}")
	with _addDllDirectory(extensionPath.parent):
		spec = spec_from_file_location(moduleName, extensionPath)
		if spec is None or spec.loader is None:
			raise ImportError(f"Bundled native module could not be loaded: {extensionPath}")
		module = module_from_spec(spec)
		sys.modules[moduleName] = module
		spec.loader.exec_module(module)
	return module
