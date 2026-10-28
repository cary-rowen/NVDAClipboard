# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Define and access NVDA Clipboard configuration."""

from __future__ import annotations

from enum import StrEnum
from typing import cast

import config


class NavigationSplitMode(StrEnum):
	"""Identify how clipboard navigation units are split."""

	WINDOWS_WORD = "windowsWord"
	PUNCTUATION = "punctuation"


MIN_PAGE_LINE_COUNT = 1
MAX_PAGE_LINE_COUNT = 150

_DEFAULT_PAGE_LINE_COUNT = 10

config.conf.spec.setdefault("nvdaClipboard", {}).update(
	{
		"pageLineCount": (
			f"integer(default={_DEFAULT_PAGE_LINE_COUNT}, min={MIN_PAGE_LINE_COUNT}, max={MAX_PAGE_LINE_COUNT})"
		),
		"navigationSplitMode": (
			f'option("{NavigationSplitMode.WINDOWS_WORD.value}", "{NavigationSplitMode.PUNCTUATION.value}", '
			f'default="{NavigationSplitMode.WINDOWS_WORD.value}")'
		),
		"splitCamelCase": "boolean(default=true)",
		"separatePunctuation": "boolean(default=false)",
		"tiantanEnabled": "boolean(default=false)",
		"autoSyncTiantan": "boolean(default=true)",
		"confirmOnClose": "boolean(default=true)",
		"showClipboardContentAsPlainText": "boolean(default=false)",
		"managerWindow": {
			# Positions use desktop coordinates; sizes use DPI-independent pixels.
			"x": "integer(default=0)",
			"y": "integer(default=0)",
			"width": "integer(default=0, min=0)",
			"height": "integer(default=0, min=0)",
			"maximized": "boolean(default=false)",
			"navigationWidth": "integer(default=280, min=0)",
		},
	},
)


def getSection() -> config.AggregatedSection:
	"""Return the NVDA Clipboard section of NVDA's active configuration."""
	return cast(config.AggregatedSection, config.conf["nvdaClipboard"])


def getPageLineCount() -> int:
	"""Return the configured number of lines moved by clipboard page commands."""
	return int(cast(int, getSection()["pageLineCount"]))


def getNavigationSplitSettings() -> tuple[NavigationSplitMode, bool, bool]:
	"""Return the configured navigation split mode and modifiers."""
	section = getSection()
	return (
		NavigationSplitMode(str(section["navigationSplitMode"])),
		bool(section["splitCamelCase"]),
		bool(section["separatePunctuation"]),
	)


def getTiantanEnabled() -> bool:
	"""Return whether Tiantan cloud synchronization is enabled."""
	return bool(getSection()["tiantanEnabled"])


def getConfirmOnClose() -> bool:
	"""Return whether the manager should prompt before closing with unsaved edits."""
	return bool(getSection()["confirmOnClose"])


def getShowClipboardContentAsPlainText() -> bool:
	"""Return whether browse mode should show clipboard content without rendering."""
	return bool(getSection()["showClipboardContentAsPlainText"])


def getManagerWindowState() -> config.AggregatedSection:
	"""Return the persisted manager window geometry section."""
	return cast(config.AggregatedSection, getSection()["managerWindow"])
