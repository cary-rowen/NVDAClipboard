"""Helpers for loading add-on modules in isolated tests."""

from __future__ import annotations

from collections.abc import Mapping
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
import sys
from unittest.mock import patch


def loadAddonModule(
	moduleName: str,
	modulePath: Path,
	*,
	injectedModules: Mapping[str, object] | None = None,
) -> ModuleType:
	"""Load one add-on module with optional temporary import shims."""
	spec = spec_from_file_location(moduleName, modulePath)
	assert spec is not None and spec.loader is not None
	module = module_from_spec(spec)
	sys.modules[moduleName] = module
	packagePrefix = f"{moduleName.rpartition('.')[0]}."
	with patch.dict(sys.modules, dict(injectedModules or {})):
		spec.loader.exec_module(module)
		loadedPackageModules = {
			name: loadedModule
			for name, loadedModule in sys.modules.items()
			if name == moduleName or name.startswith(packagePrefix)
		}
	sys.modules.update(loadedPackageModules)
	return module
