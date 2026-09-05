# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Expose NVDA Clipboard commands as an NVDA global plugin."""

from __future__ import annotations

from collections.abc import Callable
from typing import override

import addonHandler
import globalPluginHandler
import globalVars
import gui
from gui.settingsDialogs import NVDASettingsDialog
import inputCore
from keyboardHandler import KeyboardInputGesture
from logHandler import log
import scriptHandler
from scriptHandler import script
import ui
import wx

from .configuration import getPageLineCount, getTiantanEnabled
from .settings import NVDAClipboardSettingsPanel


addonHandler.initTranslation()

# Translators: Name of the NVDA Clipboard command category in NVDA's Input Gestures dialog.
_SCRIPT_CATEGORY = _("NVDA Clipboard")


def _getKeyboardGestureVkCodes(gesture: inputCore.InputGesture) -> frozenset[int]:
	"""Return the actual main and modifier VK codes for a keyboard gesture."""
	if not isinstance(gesture, KeyboardInputGesture):
		return frozenset()
	return frozenset((gesture.vkCode, *(vkCode for vkCode, _extended in gesture.modifiers)))


def _disableInSecureMode(
	decoratedClass: type[globalPluginHandler.GlobalPlugin],
) -> type[globalPluginHandler.GlobalPlugin]:
	"""Replace the add-on plugin with NVDA's empty base plugin in secure mode."""
	if globalVars.appArgs.secure:
		return globalPluginHandler.GlobalPlugin
	return decoratedClass


