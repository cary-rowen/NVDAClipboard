# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Helpers for loading add-on modules in isolated tests."""

from __future__ import annotations

from collections.abc import Mapping
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
import sys
from unittest.mock import patch


ADDON_PACKAGE_DIRECTORY = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard"
"""Source directory of the ``nvdaClipboard`` global plugin package."""


def addonSourcePath(relativeModuleName: str) -> Path:
	"""Return the source file of a module given its dotted name relative to the add-on package.

	``"storage.database"`` resolves to ``storage/database.py``; a sub-package name
	resolves to its ``__init__.py``.
	"""
	path = ADDON_PACKAGE_DIRECTORY.joinpath(*relativeModuleName.split("."))
	if path.is_dir():
		return path / "__init__.py"
	return path.with_suffix(".py")


def createAddonPackage(packageName: str) -> ModuleType:
	"""Register an isolated alias of the add-on package backed by its real source directory.

	Modules below the alias are imported from the real package files, including
	sub-packages and relative imports, while the package ``__init__`` (which needs
	NVDA) is not executed. Each test module should use its own alias name so that
	its injected fakes do not leak into other tests.
	"""
	package = ModuleType(packageName)
	package.__path__ = [str(ADDON_PACKAGE_DIRECTORY)]
	package.__package__ = packageName
	sys.modules[packageName] = package
	return package


def loadAddonModule(
	moduleName: str,
	modulePath: Path | None = None,
	*,
	injectedModules: Mapping[str, object] | None = None,
) -> ModuleType:
	"""Load one add-on module with optional temporary import shims.

	``moduleName`` is normally ``"<packageAlias>.<relative.dotted.name>"`` for an alias
	created by ``createAddonPackage``; the source path is then derived from it. The
	module is always executed afresh, after importing its parent packages. Add-on
	modules imported while loading (below the same top-level alias) stay registered
	after the shims are removed.
	"""
	rootName, _, relativeName = moduleName.partition(".")
	if modulePath is None:
		if rootName not in sys.modules or not relativeName:
			raise ValueError(f"{moduleName!r} is not below a package created by createAddonPackage")
		modulePath = addonSourcePath(relativeName)
	spec = spec_from_file_location(
		moduleName,
		modulePath,
		submodule_search_locations=[str(modulePath.parent)] if modulePath.name == "__init__.py" else None,
	)
	assert spec is not None and spec.loader is not None
	module = module_from_spec(spec)
	packagePrefix = f"{rootName}."
	parentName, _, childName = moduleName.rpartition(".")
	with patch.dict(sys.modules, dict(injectedModules or {})):
		parent = import_module(parentName) if parentName else None
		sys.modules[moduleName] = module
		spec.loader.exec_module(module)
		if parent is not None:
			setattr(parent, childName, module)
		loadedPackageModules = {
			name: loadedModule
			for name, loadedModule in sys.modules.items()
			if name in (rootName, moduleName) or name.startswith(packagePrefix)
		}
	sys.modules.update(loadedPackageModules)
	return module
