# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Provide text editing commands for the clipboard manager content control."""

from __future__ import annotations

from collections.abc import Callable
import re
from typing import override

import addonHandler
from gui import guiHelper
from gui.message import displayDialogAsModal
import textUtils
import wx

from ..textSearch import findPreviousLiteralMatch, matchRegexSelection


addonHandler.initTranslation()


class _TextSearchDialog(wx.Dialog):
	"""Lay out the controls shared by the find and replace dialogs."""

	def __init__(
		self,
		parent: wx.Window,
		*,
		title: str,
		findLabel: str,
		findText: str,
		matchCaseLabel: str,
		matchCase: bool,
	) -> None:
		"""Create the search field, extra fields, options, and buttons in Tab order."""
		super().__init__(parent, title=title)
		mainSizer = wx.BoxSizer(wx.VERTICAL)
		sHelper = guiHelper.BoxSizerHelper(self, orientation=wx.VERTICAL)
		self.findCtrl: wx.TextCtrl = sHelper.addLabeledControl(findLabel, wx.TextCtrl, value=findText)
		self._addTextFields(sHelper)
		self.matchCaseCheckBox: wx.CheckBox = sHelper.addItem(wx.CheckBox(self, label=matchCaseLabel))
		self.matchCaseCheckBox.SetValue(matchCase)
		self._addOptions(sHelper)
		self._addButtons(sHelper)
		mainSizer.Add(sHelper.sizer, border=guiHelper.BORDER_FOR_DIALOGS, flag=wx.ALL | wx.EXPAND)
		self.SetSizerAndFit(mainSizer)
		self.SetMinSize((400, self.GetBestSize().height))
		self.findCtrl.SetFocus()

	def _addTextFields(self, sHelper: guiHelper.BoxSizerHelper) -> None:
		"""Add text fields that follow the search field; none by default."""

	def _addOptions(self, sHelper: guiHelper.BoxSizerHelper) -> None:
		"""Add the check boxes that follow the case option."""
		raise NotImplementedError

	def _addButtons(self, sHelper: guiHelper.BoxSizerHelper) -> None:
		"""Add the buttons that dismiss the dialog."""
		raise NotImplementedError


class _FindDialog(_TextSearchDialog):
	"""Collect literal text search options for manager content."""

	def __init__(
		self,
		parent: wx.Window,
		findText: str = "",
		matchCase: bool = False,
	) -> None:
		"""Create a find dialog with the previous search values."""
		super().__init__(
			parent,
			# Translators: Title of the text search dialog in the clipboard manager.
			title=_("Find Text"),
			# Translators: Label for the text to find in clipboard manager content.
			findLabel=_("&Find:"),
			findText=findText,
			# Translators: Checkbox enabling case-sensitive text search.
			matchCaseLabel=_("Match &case"),
			matchCase=matchCase,
		)

	@override
	def _addOptions(self, sHelper: guiHelper.BoxSizerHelper) -> None:
		"""Add the search direction option."""
		self.backwardCheckBox: wx.CheckBox = sHelper.addItem(
			wx.CheckBox(
				self,
				# Translators: Checkbox selecting backward text search.
				label=_("Search &backward"),
			),
		)

	@override
	def _addButtons(self, sHelper: guiHelper.BoxSizerHelper) -> None:
		"""Add the standard OK and Cancel buttons; OK is the default button."""
		sHelper.addDialogDismissButtons(wx.OK | wx.CANCEL, separated=True)

	def getValues(self) -> tuple[str, bool, bool]:
		"""Return the entered text, case option, and search direction."""
		return (
			self.findCtrl.GetValue(),
			self.matchCaseCheckBox.GetValue(),
			self.backwardCheckBox.GetValue(),
		)