@_disableInSecureMode
class GlobalPlugin(globalPluginHandler.GlobalPlugin):
	"""Forward NVDA scripts to the add-on's clipboard controller."""

	scriptCategory = _SCRIPT_CATEGORY

	def __init__(self) -> None:
		super().__init__()
		try:
			from .controller import ClipboardController

			self.controller = ClipboardController(tiantanEnabled=getTiantanEnabled())
			self.controller.start()
			NVDASettingsDialog.categoryClasses.append(NVDAClipboardSettingsPanel)
		except Exception:
			super().terminate()
			raise

	@override
	def terminate(self) -> None:
		"""Terminate the clipboard controller and global plugin."""
		try:
			self.controller.terminate()
		finally:
			NVDASettingsDialog.categoryClasses.remove(NVDAClipboardSettingsPanel)
			super().terminate()

	def _runAction(self, action: Callable[..., None], *args: object) -> None:
		"""Run a controller action and report unexpected user-facing failures."""
		try:
			action(*args)
			return
		except ValueError as error:
			message = str(error)
		except RuntimeError as error:
			log.debugWarning("NVDA Clipboard action could not be completed.", exc_info=True)
			message = str(error)
		except Exception:
			log.exception("Unexpected NVDA Clipboard action failure.")
			message = ""
		if not message:
			# Translators: Generic error reported when an add-on command fails.
			message = _("The NVDA Clipboard command failed")
		ui.message(message)

	@script(
		# Translators: Input help description for reporting or viewing clipboard content.
		description=_("Reports the clipboard summary; press twice to view its content in browse mode"),
		gesture="kb:NVDA+c",
		speakOnDemand=True,
	)
	def script_reportClipboardSummary(self, gesture: inputCore.InputGesture) -> None:
		"""Report a clipboard summary or show its content in browse mode."""
		if scriptHandler.getLastScriptRepeatCount() == 0:
			self._runAction(self.controller.reportClipboardSummary)
		else:
			self._runAction(self.controller.showClipboardContent)

	@script(
		# Translators: Input help description for moving to the first clipboard line.
		description=_("Moves to and reports the first clipboard line"),
		gestures=("kb:control+numpadDivide", "kb(laptop):NVDA+windows+shift+upArrow"),
		speakOnDemand=True,
	)
	def script_firstClipboardLine(self, gesture: inputCore.InputGesture) -> None:
		"""Move to the first clipboard line."""
		self._runAction(self.controller.moveToFirstLine)

	@script(
		# Translators: Input help description for moving to the last clipboard line.
		description=_("Moves to and reports the last clipboard line"),
		gestures=("kb:control+numpadMultiply", "kb(laptop):NVDA+windows+shift+downArrow"),
		speakOnDemand=True,
	)
	def script_lastClipboardLine(self, gesture: inputCore.InputGesture) -> None:
		"""Move to the last clipboard line."""
		self._runAction(self.controller.moveToLastLine)

	@script(
		# Translators: Input help description for moving to the previous clipboard line.
		description=_("Moves to and reports the previous clipboard line"),
		gestures=("kb:control+numpad7", "kb(laptop):NVDA+windows+upArrow"),
		speakOnDemand=True,
	)
	def script_previousClipboardLine(self, gesture: inputCore.InputGesture) -> None:
		"""Move to the previous clipboard line."""
		self._runAction(self.controller.moveLine, -1)

	@script(
		# Translators: Input help description for moving to the next clipboard line.
		description=_("Moves to and reports the next clipboard line"),
		gestures=("kb:control+numpad9", "kb(laptop):NVDA+windows+downArrow"),
		speakOnDemand=True,
	)
	def script_nextClipboardLine(self, gesture: inputCore.InputGesture) -> None:
		"""Move to the next clipboard line."""
		self._runAction(self.controller.moveLine, 1)

	@script(
		# Translators: Input help description for moving up one page through clipboard lines.
		description=_("Moves up one page through the clipboard and reports the resulting line"),
		speakOnDemand=True,
	)
	def script_moveClipboardPageUp(self, gesture: inputCore.InputGesture) -> None:
		"""Move up one configured page through the clipboard lines."""
		self._runAction(self.controller.moveLine, -1, getPageLineCount())

	@script(
		# Translators: Input help description for moving down one page through clipboard lines.
		description=_("Moves down one page through the clipboard and reports the resulting line"),
		speakOnDemand=True,
	)
	def script_moveClipboardPageDown(self, gesture: inputCore.InputGesture) -> None:
		"""Move down one configured page through the clipboard lines."""
		self._runAction(self.controller.moveLine, 1, getPageLineCount())

	@script(
		description=_(
			# Translators: Input help description for reporting the current clipboard line.
			"Reports the current clipboard line. Press twice to spell it and three times for character descriptions.",
		),
		gesture="kb:control+numpad8",
		speakOnDemand=True,
	)
	def script_currentClipboardLine(self, gesture: inputCore.InputGesture) -> None:
		"""Report the current clipboard line using repeat semantics."""
		self._runAction(self.controller.reportCurrentLine, scriptHandler.getLastScriptRepeatCount())

	@script(
		# Translators: Input help description for moving to the previous clipboard navigation unit.
		description=_("Moves to and reports the previous clipboard navigation unit"),
		gestures=("kb:control+numpad4", "kb(laptop):NVDA+shift+windows+leftArrow"),
		speakOnDemand=True,
	)
	def script_previousClipboardNavigationUnit(self, gesture: inputCore.InputGesture) -> None:
		"""Move to the previous clipboard navigation unit."""
		self._runAction(self.controller.moveNavigationUnit, -1)

	@script(
		# Translators: Input help description for moving to the next clipboard navigation unit.
		description=_("Moves to and reports the next clipboard navigation unit"),
		gestures=("kb:control+numpad6", "kb(laptop):NVDA+shift+windows+rightArrow"),
		speakOnDemand=True,
	)
	def script_nextClipboardNavigationUnit(self, gesture: inputCore.InputGesture) -> None:
		"""Move to the next clipboard navigation unit."""
		self._runAction(self.controller.moveNavigationUnit, 1)

	@script(
		description=_(
			# Translators: Input help description for reporting the current clipboard navigation unit.
			"Reports the current clipboard navigation unit. "
			"Press twice to spell it and three times for character descriptions.",
		),
		gestures=("kb:control+numpad5", "kb(laptop):NVDA+shift+windows+."),
		speakOnDemand=True,
	)
	def script_currentClipboardNavigationUnit(self, gesture: inputCore.InputGesture) -> None:
		"""Report the current clipboard navigation unit using repeat semantics."""
		self._runAction(
			self.controller.reportCurrentNavigationUnit,
			scriptHandler.getLastScriptRepeatCount(),
		)

	@script(
		# Translators: Input help description for moving to the previous clipboard character.
		description=_("Moves to and reports the previous clipboard character"),
		gestures=("kb:control+numpad1", "kb(laptop):NVDA+windows+leftArrow"),
		speakOnDemand=True,
	)
	def script_previousClipboardCharacter(self, gesture: inputCore.InputGesture) -> None:
		"""Move to the previous clipboard character."""
		self._runAction(self.controller.moveCharacter, -1)

	@script(
		# Translators: Input help description for moving to the next clipboard character.
		description=_("Moves to and reports the next clipboard character"),
		gestures=("kb:control+numpad3", "kb(laptop):NVDA+windows+rightArrow"),
		speakOnDemand=True,
	)
	def script_nextClipboardCharacter(self, gesture: inputCore.InputGesture) -> None:
		"""Move to the next clipboard character."""
		self._runAction(self.controller.moveCharacter, 1)

	@script(
		description=_(
			# Translators: Input help description for reporting the current clipboard character.
			"Reports the current clipboard character. Press twice for a description and three times for its numeric value.",
		),
		gestures=("kb:control+numpad2", "kb(laptop):NVDA+windows+."),
		speakOnDemand=True,
	)
	def script_currentClipboardCharacter(self, gesture: inputCore.InputGesture) -> None:
		"""Report the current clipboard character using repeat semantics."""
		self._runAction(self.controller.reportCurrentCharacter, scriptHandler.getLastScriptRepeatCount())

	@script(
		# Translators: Input help description for marking the start of clipboard text to paste.
		description=_("Marks the current clipboard navigation position as the selection start"),
		gesture="kb:NVDA+windows+[",
		speakOnDemand=True,
	)
	def script_markClipboardSelectionStart(self, gesture: inputCore.InputGesture) -> None:
		"""Mark the current clipboard navigation position as the selection start."""
		self._runAction(self.controller.markClipboardSelectionStart)

	@script(
		# Translators: Input help description for marking the end of clipboard text to paste.
		description=_("Marks the selection end and selects the clipboard text between both markers"),
		gesture="kb:NVDA+windows+]",
		speakOnDemand=True,
	)
	def script_markClipboardSelectionEnd(self, gesture: inputCore.InputGesture) -> None:
		"""Mark the clipboard selection end and report the selected text."""
		self._runAction(self.controller.markClipboardSelectionEnd)

	@script(
		# Translators: Input help description for pasting the current clipboard-navigation target.
		description=_(
			"Pastes the clipboard selection, or the current stored entry if no selection is marked",
		),
		gesture="kb:NVDA+windows+v",
		speakOnDemand=True,
	)
	def script_pasteCurrentNavigationTarget(self, gesture: inputCore.InputGesture) -> None:
		"""Paste the complete clipboard selection or current stored entry temporarily."""
		self._runAction(
			self.controller.pasteCurrentNavigationTarget,
			_getKeyboardGestureVkCodes(gesture),
		)

	@script(
		# Translators: Input help description for opening the clipboard manager.
		description=_("Opens the Clipboard Manager"),
		gesture="kb:NVDA+e",
		speakOnDemand=True,
	)
	@gui.blockAction.when(gui.blockAction.Context.MODAL_DIALOG_OPEN)
	def script_openClipboardManager(self, gesture: inputCore.InputGesture) -> None:
		"""Open the reusable clipboard manager."""
		self._runAction(self.controller.showManager)

	@script(
		# Translators: Input help description for appending selected text to the clipboard.
		description=_("Appends selected text to the clipboard"),
		gesture="kb:NVDA+alt+a",
		speakOnDemand=True,
	)
	def script_appendSelectedText(self, gesture: inputCore.InputGesture) -> None:
		"""Append selected text on the first press only."""
		if scriptHandler.getLastScriptRepeatCount() == 0:
			self._runAction(self.controller.appendSelectedText)

	@script(
		# Translators: Input help description for appending the last spoken text.
		description=_("Appends the last spoken text to the clipboard"),
		gesture="kb:NVDA+shift+x",
		speakOnDemand=True,
	)
	def script_appendLastSpokenText(self, gesture: inputCore.InputGesture) -> None:
		"""Append the last spoken text to the clipboard."""
		self._runAction(self.controller.appendLastSpokenText)

	@script(
		# Translators: Input help description for temporarily pasting the last spoken text.
		description=_("Pastes the last spoken text without keeping it in clipboard history"),
		gesture="kb:NVDA+`",
		speakOnDemand=True,
	)
	def script_pasteLastSpokenText(self, gesture: inputCore.InputGesture) -> None:
		"""Paste the last spoken text through a temporary clipboard transaction."""
		self._runAction(self.controller.pasteLastSpokenText, _getKeyboardGestureVkCodes(gesture))

	@script(
		# Translators: Input help description for receiving and pasting Tiantan Cloud Clipboard text.
		description=_("Receives and pastes from Tiantan Cloud Clipboard"),
		gesture="kb:NVDA+alt+v",
		speakOnDemand=True,
	)
	def script_pasteCloudClipboard(self, gesture: inputCore.InputGesture) -> None:
		"""Receive and paste Tiantan Cloud Clipboard text."""
		self._runAction(self.controller.pasteCloudText, _getKeyboardGestureVkCodes(gesture))

	@script(
		# Translators: Input help description for copying a navigator object screenshot.
		description=_("Copies an image of the current navigator object to the clipboard"),
		gesture="kb:NVDA+printScreen",
		speakOnDemand=True,
	)
	def script_copyNavigatorObjectImage(self, gesture: inputCore.InputGesture) -> None:
		"""Copy an image of the current navigator object."""
		self._runAction(self.controller.copyNavigatorObjectImage)

	@script(
		# Translators: Input help description for cycling through categories of stored entries.
		description=_("Cycles through categories of stored entries"),
		speakOnDemand=True,
	)
	def script_cycleStoredItemCategory(self, gesture: inputCore.InputGesture) -> None:
		"""Select the next category of stored items."""
		self._runAction(self.controller.cycleStoredItemCategory)

	@script(
		# Translators: Input help description for moving to the next stored entry in the current category.
		description=_("Moves to the next stored entry in the current category"),
		gestures=("kb:control+windows+numpadPlus", "kb(laptop):control+windows+]"),
		speakOnDemand=True,
	)
	def script_nextStoredItem(self, gesture: inputCore.InputGesture) -> None:
		"""Move to the next stored item in the current category."""
		self._runAction(self.controller.moveToNextStoredItem)

	@script(
		# Translators: Input help description for moving to the previous stored entry in the current category.
		description=_("Moves to the previous stored entry in the current category"),
		gestures=("kb:control+windows+numpadMinus", "kb(laptop):control+windows+["),
		speakOnDemand=True,
	)
	def script_previousStoredItem(self, gesture: inputCore.InputGesture) -> None:
		"""Move to the previous stored item in the current category."""
		self._runAction(self.controller.moveToPreviousStoredItem)

	@script(
		# Translators: Input help description for putting the selected stored entry on the system clipboard.
		description=_("Puts the selected stored entry on the system clipboard"),
		gestures=("kb:control+windows+numpadMultiply", "kb(laptop):control+windows+\\"),
		speakOnDemand=True,
	)
	def script_restoreStoredItem(self, gesture: inputCore.InputGesture) -> None:
		"""Put the selected stored item on the system clipboard."""
		self._runAction(self.controller.restoreCurrentStoredItem)

	@script(
		# Translators: Input help description for saving a clipboard image.
		description=_("Saves the clipboard image to a file"),
		speakOnDemand=True,
	)
	@gui.blockAction.when(gui.blockAction.Context.MODAL_DIALOG_OPEN)
	def script_saveCurrentClipboardImage(self, gesture: inputCore.InputGesture) -> None:
		"""Save the current clipboard image to a file."""
		wx.CallAfter(self._runAction, self.controller.saveCurrentClipboardImage)
