# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Load the real clipboard manager modules with fake wx, GUI, and NVDA modules."""

from __future__ import annotations

from collections.abc import Mapping
import sys
from types import ModuleType
from unittest.mock import Mock

from tests._module_loader import createAddonPackage, loadAddonModule


def _translate(message: str) -> str:
	"""Return an untranslated message for standalone validation."""
	return message


def _ngettext(singular: str, plural: str, count: int) -> str:
	"""Choose the English singular or plural form without translation."""
	return singular if count == 1 else plural


def _pgettext(_context: str, message: str) -> str:
	"""Return an untranslated message while ignoring its context."""
	return message


def _npgettext(_context: str, singular: str, plural: str, count: int) -> str:
	"""Choose the English plural form while ignoring its context."""
	return _ngettext(singular, plural, count)


def _initTranslation() -> None:
	"""Install the standalone translators in the importing module globals."""
	callerGlobals = sys._getframe(1).f_globals
	callerGlobals["_"] = _translate
	callerGlobals["ngettext"] = _ngettext
	callerGlobals["pgettext"] = _pgettext
	callerGlobals["npgettext"] = _npgettext


class _PlaceholderModule(ModuleType):
	"""Fake module whose unknown attributes are distinct placeholder classes.

	Placeholder classes can be used as base classes, called, or compared, which
	covers module-level uses such as ``class Frame(wx.Frame)``. Tests patch the
	loaded module's globals when a method needs specific fake behavior.
	"""

	def __getattr__(self, name: str) -> type:
		"""Create and remember a placeholder class for an unknown attribute."""
		if name.startswith("__"):
			raise AttributeError(name)
		placeholder = type(name, (), {"__module__": self.__name__})
		setattr(self, name, placeholder)
		return placeholder


def fakeNvdaModules(
	attributes: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
	"""Return fake wx, GUI, and NVDA modules needed to import the manager modules.

	``attributes`` maps a fake module name (``"wx"``, ``"textUtils"`` ...) to extra
	attributes to set on that module.
	"""
	modules: dict[str, ModuleType] = {
		name: _PlaceholderModule(name)
		for name in (
			"addonHandler",
			"api",
			"config",
			"gui",
			"gui.message",
			"gui.nvdaControls",
			"logHandler",
			"nvwave",
			"textUtils",
			"ui",
			"winUser",
			"wx",
		)
	}
	modules["addonHandler"].initTranslation = _initTranslation
	modules["config"].conf = Mock()
	modules["gui"].message = modules["gui.message"]
	modules["gui"].nvdaControls = modules["gui.nvdaControls"]
	modules["gui.message"].displayDialogAsModal = Mock()
	modules["logHandler"].log = Mock()
	modules["nvwave"].playWaveFile = Mock()
	modules["ui"].message = Mock()
	for moduleName, moduleAttributes in (attributes or {}).items():
		for name, value in moduleAttributes.items():
			setattr(modules[moduleName], name, value)
	return dict(modules)


def loadManagerModule(packageName: str) -> ModuleType:
	"""Import the real clipboard manager module below a fresh add-on package alias."""
	createAddonPackage(packageName)
	return loadAddonModule(f"{packageName}.manager.window", injectedModules=fakeNvdaModules())