class _ReplaceDialog(_TextSearchDialog):
	"""Collect literal or regular-expression replacement options."""

	def __init__(
		self,
		parent: wx.Window,
		findText: str = "",
		matchCase: bool = False,
	) -> None:
		"""Create a replacement dialog with the previous search values."""
		super().__init__(
			parent,
			# Translators: Title of the text replacement dialog.
			title=_("Replace Text"),
			# Translators: Label for the text to find in the replacement dialog.
			findLabel=_("&Find:"),
			findText=findText,
			# Translators: Checkbox enabling case-sensitive replacement matching.
			matchCaseLabel=_("Match &case"),
			matchCase=matchCase,
		)

	@override
	def _addTextFields(self, sHelper: guiHelper.BoxSizerHelper) -> None:
		"""Add the replacement text field after the search field."""
		self.replaceCtrl: wx.TextCtrl = sHelper.addLabeledControl(
			# Translators: Label for replacement text in the replacement dialog.
			_("&Replace with:"),
			wx.TextCtrl,
		)

	@override
	def _addOptions(self, sHelper: guiHelper.BoxSizerHelper) -> None:
		"""Add the regular expression option."""
		self.regexCheckBox: wx.CheckBox = sHelper.addItem(
			wx.CheckBox(
				self,
				# Translators: Checkbox enabling regular expressions in replacement.
				label=_("Use regular e&xpressions"),
			),
		)

	@override
	def _addButtons(self, sHelper: guiHelper.BoxSizerHelper) -> None:
		"""Add Replace, Replace All, and Cancel without a default button."""
		buttonHelper = guiHelper.ButtonHelper(wx.HORIZONTAL)
		replaceButton = buttonHelper.addButton(
			self,
			wx.ID_APPLY,
			# Translators: Button replacing one text match.
			_("Re&place"),
		)
		buttonHelper.addButton(
			self,
			wx.ID_OK,
			# Translators: Button replacing all text matches.
			_("Replace &All"),
		)
		buttonHelper.addButton(self, wx.ID_CANCEL)
		replaceButton.Bind(wx.EVT_BUTTON, self._onReplace)
		sHelper.addDialogDismissButtons(buttonHelper, separated=True)

	def getValues(self) -> tuple[str, str, bool, bool]:
		"""Return the search text, replacement, and matching options."""
		return (
			self.findCtrl.GetValue(),
			self.replaceCtrl.GetValue(),
			self.matchCaseCheckBox.GetValue(),
			self.regexCheckBox.GetValue(),
		)

	def _onReplace(self, event: wx.CommandEvent) -> None:
		"""Close the dialog with the single-replacement result."""
		self.EndModal(wx.ID_APPLY)


