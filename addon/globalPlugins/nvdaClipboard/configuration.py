# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Define and access NVDA Clipboard configuration."""

from enum import StrEnum

import config


class NavigationSplitMode(StrEnum):
	"""Identify how clipboard navigation units are split."""

	WINDOWS_WORD = "windowsWord"
	PUNCTUATION = "punctuation"


MIN_PAGE_LINE_COUNT = 1
MAX_PAGE_LINE_COUNT = 150

_CONFIG_SECTION = "nvdaClipboard"
_CONFIG_PAGE_LINE_COUNT = "pageLineCount"
_CONFIG_NAVIGATION_SPLIT_MODE = "navigationSplitMode"
_CONFIG_SPLIT_CAMEL_CASE = "splitCamelCase"
_CONFIG_SEPARATE_PUNCTUATION = "separatePunctuation"
_CONFIG_TIANTAN_ENABLED = "tiantanEnabled"
_CONFIG_CONFIRM_ON_CLOSE = "confirmOnClose"
_DEFAULT_PAGE_LINE_COUNT = 10

_sectionSpec = config.conf.spec.setdefault(_CONFIG_SECTION, {})
_sectionSpec.update(
	{
		_CONFIG_PAGE_LINE_COUNT: (
			f"integer(default={_DEFAULT_PAGE_LINE_COUNT}, min={MIN_PAGE_LINE_COUNT}, max={MAX_PAGE_LINE_COUNT})"
		),
		_CONFIG_NAVIGATION_SPLIT_MODE: (
			f'option("{NavigationSplitMode.WINDOWS_WORD.value}", "{NavigationSplitMode.PUNCTUATION.value}", '
			f'default="{NavigationSplitMode.WINDOWS_WORD.value}")'
		),
		_CONFIG_SPLIT_CAMEL_CASE: "boolean(default=true)",
		_CONFIG_SEPARATE_PUNCTUATION: "boolean(default=false)",
		_CONFIG_TIANTAN_ENABLED: "boolean(default=false)",
		_CONFIG_CONFIRM_ON_CLOSE: "boolean(default=true)",
	},
)


def getPageLineCount() -> int:
	"""Return the configured number of lines moved by clipboard page commands."""
	return int(config.conf[_CONFIG_SECTION][_CONFIG_PAGE_LINE_COUNT])


def getNavigationSplitSettings() -> tuple[NavigationSplitMode, bool, bool]:
	"""Return the configured navigation split mode and modifiers."""
	section = config.conf[_CONFIG_SECTION]
	return (
		NavigationSplitMode(str(section[_CONFIG_NAVIGATION_SPLIT_MODE])),
		bool(section[_CONFIG_SPLIT_CAMEL_CASE]),
		bool(section[_CONFIG_SEPARATE_PUNCTUATION]),
	)


def getTiantanEnabled() -> bool:
	"""Return whether Tiantan cloud synchronization is enabled."""
	return bool(config.conf[_CONFIG_SECTION][_CONFIG_TIANTAN_ENABLED])


def getConfirmOnClose() -> bool:
	"""Return whether the manager should prompt before closing with unsaved edits."""
	return bool(config.conf[_CONFIG_SECTION][_CONFIG_CONFIRM_ON_CLOSE])


def saveSettings(
	*,
	pageLineCount: int,
	navigationSplitMode: NavigationSplitMode,
	splitCamelCase: bool,
	separatePunctuation: bool,
	tiantanEnabled: bool,
	confirmOnClose: bool,
) -> None:
	"""Save settings from the NVDA Clipboard settings panel."""
	section = config.conf[_CONFIG_SECTION]
	section[_CONFIG_PAGE_LINE_COUNT] = pageLineCount
	section[_CONFIG_NAVIGATION_SPLIT_MODE] = navigationSplitMode.value
	section[_CONFIG_SPLIT_CAMEL_CASE] = splitCamelCase
	section[_CONFIG_SEPARATE_PUNCTUATION] = separatePunctuation
	section[_CONFIG_TIANTAN_ENABLED] = tiantanEnabled
	section[_CONFIG_CONFIRM_ON_CLOSE] = confirmOnClose
