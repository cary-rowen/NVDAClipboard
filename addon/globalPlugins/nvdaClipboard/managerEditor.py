# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Provide text editing commands for the clipboard manager content control."""

from __future__ import annotations

from collections.abc import Callable
import re

import addonHandler
from gui.message import displayDialogAsModal
import textUtils
import wx

from .search import findPreviousLiteralMatch


addonHandler.initTranslation()


class _FindDialog(wx.Dialog):
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
		)
		mainSizer = wx.BoxSizer(wx.VERTICAL)
		findLabel = wx.StaticText(
			self,
			# Translators: Label for the text to find in clipboard manager content.
			label=_("&Find:"),
		)
		self.findCtrl = wx.TextCtrl(self, value=findText)
		mainSizer.Add(findLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
		mainSizer.Add(self.findCtrl, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)
		self.matchCaseCheckBox = wx.CheckBox(
			self,
			# Translators: Checkbox enabling case-sensitive text search.
			label=_("Match &case"),
		)
		self.matchCaseCheckBox.SetValue(matchCase)
		self.backwardCheckBox = wx.CheckBox(
			self,
			# Translators: Checkbox selecting backward text search.
			label=_("Search &backward"),
		)
		mainSizer.Add(self.matchCaseCheckBox, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
		mainSizer.Add(self.backwardCheckBox, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
		buttonSizer = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
		if buttonSizer is not None:
			mainSizer.Add(buttonSizer, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)
		self.SetSizerAndFit(mainSizer)
		self.SetMinSize((400, self.GetBestSize().height))
		self.findCtrl.SetFocus()

	def getValues(self) -> tuple[str, bool, bool]:
		"""Return the entered text, case option, and search direction."""
		return (
			self.findCtrl.GetValue(),
			self.matchCaseCheckBox.GetValue(),
			self.backwardCheckBox.GetValue(),
		)


class _ReplaceDialog(wx.Dialog):
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
		)
		mainSizer = wx.BoxSizer(wx.VERTICAL)
		findLabel = wx.StaticText(
			self,
			# Translators: Label for the text to find in the replacement dialog.
			label=_("&Find:"),
		)
		self.findCtrl = wx.TextCtrl(self, value=findText)
		mainSizer.Add(findLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP, border=10)
		mainSizer.Add(self.findCtrl, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		replaceLabel = wx.StaticText(
			self,
			# Translators: Label for replacement text in the replacement dialog.
			label=_("&Replace with:"),
		)
		self.replaceCtrl = wx.TextCtrl(self)
		mainSizer.Add(replaceLabel, flag=wx.LEFT | wx.RIGHT, border=10)
		mainSizer.Add(self.replaceCtrl, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		self.matchCaseCheckBox = wx.CheckBox(
			self,
			# Translators: Checkbox enabling case-sensitive replacement matching.
			label=_("Match &case"),
		)
		self.matchCaseCheckBox.SetValue(matchCase)
		mainSizer.Add(self.matchCaseCheckBox, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)
		self.regexCheckBox = wx.CheckBox(
			self,
			# Translators: Checkbox enabling regular expressions in replacement.
			label=_("Use regular e&xpressions"),
		)
		mainSizer.Add(self.regexCheckBox, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM, border=10)

		buttonSizer = wx.StdDialogButtonSizer()
		replaceButton = wx.Button(
			self,
			wx.ID_APPLY,
			# Translators: Button replacing one text match.
			_("Re&place"),
		)
		replaceAllButton = wx.Button(
			self,
			wx.ID_OK,
			# Translators: Button replacing all text matches.
			_("Replace &All"),
		)
		cancelButton = wx.Button(self, wx.ID_CANCEL)
		buttonSizer.AddButton(replaceButton)
		buttonSizer.AddButton(replaceAllButton)
		buttonSizer.AddButton(cancelButton)
		buttonSizer.Realize()
		replaceButton.Bind(wx.EVT_BUTTON, self._onReplace)
		mainSizer.Add(buttonSizer, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)
		self.SetSizerAndFit(mainSizer)
		self.SetMinSize((400, self.GetBestSize().height))
		self.findCtrl.SetFocus()

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
				match = pattern.search(text, 0, start)
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
		except re.error as error:
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
		selectionStart, selectionEnd = self._editor.GetSelection()
		selection = self._editor.GetStringSelection()
		selectedMatch = pattern.fullmatch(selection) if selection else None
		if selectedMatch is not None:
			start = selectionStart
			end = selectionEnd
			match = selectedMatch
		else:
			encodedInsertionPoint = self._editor.GetInsertionPoint()
			insertionPoint = offsetConverter.encodedToStrOffsets(
				encodedInsertionPoint,
				encodedInsertionPoint,
			)[0]
			match = pattern.search(text, insertionPoint)
			if match is None:
				match = pattern.search(text, 0, insertionPoint)
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
