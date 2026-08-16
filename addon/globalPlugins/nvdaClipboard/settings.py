# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Provide configuration and the NVDA Clipboard settings panel."""

from typing import override

import addonHandler
from gui import guiHelper, nvdaControls
from gui.settingsDialogs import SettingsPanel
import wx

from .configuration import (
	MAX_PAGE_LINE_COUNT,
	MIN_PAGE_LINE_COUNT,
	NavigationSplitMode,
	getNavigationSplitSettings,
	getPageLineCount,
	saveSettings,
)


addonHandler.initTranslation()

_NAVIGATION_SPLIT_MODES = tuple(NavigationSplitMode)
_PUNCTUATION_MODE_INDEX = _NAVIGATION_SPLIT_MODES.index(NavigationSplitMode.PUNCTUATION)


class NVDAClipboardSettingsPanel(SettingsPanel):
	"""Configure NVDA Clipboard from NVDA's multi-category settings dialog."""

	# Translators: Title of the NVDA Clipboard settings panel.
	title = _("NVDA Clipboard")

	@override
	def makeSettings(self, settingsSizer: wx.BoxSizer) -> None:
		"""Add NVDA Clipboard controls to the settings panel."""
		sizerHelper = guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
		navigationSplitMode, separatePunctuation = getNavigationSplitSettings()
		# Translators: Label for choosing how previous, current, and next navigation units are split.
		navigationSplitLabel = _("Navigation unit &splitting:")
		self.navigationSplitChoice = sizerHelper.addLabeledControl(
			navigationSplitLabel,
			wx.Choice,
			choices=(
				# Translators: Split clipboard navigation units using Windows word boundaries.
				_("Windows word boundaries"),
				# Translators: Split clipboard navigation units at Unicode punctuation characters.
				_("Split at punctuation"),
			),
		)
		self.navigationSplitChoice.SetSelection(_NAVIGATION_SPLIT_MODES.index(navigationSplitMode))
		self.separatePunctuationCheckBox = sizerHelper.addItem(
			wx.CheckBox(
				self,
				label=_(
					# Translators: Checkbox to make each punctuation character a separate clipboard navigation unit.
					"Treat &punctuation as separate navigation units",
				),
			),
		)
		self.separatePunctuationCheckBox.SetValue(separatePunctuation)
		self.separatePunctuationCheckBox.Enable(navigationSplitMode is NavigationSplitMode.PUNCTUATION)
		self.navigationSplitChoice.Bind(wx.EVT_CHOICE, self._onNavigationSplitModeChange)

		# Translators: Label for the number of clipboard lines moved by the page up and page down commands.
		label = _("&Number of lines to move when paging up or down:")
		self.pageLineCountSpin = sizerHelper.addLabeledControl(
			label,
			nvdaControls.SelectOnFocusSpinCtrl,
			min=MIN_PAGE_LINE_COUNT,
			max=MAX_PAGE_LINE_COUNT,
			initial=getPageLineCount(),
		)

	def _onNavigationSplitModeChange(self, event: wx.CommandEvent) -> None:
		"""Update punctuation control availability after a mode change."""
		self.separatePunctuationCheckBox.Enable(event.GetSelection() == _PUNCTUATION_MODE_INDEX)

	@override
	def onSave(self) -> None:
		"""Save the configured clipboard navigation settings."""
		saveSettings(
			pageLineCount=self.pageLineCountSpin.GetValue(),
			navigationSplitMode=_NAVIGATION_SPLIT_MODES[self.navigationSplitChoice.GetSelection()],
			separatePunctuation=self.separatePunctuationCheckBox.GetValue(),
		)
