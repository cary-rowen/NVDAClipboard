# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Provide configuration and the NVDA Clipboard settings panel."""

from typing import override

import addonHandler
import config
from gui import guiHelper, nvdaControls
from gui.settingsDialogs import SettingsPanel
import wx


addonHandler.initTranslation()

_CONFIG_SECTION = "nvdaClipboard"
_CONFIG_PAGE_LINE_COUNT = "pageLineCount"
_MIN_PAGE_LINE_COUNT = 1
_MAX_PAGE_LINE_COUNT = 150
_DEFAULT_PAGE_LINE_COUNT = 10

config.conf.spec.setdefault(_CONFIG_SECTION, {})[_CONFIG_PAGE_LINE_COUNT] = (
	f"integer(default={_DEFAULT_PAGE_LINE_COUNT}, min={_MIN_PAGE_LINE_COUNT}, max={_MAX_PAGE_LINE_COUNT})"
)


def getPageLineCount() -> int:
	"""Return the configured number of lines moved by clipboard page commands."""
	return int(config.conf[_CONFIG_SECTION][_CONFIG_PAGE_LINE_COUNT])


class NVDAClipboardSettingsPanel(SettingsPanel):
	"""Configure NVDA Clipboard from NVDA's multi-category settings dialog."""

	# Translators: Title of the NVDA Clipboard settings panel.
	title = _("NVDA Clipboard")

	@override
	def makeSettings(self, settingsSizer: wx.BoxSizer) -> None:
		"""Add NVDA Clipboard controls to the settings panel."""
		sizerHelper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		# Translators: Label for the number of clipboard lines moved by the page up and page down commands.
		label = _("&Number of lines to move when paging up or down:")
		self.pageLineCountSpin = sizerHelper.addLabeledControl(
			label,
			nvdaControls.SelectOnFocusSpinCtrl,
			min=_MIN_PAGE_LINE_COUNT,
			max=_MAX_PAGE_LINE_COUNT,
			initial=getPageLineCount(),
		)

	@override
	def onSave(self) -> None:
		"""Save the configured clipboard page line count."""
		config.conf[_CONFIG_SECTION][_CONFIG_PAGE_LINE_COUNT] = self.pageLineCountSpin.GetValue()
