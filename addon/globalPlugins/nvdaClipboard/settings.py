# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Provide configuration and the NVDA Clipboard settings panel."""

from typing import override

import addonHandler
import config
import core
import gui
from gui import guiHelper, nvdaControls
from gui.settingsDialogs import SettingsPanel
import queueHandler
import wx

from .configuration import (
	MAX_PAGE_LINE_COUNT,
	MIN_PAGE_LINE_COUNT,
	NavigationSplitMode,
	getNavigationSplitSettings,
	getPageLineCount,
	getTiantanEnabled,
	saveSettings,
)
from .tiantanSupport import getTiantanSupportState


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
		navigationSplitMode, splitCamelCase, separatePunctuation = getNavigationSplitSettings()
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
		self.splitCamelCaseCheckBox = sizerHelper.addItem(
			# Translators: Checkbox to navigate joined words such as "getWord" as separate units.
			wx.CheckBox(self, label=_("Split joined words by &capitalization")),
		)
		self.splitCamelCaseCheckBox.SetValue(splitCamelCase)
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

		self._initialTiantanEnabled = getTiantanEnabled()
		self._tiantanSupportState = getTiantanSupportState()
		self.tiantanEnabledCheckBox = sizerHelper.addItem(
			wx.CheckBox(
				self,
				# Translators: Checkbox to enable Tiantan Cloud Clipboard after NVDA restarts.
				label=_("Enable &Tiantan Cloud Clipboard (requires restart)"),
			),
		)
		self.tiantanEnabledCheckBox.SetValue(self._initialTiantanEnabled)
		self.tiantanEnabledCheckBox.Enable(
			self._tiantanSupportState.isAvailable or self._initialTiantanEnabled,
		)
		if self._tiantanSupportState.isAvailable:
			sizerHelper.addItem(
				wx.StaticText(
					self,
					# Translators: Note shown under the Tiantan Cloud Clipboard option.
					label=_("Tiantan Cloud Clipboard loads only after NVDA restarts."),
				),
			)
		else:
			sizerHelper.addItem(
				wx.StaticText(
					self,
					label=self._tiantanSupportState.statusMessage,
				),
			)

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
			splitCamelCase=self.splitCamelCaseCheckBox.GetValue(),
			separatePunctuation=self.separatePunctuationCheckBox.GetValue(),
			tiantanEnabled=self.tiantanEnabledCheckBox.GetValue(),
		)

	@override
	def postSave(self) -> None:
		"""Offer to restart NVDA after changing Tiantan Cloud Clipboard availability."""
		if self._initialTiantanEnabled == getTiantanEnabled():
			return
		result = gui.messageBox(
			_(
				# Translators: Message shown after changing Tiantan Cloud Clipboard availability.
				"Tiantan Cloud Clipboard changes require NVDA to restart. Restart now?",
			),
			# Translators: Title for the restart prompt after changing Tiantan Cloud Clipboard availability.
			_("Restart NVDA"),
			wx.YES | wx.NO | wx.ICON_WARNING,
		)
		if result == wx.YES:
			config.conf.save()
			queueHandler.queueFunction(queueHandler.eventQueue, core.restart)
