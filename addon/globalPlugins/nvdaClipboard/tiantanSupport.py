# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Probe whether Tiantan Cloud Clipboard can be used on this machine."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
import sys

import addonHandler
from logHandler import log

from . import cloudClipboard


addonHandler.initTranslation()


@dataclass(frozen=True, slots=True)
class TiantanSupportState:
	"""Describe whether Tiantan Cloud Clipboard can be loaded."""

	isAvailable: bool
	statusMessage: str


# Translators: Error shown when Tiantan Cloud Clipboard is not supported by the current NVDA process bitness.
_UNSUPPORTED_ARCHITECTURE_MESSAGE = _("Tiantan Cloud Clipboard requires 64-bit NVDA.")
# Translators: Error shown when the native cloud clipboard library cannot be used.
_SDK_UNAVAILABLE_MESSAGE = _("Tiantan Cloud Clipboard is unavailable. Check the ClipDataCloud SDK.")


@cache
def getTiantanSupportState() -> TiantanSupportState:
	"""Return the cached Tiantan Cloud Clipboard support state."""
	if sys.maxsize <= 2**32:
		log.debugWarning("Tiantan Cloud Clipboard requires a 64-bit NVDA process.")
		return TiantanSupportState(False, _UNSUPPORTED_ARCHITECTURE_MESSAGE)
	try:
		cloudClipboard.CloudClipboardSdk()
	except cloudClipboard.CloudClipboardError as error:
		log.debugWarning("ClipDataCloud SDK is unavailable.", exc_info=error)
		return TiantanSupportState(False, _SDK_UNAVAILABLE_MESSAGE)
	except Exception:
		log.exception("Unexpected ClipDataCloud SDK probe failure.")
		return TiantanSupportState(False, _SDK_UNAVAILABLE_MESSAGE)
	return TiantanSupportState(True, "")
