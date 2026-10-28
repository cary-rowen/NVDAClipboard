# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Load the Tiantan Cloud Clipboard SDK once and report whether it can be used."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

import addonHandler
from logHandler import log

from . import cloudClipboard


addonHandler.initTranslation()


@dataclass(frozen=True, slots=True)
class TiantanSupportState:
	"""Describe whether Tiantan Cloud Clipboard can be loaded and hold the loaded SDK."""

	sdk: cloudClipboard.CloudClipboardSdk | None
	statusMessage: str

	@property
	def isAvailable(self) -> bool:
		"""Return whether the ClipDataCloud SDK was loaded successfully."""
		return self.sdk is not None


# Translators: Error shown when the native cloud clipboard library cannot be used.
_SDK_UNAVAILABLE_MESSAGE = _("Tiantan Cloud Clipboard is unavailable. Check the ClipDataCloud SDK.")


@cache
def getTiantanSupportState() -> TiantanSupportState:
	"""Load the ClipDataCloud SDK on first use and return the cached support state."""
	try:
		sdk = cloudClipboard.CloudClipboardSdk()
	except cloudClipboard.CloudClipboardError as error:
		log.debugWarning("ClipDataCloud SDK is unavailable.", exc_info=error)
		return TiantanSupportState(None, _SDK_UNAVAILABLE_MESSAGE)
	except Exception:
		log.exception("Unexpected ClipDataCloud SDK probe failure.")
		return TiantanSupportState(None, _SDK_UNAVAILABLE_MESSAGE)
	return TiantanSupportState(sdk, "")
