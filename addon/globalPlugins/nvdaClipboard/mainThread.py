# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Queue work from background threads onto wx's GUI thread."""

from __future__ import annotations

from collections.abc import Callable

from logHandler import log
import wx


__all__ = ["queueMainThread"]


def queueMainThread(
	callback: Callable[..., object],
	*args: object,
	onFailure: Callable[[], bool],
	failureMessage: str,
) -> bool:
	"""Queue a callback on wx's GUI thread and return whether wx accepted it.

	wx raises :class:`RuntimeError` once it no longer accepts work, typically during shutdown.
	In that case ``onFailure`` releases any caller state and returns whether the owner is still
	active, and ``failureMessage`` is logged as a debug warning only for an active owner.
	"""
	try:
		wx.CallAfter(callback, *args)
	except RuntimeError:
		if onFailure():
			log.debugWarning(failureMessage, exc_info=True)
		return False
	return True
