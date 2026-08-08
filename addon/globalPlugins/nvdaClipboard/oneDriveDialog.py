# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Provide the OneDrive account and synchronization dialog."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import addonHandler
from logHandler import log
import ui
import wx

if TYPE_CHECKING:
	from .oneDriveSync import OneDriveSyncManager


addonHandler.initTranslation()


class _FocusableReadOnlyTextCtrl(wx.TextCtrl):
	"""Keep a single-line read-only field in keyboard traversal."""

	def AcceptsFocusFromKeyboard(self) -> bool:
		"""Allow Tab to focus selectable read-only content."""
		return True


class OneDriveSyncDialog(wx.Dialog):
	"""Show OneDrive account state and synchronization actions."""

	def __init__(
		self,
		parent: wx.Window,
		manager: OneDriveSyncManager,
		onDestroy: Callable[[OneDriveSyncDialog], None],
	) -> None:
		"""Create a single non-modal OneDrive synchronization dialog."""
		super().__init__(
			parent,
			# Translators: Title of the OneDrive account dialog.
			title=_("OneDrive"),
			style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
		)
		self._manager = manager
		self._onDestroy = onDestroy
		self._isDestroyed = False
		self._deviceCodeActive = False
		self._verificationUrl = ""
		self._makeUi()
		self.refreshFromManager()
		self.Bind(wx.EVT_CLOSE, self._onClose)
		self.Bind(wx.EVT_WINDOW_DESTROY, self._onWindowDestroy)
		self.Fit()
		self.SetMinSize((620, self.GetBestSize().height))
		self.CentreOnParent()

	def _makeUi(self) -> None:
		"""Create focusable status fields and synchronization controls."""
		mainSizer = wx.BoxSizer(wx.VERTICAL)
		mainSizer.AddSpacer(10)
		self.accountCtrl = self._addReadOnlyRow(
			mainSizer,
			# Translators: Label for the Microsoft account used by OneDrive.
			_("&Account:"),
		)
		self.statusCtrl = self._addReadOnlyText(
			mainSizer,
			# Translators: Label for the current OneDrive status.
			_("&Status:"),
			size=(-1, 58),
		)
		self.privacyCtrl = self._addReadOnlyText(
			mainSizer,
			# Translators: Label for a short description of OneDrive synchronization.
			_("A&bout:"),
			value=_(
				# Translators: Short description of OneDrive backup and synchronization.
				"Backs up and syncs eligible history and categories. Deletions are also synchronized.",
			),
			size=(-1, 58),
		)

		self.loginPanel = wx.Panel(self)
		loginSizer = wx.BoxSizer(wx.HORIZONTAL)
		self.browserLoginButton = wx.Button(
			self.loginPanel,
			# Translators: Button to sign in to OneDrive with the system browser.
			label=_("Sign &in"),
		)
		self.deviceCodeButton = wx.Button(
			self.loginPanel,
			# Translators: Button to use Microsoft's device-code sign-in fallback.
			label=_("&Device code sign-in"),
		)
		loginSizer.Add(self.browserLoginButton, flag=wx.RIGHT, border=8)
		loginSizer.Add(self.deviceCodeButton)
		self.loginPanel.SetSizer(loginSizer)
		mainSizer.Add(self.loginPanel, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		self.devicePanel = wx.Panel(self)
		deviceSizer = wx.BoxSizer(wx.VERTICAL)
		self.deviceCodeCtrl = self._addReadOnlyRow(
			deviceSizer,
			# Translators: Label for the selectable Microsoft device sign-in code.
			_("Cod&e:"),
			parent=self.devicePanel,
		)
		self.verificationUrlCtrl = self._addReadOnlyRow(
			deviceSizer,
			# Translators: Label for the selectable Microsoft device sign-in address.
			_("Add&ress:"),
			parent=self.devicePanel,
		)
		deviceActionSizer = wx.BoxSizer(wx.HORIZONTAL)
		self.openUrlButton = wx.Button(
			self.devicePanel,
			# Translators: Button to open the Microsoft device sign-in page.
			label=_("&Open page"),
		)
		self.cancelDeviceCodeButton = wx.Button(
			self.devicePanel,
			# Translators: Button to cancel Microsoft device-code sign-in.
			label=_("Ca&ncel sign-in"),
		)
		deviceActionSizer.Add(self.openUrlButton, flag=wx.RIGHT, border=8)
		deviceActionSizer.Add(self.cancelDeviceCodeButton)
		deviceSizer.Add(deviceActionSizer, flag=wx.TOP, border=2)
		self.devicePanel.SetSizer(deviceSizer)
		mainSizer.Add(self.devicePanel, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		self.loggedInPanel = wx.Panel(self)
		loggedInSizer = wx.BoxSizer(wx.HORIZONTAL)
		self.signOutButton = wx.Button(
			self.loggedInPanel,
			# Translators: Button to sign out of OneDrive synchronization.
			label=_("Sign &out"),
		)
		loggedInSizer.Add(self.signOutButton)
		self.loggedInPanel.SetSizer(loggedInSizer)
		mainSizer.Add(self.loggedInPanel, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		mainSizer.Add(wx.StaticLine(self), flag=wx.LEFT | wx.RIGHT | wx.EXPAND, border=10)
		closeSizer = wx.BoxSizer(wx.HORIZONTAL)
		self.closeButton = wx.Button(
			self,
			wx.ID_CLOSE,
			# Translators: Button to close the OneDrive account dialog.
			label=_("&Close"),
		)
		self.SetEscapeId(wx.ID_CLOSE)
		closeSizer.AddStretchSpacer()
		closeSizer.Add(self.closeButton)
		mainSizer.Add(closeSizer, flag=wx.ALL | wx.EXPAND, border=10)
		self.SetSizer(mainSizer)

		self.browserLoginButton.Bind(wx.EVT_BUTTON, self._onBrowserLogin)
		self.deviceCodeButton.Bind(wx.EVT_BUTTON, self._onDeviceCodeLogin)
		self.openUrlButton.Bind(wx.EVT_BUTTON, self._onOpenUrl)
		self.cancelDeviceCodeButton.Bind(wx.EVT_BUTTON, self._onCancelDeviceCode)
		self.signOutButton.Bind(wx.EVT_BUTTON, self._onSignOut)
		self.closeButton.Bind(wx.EVT_BUTTON, self._onClose)

	def _addReadOnlyRow(
		self,
		parentSizer: wx.BoxSizer,
		label: str,
		*,
		parent: wx.Window | None = None,
	) -> wx.TextCtrl:
		"""Add a labelled, focusable, single-line read-only field."""
		controlParent = parent or self
		rowSizer = wx.BoxSizer(wx.HORIZONTAL)
		labelCtrl = wx.StaticText(controlParent, label=label)
		textCtrl = _FocusableReadOnlyTextCtrl(controlParent, style=wx.TE_READONLY)
		textCtrl.SetName(label.replace("&", "").rstrip(":"))
		rowSizer.Add(labelCtrl, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=8)
		rowSizer.Add(textCtrl, proportion=1, flag=wx.EXPAND)
		parentSizer.Add(rowSizer, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)
		return textCtrl

	def _addReadOnlyText(
		self,
		parentSizer: wx.BoxSizer,
		label: str,
		*,
		value: str = "",
		size: tuple[int, int] = (-1, -1),
	) -> wx.TextCtrl:
		"""Add a labelled, focusable, multiline read-only field."""
		labelCtrl = wx.StaticText(self, label=label)
		textCtrl = wx.TextCtrl(
			self,
			value=value,
			size=size,
			style=wx.TE_MULTILINE | wx.TE_READONLY,
		)
		textCtrl.SetName(label.replace("&", "").rstrip(":"))
		parentSizer.Add(labelCtrl, flag=wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, border=10)
		parentSizer.Add(textCtrl, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)
		return textCtrl

	def refreshFromManager(self) -> None:
		"""Refresh account, status, visibility, and enabled states."""
		if self._isDestroyed:
			return
		focus = wx.Window.FindFocus()
		previousPanels = (
			self.loginPanel.IsShown(),
			self.devicePanel.IsShown(),
			self.loggedInPanel.IsShown(),
		)
		state = self._manager.getState()
		if state.isLoggedIn:
			# Translators: Fallback account label when Microsoft did not provide an account name.
			accountName = state.accountName or _("Microsoft account")
		else:
			# Translators: OneDrive account status when no Microsoft account is signed in.
			accountName = _("Not signed in")
		self.accountCtrl.ChangeValue(accountName)
		self.statusCtrl.ChangeValue(state.statusMessage)

		if state.isLoggedIn and self._deviceCodeActive:
			self._clearDeviceCode()
		canStartLogin = state.isAvailable and not state.isLoggedIn and not state.isBusy
		self.loginPanel.Show(not state.isLoggedIn)
		self.loggedInPanel.Show(state.isLoggedIn)
		self.devicePanel.Show(self._deviceCodeActive)
		self.browserLoginButton.Enable(canStartLogin and not self._deviceCodeActive)
		self.deviceCodeButton.Enable(canStartLogin and not self._deviceCodeActive)
		self.openUrlButton.Enable(self._deviceCodeActive and bool(self._verificationUrl))
		self.cancelDeviceCodeButton.Enable(self._deviceCodeActive)
		self.signOutButton.Enable(state.isAvailable and state.isLoggedIn and not state.isSigningOut)
		self.Layout()
		currentPanels = (
			self.loginPanel.IsShown(),
			self.devicePanel.IsShown(),
			self.loggedInPanel.IsShown(),
		)
		if currentPanels != previousPanels:
			bestSize = self.GetBestSize()
			currentSize = self.GetSize()
			self.SetSize((max(currentSize.width, bestSize.width), max(currentSize.height, bestSize.height)))
		if (
			focus is not None
			and wx.GetTopLevelParent(focus) is self
			and (not focus.IsShownOnScreen() or not focus.IsEnabled())
		):
			if self.cancelDeviceCodeButton.IsShownOnScreen() and self.cancelDeviceCodeButton.IsEnabled():
				self.cancelDeviceCodeButton.SetFocus()
			elif self.signOutButton.IsShownOnScreen() and self.signOutButton.IsEnabled():
				self.signOutButton.SetFocus()
			elif self.browserLoginButton.IsShownOnScreen() and self.browserLoginButton.IsEnabled():
				self.browserLoginButton.SetFocus()
			else:
				self.closeButton.SetFocus()

	def _clearDeviceCode(self) -> None:
		"""Clear the current device-code presentation without cancelling service work."""
		self._deviceCodeActive = False
		self._verificationUrl = ""
		self.deviceCodeCtrl.ChangeValue("")
		self.verificationUrlCtrl.ChangeValue("")

	def _operationDone(self, success: bool, message: str) -> None:
		"""Refresh after an explicit operation and announce its result."""
		if self._isDestroyed:
			return
		self.refreshFromManager()
		if message:
			ui.message(message)

	def _deviceOperationDone(self, success: bool, message: str) -> None:
		"""Finish device-code sign-in and remove the temporary code fields."""
		if self._isDestroyed:
			return
		self._clearDeviceCode()
		self._operationDone(success, message)

	def _showDeviceCode(self, userCode: str, verificationUrl: str) -> None:
		"""Show and focus the selectable device code supplied by Microsoft."""
		if self._isDestroyed:
			return
		self._deviceCodeActive = True
		self._verificationUrl = verificationUrl
		self.deviceCodeCtrl.ChangeValue(userCode)
		self.verificationUrlCtrl.ChangeValue(verificationUrl)
		self.refreshFromManager()
		self.deviceCodeCtrl.SetFocus()
		self.deviceCodeCtrl.SelectAll()

	def _onBrowserLogin(self, event: wx.CommandEvent) -> None:
		"""Start Microsoft interactive sign-in in the system browser."""
		self._manager.loginInteractive(int(self.GetHandle()), onDone=self._operationDone)

	def _onDeviceCodeLogin(self, event: wx.CommandEvent) -> None:
		"""Start the fallback Microsoft device-code sign-in flow."""
		self._deviceCodeActive = True
		self._manager.startDeviceCode(self._showDeviceCode, onDone=self._deviceOperationDone)
		self.refreshFromManager()

	def _onOpenUrl(self, event: wx.CommandEvent) -> None:
		"""Open the current Microsoft device sign-in address."""
		if not self._verificationUrl:
			return
		try:
			opened = wx.LaunchDefaultBrowser(self._verificationUrl)
		except Exception:
			opened = False
			log.debugWarning("Could not open the OneDrive device sign-in page.", exc_info=True)
		if opened:
			return
		# Translators: Error shown when the Microsoft device sign-in page cannot be opened.
		message = _("Could not open the Microsoft sign-in page")
		self.statusCtrl.ChangeValue(message)
		ui.message(message)

	def _onCancelDeviceCode(self, event: wx.CommandEvent) -> None:
		"""Cancel the active Microsoft device-code sign-in flow."""
		self._manager.cancelDeviceCode()
		self._clearDeviceCode()
		self.refreshFromManager()

	def _onSignOut(self, event: wx.CommandEvent) -> None:
		"""Sign out of OneDrive synchronization."""
		self._manager.signOut(onDone=self._operationDone)

	def _onClose(self, event: wx.Event) -> None:
		"""Cancel device sign-in and destroy the non-modal dialog."""
		if self._isDestroyed:
			return
		self._isDestroyed = True
		if self._deviceCodeActive:
			self._manager.cancelDeviceCode()
		self._onDestroy(self)
		self.Destroy()

	def _onWindowDestroy(self, event: wx.WindowDestroyEvent) -> None:
		"""Clear controller ownership when the dialog is destroyed externally."""
		if event.GetEventObject() is self and not self._isDestroyed:
			self._isDestroyed = True
			if self._deviceCodeActive:
				self._manager.cancelDeviceCode()
			self._onDestroy(self)
		event.Skip()
