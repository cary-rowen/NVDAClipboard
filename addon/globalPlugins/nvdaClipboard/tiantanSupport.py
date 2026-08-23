# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Probe whether Tiantan Cloud Clipboard can be used on this machine."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
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


def _getUnsupportedArchitectureMessage() -> str:
	# Translators: Error shown when Tiantan Cloud Clipboard is not supported by the current NVDA process bitness.
	return _("Tiantan Cloud Clipboard requires x64 NVDA.")


def _getSdkUnavailableMessage() -> str:
	# Translators: Error shown when the native cloud clipboard library cannot be used.
	return _("Tiantan Cloud Clipboard is unavailable. Check ClipDataCloud.SDK.dll.")


@lru_cache(maxsize=1)
def getTiantanSupportState() -> TiantanSupportState:
	"""Return the cached Tiantan Cloud Clipboard support state."""
	if sys.maxsize <= 2**32:
		log.debugWarning("Tiantan Cloud Clipboard requires a 64-bit NVDA process.")
		return TiantanSupportState(False, _getUnsupportedArchitectureMessage())
	try:
		cloudClipboard.CloudClipboardSdk()
	except cloudClipboard.CloudClipboardError as error:
		log.debugWarning("ClipDataCloud SDK is unavailable.", exc_info=error)
		return TiantanSupportState(False, _getSdkUnavailableMessage())
	except Exception:
		log.exception("Unexpected ClipDataCloud SDK probe failure.")
		return TiantanSupportState(False, _getSdkUnavailableMessage())
	return TiantanSupportState(True, "")
