# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Provide the Tiantan Cloud Clipboard account dialog."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import addonHandler
import wx

if TYPE_CHECKING:
	from .cloudSync import CloudSyncManager


addonHandler.initTranslation()


class CloudClipboardDialog(wx.Dialog):
	"""Show Tiantan Cloud Clipboard account and synchronization state."""

	def __init__(
		self,
		parent: wx.Window,
		manager: CloudSyncManager,
		onDestroy: Callable[[], None],
	) -> None:
		super().__init__(
			parent,
			# Translators: Title of the Tiantan Cloud Clipboard account dialog.
			title=_("Tiantan Cloud Clipboard"),
			style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
		)
		self._manager = manager
		self._onDestroy = onDestroy
		self._isDestroyed = False
		self._busy = False
		self._makeUi()
		self._refreshUi()
		self.Bind(wx.EVT_CLOSE, self._onClose)
		self.Bind(wx.EVT_WINDOW_DESTROY, self._onWindowDestroy)
		self.SetMinSize(self.GetBestSize())
		self.SetEscapeId(wx.ID_CLOSE)
		self.CentreOnParent()

	def _makeUi(self) -> None:
		self._mainSizer = wx.BoxSizer(wx.VERTICAL)
		statusLabel = wx.StaticText(
			self,
			# Translators: Label for the current Tiantan Cloud Clipboard status.
			label=_("Status:"),
		)
		self._mainSizer.Add(statusLabel, flag=wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, border=10)
		self.statusText = wx.StaticText(self, label="")
		self._mainSizer.Add(self.statusText, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)
		self.privacyText = wx.StaticText(
			self,
			# Translators: Description shown in the Tiantan Cloud Clipboard dialog.
			label=_("Synchronizes clipboard text between this computer and Tiantan Cloud Clipboard."),
		)
		self._mainSizer.Add(self.privacyText, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		self.loginPanel = wx.Panel(self)
		loginSizer = wx.BoxSizer(wx.VERTICAL)
		self.phoneCtrl = self._addTextRow(
			loginSizer,
			# Translators: Label for the Tiantan Cloud Clipboard phone number field.
			_("&Phone number:"),
			style=0,
		)
		self.passwordCtrl = self._addTextRow(
			loginSizer,
			# Translators: Label for the Tiantan Cloud Clipboard password field.
			_("Pass&word:"),
			style=wx.TE_PASSWORD,
		)
		self.loginButton = wx.Button(
			self.loginPanel,
			# Translators: Button to log in to Tiantan Cloud Clipboard.
			label=_("Sign &in"),
		)
		loginSizer.Add(self.loginButton, flag=wx.TOP | wx.ALIGN_RIGHT, border=10)
		self.loginPanel.SetSizer(loginSizer)
		self._mainSizer.Add(self.loginPanel, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		self.loggedInPanel = wx.Panel(self)
		loggedInSizer = wx.BoxSizer(wx.VERTICAL)
		accountLabel = wx.StaticText(
			self.loggedInPanel,
			# Translators: Label for the signed-in Tiantan Cloud Clipboard account.
			label=_("Account:"),
		)
		loggedInSizer.Add(accountLabel, flag=wx.BOTTOM | wx.EXPAND, border=2)
		self.loginStatusText = wx.StaticText(self.loggedInPanel, label="")
		loggedInSizer.Add(self.loginStatusText, flag=wx.BOTTOM | wx.EXPAND, border=10)
		self.logoutButton = wx.Button(
			self.loggedInPanel,
			# Translators: Button to log out of Tiantan Cloud Clipboard.
			label=_("Sign &out"),
		)
		loggedInSizer.Add(self.logoutButton)
		self.loggedInPanel.SetSizer(loggedInSizer)
		self._mainSizer.Add(self.loggedInPanel, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		self._mainSizer.Add(wx.StaticLine(self), flag=wx.LEFT | wx.RIGHT | wx.EXPAND, border=10)
		closeSizer = wx.BoxSizer(wx.HORIZONTAL)
		self.closeButton = wx.Button(
			self,
			wx.ID_CLOSE,
			# Translators: Button to close the Tiantan Cloud Clipboard account dialog.
			label=_("&Close"),
		)
		closeSizer.AddStretchSpacer()
		closeSizer.Add(self.closeButton)
		self._mainSizer.Add(closeSizer, flag=wx.ALL | wx.EXPAND, border=10)
		self.SetSizer(self._mainSizer)

		self.loginButton.Bind(wx.EVT_BUTTON, self._onLogin)
		self.logoutButton.Bind(wx.EVT_BUTTON, self._onLogout)
		self.closeButton.Bind(wx.EVT_BUTTON, self._onClose)

	def _addTextRow(self, parentSizer: wx.BoxSizer, label: str, style: int) -> wx.TextCtrl:
		rowSizer = wx.BoxSizer(wx.HORIZONTAL)
		labelCtrl = wx.StaticText(self.loginPanel, label=label)
		textCtrl = wx.TextCtrl(self.loginPanel, style=style)
		rowSizer.Add(labelCtrl, flag=wx.ALIGN_CENTER_VERTICAL)
		rowSizer.AddSpacer(8)
		rowSizer.Add(textCtrl, proportion=1, flag=wx.EXPAND)
		parentSizer.Add(rowSizer, flag=wx.BOTTOM | wx.EXPAND, border=8)
		return textCtrl

	def _refreshUi(self, updateStatus: bool = True) -> None:
		state = self._manager.getState()
		if updateStatus:
			self.statusText.SetLabel(state.statusMessage)
		self.loginStatusText.SetLabel(self._manager.getLoginStatusText())
		self.loginPanel.Show(not state.isLoggedIn)
		self.loggedInPanel.Show(state.isLoggedIn)

		canUse = not self._busy and state.isAvailable
		self.phoneCtrl.Enable(canUse)
		self.passwordCtrl.Enable(canUse)
		self.loginButton.Enable(canUse)
		self.logoutButton.Enable(canUse and state.isLoggedIn)
		self.closeButton.Enable(not self._busy)

		self.Layout()
		self.Fit()

	def refreshFromManager(self, updateStatus: bool = True) -> None:
		"""Refresh controls after cloud state changes outside this dialog."""
		if not self._isDestroyed:
			self._refreshUi(updateStatus=updateStatus)

	def _setBusy(self, busy: bool, message: str | None = None) -> None:
		self._busy = busy
		if message:
			self.statusText.SetLabel(message)
		self._refreshUi(updateStatus=message is None)

	def _operationDone(self, success: bool, message: str) -> None:
		if self._isDestroyed:
			return
		self._setBusy(False, message if message else None)

	def _loginDone(self, success: bool, message: str) -> None:
		self._operationDone(success, message)
		if success:
			self.passwordCtrl.SetValue("")

	def _logoutDone(self, success: bool, message: str) -> None:
		self._operationDone(success, message)
		if success:
			self.phoneCtrl.SetFocus()

	def _onLogin(self, evt: wx.CommandEvent) -> None:
		# Translators: Progress message while signing in to Tiantan Cloud Clipboard.
		self._setBusy(True, _("Signing in..."))
		self._manager.login(
			self.phoneCtrl.GetValue(),
			self.passwordCtrl.GetValue(),
			onDone=self._loginDone,
		)

	def _onLogout(self, evt: wx.CommandEvent) -> None:
		# Translators: Progress message while signing out of Tiantan Cloud Clipboard.
		self._setBusy(True, _("Signing out..."))
		self._manager.logout(onDone=self._logoutDone)

	def _onClose(self, evt: wx.Event) -> None:
		if not self._isDestroyed:
			self._isDestroyed = True
			self._onDestroy()
		self.Destroy()

	def _onWindowDestroy(self, evt: wx.WindowDestroyEvent) -> None:
		if evt.GetEventObject() is self and not self._isDestroyed:
			self._isDestroyed = True
			self._onDestroy()
		evt.Skip()