class _ManagerEditorCommands:
	"""Run local find, replace, and line navigation commands for one editor."""

	def __init__(
		self,
		parent: wx.Window,
		editor: wx.TextCtrl,
		showInfo: Callable[[str], None],
		showError: Callable[[Exception], None],
	) -> None:
		"""Bind the commands to an editor and the manager's message handlers."""
		self._parent = parent
		self._editor = editor
		self._showInfo = showInfo
		self._showError = showError
		self._findText = ""
		self._findMatchCase = False

	def showFind(self, *, backwards: bool = False) -> None:
		"""Prompt for literal search text and select the next match."""
		dialog = _FindDialog(
			self._parent,
			self._editor.GetStringSelection() or self._findText,
			self._findMatchCase,
		)
		dialog.backwardCheckBox.SetValue(backwards)
		try:
			if displayDialogAsModal(dialog) != wx.ID_OK:
				return
			findText, matchCase, backwards = dialog.getValues()
		finally:
			dialog.Destroy()
		if not findText:
			# Translators: Error shown when no find text was entered.
			self._showInfo(_("Enter text to find."))
			return
		self._findText = findText
		self._findMatchCase = matchCase
		self._selectNextMatch(backwards)

	def findNext(self, *, backwards: bool = False) -> None:
		"""Repeat the previous search, prompting when no search exists."""
		if not self._findText:
			if self._editor.GetLastPosition() == 0:
				return
			self.showFind(backwards=backwards)
			return
		self._selectNextMatch(backwards)

	def _selectNextMatch(self, backwards: bool) -> None:
		"""Select a literal occurrence of stored search text, wrapping once."""
		text = self._editor.GetValue()
		flags = 0 if self._findMatchCase else re.IGNORECASE
		pattern = re.compile(re.escape(self._findText), flags)
		offsetConverter = textUtils.WideStringOffsetConverter(text)
		selectionStart, selectionEnd = offsetConverter.encodedToStrOffsets(
			*self._editor.GetSelection(),
		)
		selection = self._editor.GetStringSelection()
		isCurrentMatch = bool(selection and pattern.fullmatch(selection))
		encodedInsertionPoint = self._editor.GetInsertionPoint()
		insertionPoint = offsetConverter.encodedToStrOffsets(
			encodedInsertionPoint,
			encodedInsertionPoint,
		)[0]
		start = (
			selectionStart
			if isCurrentMatch and backwards
			else selectionEnd
			if isCurrentMatch
			else insertionPoint
		)
		matchSpan: tuple[int, int] | None = None
		if backwards:
			matchSpan = findPreviousLiteralMatch(
				text,
				self._findText,
				start,
				matchCase=self._findMatchCase,
				currentMatch=isCurrentMatch,
			)
		else:
			match = pattern.search(text, start)
			if match is None and start > 0:
				match = pattern.search(text)
			if match is not None:
				matchSpan = match.span()
		if matchSpan is None:
			# Translators: Message shown when search text is absent.
			self._showInfo(_("Text not found."))
			return
		encodedStart, encodedEnd = offsetConverter.strToEncodedOffsets(*matchSpan)
		self._editor.SetSelection(encodedStart, encodedEnd)
		self._editor.ShowPosition(encodedStart)
		self._editor.SetFocus()

	def showReplace(self) -> None:
		"""Prompt for replacement options and apply one or all matches."""
		dialog = _ReplaceDialog(
			self._parent,
			self._editor.GetStringSelection() or self._findText,
			self._findMatchCase,
		)
		try:
			result = displayDialogAsModal(dialog)
			if result not in (wx.ID_APPLY, wx.ID_OK):
				return
			findText, replacement, matchCase, isRegex = dialog.getValues()
		finally:
			dialog.Destroy()
		if not findText:
			# Translators: Error shown when no replacement search text was entered.
			self._showInfo(_("Enter text to replace."))
			return
		try:
			flags = 0 if matchCase else re.IGNORECASE
			pattern = re.compile(findText if isRegex else re.escape(findText), flags)
			if result == wx.ID_APPLY:
				count = self._replaceOne(pattern, replacement, isRegex)
			else:
				newText, count = pattern.subn(
					replacement if isRegex else lambda _match: replacement,
					self._editor.GetValue(),
				)
				if count:
					self._editor.SetValue(newText)
		except (re.error, IndexError, ValueError, OverflowError, RecursionError) as error:
			self._showError(error)
			return
		if count == 0:
			# Translators: Message shown when replacement text is absent.
			self._showInfo(_("Text not found."))
			return
		if not isRegex:
			self._findText = findText
			self._findMatchCase = matchCase
		message = ngettext(
			# Translators: Message reporting one or multiple text replacements.
			"{count} replacement made.",
			"{count} replacements made.",
			count,
		).format(count=count)
		self._showInfo(message)

	def _replaceOne(
		self,
		pattern: re.Pattern[str],
		replacement: str,
		isRegex: bool,
	) -> int:
		"""Replace the selected or next matching text once."""
		text = self._editor.GetValue()
		offsetConverter = textUtils.WideStringOffsetConverter(text)
		selectionStart, selectionEnd = offsetConverter.encodedToStrOffsets(*self._editor.GetSelection())
		selectedMatch = (
			matchRegexSelection(pattern, text, selectionStart, selectionEnd)
			if selectionStart < selectionEnd
			else None
		)
		if selectedMatch is not None:
			match = selectedMatch
		else:
			encodedInsertionPoint = self._editor.GetInsertionPoint()
			insertionPoint = offsetConverter.encodedToStrOffsets(
				encodedInsertionPoint,
				encodedInsertionPoint,
			)[0]
			match = pattern.search(text, insertionPoint)
			if match is None:
				match = pattern.search(text)
			if match is None:
				return 0
		start, end = offsetConverter.strToEncodedOffsets(match.start(), match.end())
		replacementText = match.expand(replacement) if isRegex else replacement
		self._editor.Replace(start, end, replacementText)
		replacementEnd = self._editor.GetInsertionPoint()
		self._editor.SetSelection(start, replacementEnd)
		self._editor.ShowPosition(start)
		self._editor.SetFocus()
		return 1

	def goToLine(self) -> None:
		"""Prompt for a line number and move the editor insertion point."""
		position = self._editor.GetInsertionPoint()
		currentLine = self._editor.GetRange(0, position).count("\n") + 1
		totalLines = max(1, self._editor.GetNumberOfLines())
		dialog = wx.NumberEntryDialog(
			self._parent,
			# Translators: Prompt for a line number in clipboard content.
			_("Enter a line number:"),
			# Translators: Label for the line number field.
			_("&Line:"),
			# Translators: Title of the go-to-line dialog.
			_("Go to Line"),
			min(currentLine, totalLines),
			1,
			totalLines,
		)
		try:
			if displayDialogAsModal(dialog) != wx.ID_OK:
				return
			targetPosition = self._editor.XYToPosition(0, dialog.GetValue() - 1)
			if targetPosition == wx.NOT_FOUND:
				targetPosition = self._editor.GetLastPosition()
			self._editor.SetInsertionPoint(targetPosition)
			self._editor.ShowPosition(targetPosition)
			self._editor.SetFocus()
		finally:
			dialog.Destroy()
