# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Run installation tasks for the NVDA Clipboard add-on."""

from __future__ import annotations

import addonHandler
from logHandler import log


_LEGACY_ADDON_NAME = "clipboardEnhancement"


def _removeLegacyAddon() -> None:
	"""Silently request removal of the previous Clipboard Enhancement add-on."""
	try:
		for addon in addonHandler.getAvailableAddons():
			if addon.name == _LEGACY_ADDON_NAME:
				addon.requestRemove()
	except Exception:
		# A failed cleanup must not roll back installation of the new add-on.
		log.exception("Unable to schedule removal of the legacy Clipboard Enhancement add-on")


def onInstall() -> None:
	"""Schedule removal of the legacy add-on without showing a dialog."""
	_removeLegacyAddon()
