# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Coordinate ClipDataCloud operations with local clipboard changes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock, Thread

import addonHandler
import config
from logHandler import log
import ui
import wx

from .cloudClipboard import (
	BUFFER_TOO_SMALL,
	INVALID_ARGUMENT,
	TEXT_CONTAINS_NUL_ERROR,
	TEXT_EMPTY_ERROR,
	TEXT_EXCEEDS_MAX_BYTES_ERROR,
	TEXT_INVALID_UTF8_ERROR,
	USER_NOT_LOGGED_IN,
	CloudClipboardError,
	CloudClipboardSdk,
	CloudLoginResult,
	validateUploadText,
)


addonHandler.initTranslation()

CompletionCallback = Callable[[bool, str], None]
ClipboardWriter = Callable[[str], None]
StateChangedCallback = Callable[[], None]

_CONFIG_SECTION = "nvdaClipboard"
_CONFIG_AUTO_SYNC_TIANTAN = "autoSyncTiantan"

config.conf.spec.setdefault(_CONFIG_SECTION, {})[_CONFIG_AUTO_SYNC_TIANTAN] = "boolean(default=true)"


def _getSdkUnavailableMessage() -> str:
	# Translators: Error shown when the native cloud clipboard library cannot be used.
	return _("Tiantan Cloud Clipboard is unavailable. Check the ClipDataCloud SDK.")


class CloudClipboardWriteError(Exception):
	"""Raised when fetched cloud text cannot be written to the local clipboard."""


@dataclass(frozen=True)
class CloudSyncState:
	"""Describe the current cloud clipboard state for user interfaces."""

	isAvailable: bool
	isLoggedIn: bool
	isAutoSyncEnabled: bool
	isManualSendInProgress: bool
	statusMessage: str


class CloudSyncManager:
	"""Run cloud SDK calls in worker threads and track automatic upload state."""

	def __init__(self, onStateChanged: StateChangedCallback | None = None) -> None:
		self._onStateChanged = onStateChanged
		self._sdk: CloudClipboardSdk | None = None
		self._loadError: str | None = None
		try:
			self._sdk = CloudClipboardSdk()
		except CloudClipboardError as error:
			self._loadError = error.message
			self._statusMessage = _getSdkUnavailableMessage()
			log.debugWarning("ClipDataCloud SDK is unavailable.", exc_info=error)
		else:
			# Translators: Initial cloud clipboard status before the persisted login is checked.
			self._statusMessage = _("Checking...")
		self._isLoggedIn = False
		self._nickname: str | None = None
		self._isVip: bool | None = None
		self._autoUploadEnabled = False
		self._terminated = False
		self._stateOperationId = 0
		self._stateOperationInProgress = False
		self._sdkOperationLock = Lock()
		self._manualSendLock = Lock()
		self._uploadLock = Lock()
		self._uploadInProgress = False
		self._pendingUploadText: str | None = None
		self._autoUploadFailureReported = False
		self._notLoggedInNotified = False
		config.post_configProfileSwitch.register(self._onConfigProfileSwitch)

	def initialize(self) -> None:
		"""Check the persisted login state without blocking NVDA's main thread."""
		if self._loadError is None:
			self.checkStatus(announce=False)

	def terminate(self) -> None:
		"""Prevent future callbacks and automatic uploads."""
		config.post_configProfileSwitch.unregister(self._onConfigProfileSwitch)
		self._terminated = True
		self._autoUploadEnabled = False
		self._onStateChanged = None
		with self._uploadLock:
			self._pendingUploadText = None

	def getState(self) -> CloudSyncState:
		"""Return a snapshot of the current cloud state."""
		return CloudSyncState(
			isAvailable=self._loadError is None,
			isLoggedIn=self._isLoggedIn,
			isAutoSyncEnabled=self.isAutoSyncEnabled(),
			isManualSendInProgress=self._manualSendLock.locked(),
			statusMessage=self._statusMessage,
		)

	def isAutoSyncEnabled(self) -> bool:
		"""Return whether automatic Tiantan Cloud Clipboard synchronization is configured."""
		return bool(config.conf[_CONFIG_SECTION][_CONFIG_AUTO_SYNC_TIANTAN])

	def setAutoSyncEnabled(self, enabled: bool) -> None:
		"""Persist and immediately apply automatic Tiantan Cloud Clipboard synchronization."""
		config.conf[_CONFIG_SECTION][_CONFIG_AUTO_SYNC_TIANTAN] = enabled
		self._applyAutoSyncSetting()

	def getLoginStatusText(self) -> str:
		"""Return the account status shown in the cloud dialog."""
		if self._isLoggedIn:
			if self._nickname:
				if self._isVip is None:
					return self._nickname
				# Translators: Membership label for a paid cloud clipboard account.
				memberStatus = _("VIP account") if self._isVip else _("standard account")
				return _("{nickname}, {memberStatus}").format(
					nickname=self._nickname,
					memberStatus=memberStatus,
				)
			# Translators: Tiantan Cloud Clipboard account status when signed in without profile details.
			return _("Signed in")
		# Translators: Tiantan Cloud Clipboard account status when no account is signed in.
		return _("Not signed in")

	def login(self, phone: str, password: str, onDone: CompletionCallback | None = None) -> None:
		"""Sign in and apply the configured automatic upload state."""
		if self._rejectConcurrentStateOperation(onDone):
			return
		phone = phone.strip()
		if not phone or not password:
			# Translators: Validation message in the cloud clipboard login dialog.
			self._finish(False, _("Enter a phone number and password"), onDone)
			return

		def work() -> CloudLoginResult:
			return self._getSdk().login(phone, password)

		def success(result: object) -> str:
			assert isinstance(result, CloudLoginResult)
			self._setLoggedIn(result.nickname, result.isVip)
			return self._statusMessage

		operationId = self._beginStateOperation()
		# Translators: Error prefix for a failed cloud clipboard login.
		failureTemplate = _("Sign in failed: {error}")
		self._runSdkOperation(
			work,
			success,
			failureTemplate,
			onDone,
			stateOperationId=operationId,
		)

	def logout(self, onDone: CompletionCallback | None = None) -> None:
		"""Sign out and disable automatic upload."""
		if self._rejectConcurrentStateOperation(onDone):
			return
		operationId = self._beginStateOperation()

		def work() -> None:
			self._getSdk().logout()

		def success(_result: object) -> str:
			# Translators: Status shown after signing out of cloud clipboard.
			self._setLoggedOut(_("Signed out"))
			return self._statusMessage

		# Translators: Error prefix for a failed cloud clipboard logout.
		failureTemplate = _("Sign out failed: {error}")
		self._runSdkOperation(
			work,
			success,
			failureTemplate,
			onDone,
			stateOperationId=operationId,
		)

	def checkStatus(self, announce: bool = True, onDone: CompletionCallback | None = None) -> None:
		"""Check the SDK login state and update automatic upload."""
		if self._rejectConcurrentStateOperation(onDone, announce):
			return
		operationId = self._beginStateOperation()

		def work() -> bool:
			return self._getSdk().isUserLoggedIn()

		def success(result: object) -> str:
			isLoggedIn = bool(result)
			if isLoggedIn:
				self._setLoggedIn(self._nickname, self._isVip)
			else:
				# Translators: Tiantan Cloud Clipboard status when no account is signed in.
				self._setLoggedOut(_("Not signed in"))
			return self._statusMessage

		# Translators: Error prefix when checking cloud clipboard account status fails.
		failureTemplate = _("Status check failed: {error}")
		self._runSdkOperation(
			work,
			success,
			failureTemplate,
			onDone,
			announce=announce,
			stateOperationId=operationId,
		)

	def uploadCurrentText(
		self,
		text: str,
		onDone: CompletionCallback | None = None,
		announce: bool = True,
	) -> None:
		"""Upload clipboard text as an explicit user action."""
		if self._rejectConcurrentStateOperation(onDone, announce):
			return
		invalidReason = self._getInvalidUploadReason(text)
		if invalidReason is not None:
			self._finish(False, invalidReason, onDone, announce=announce)
			return
		if not self._manualSendLock.acquire(blocking=False):
			# Translators: Message shown when a manual cloud clipboard send is still running.
			self._finish(False, _("Tiantan Cloud Clipboard send is in progress"), onDone, announce)
			return
		self._notifyStateChanged()

		def finish(success: bool, message: str) -> None:
			"""Release the manual-send guard and forward completion."""
			self._manualSendLock.release()
			if not self._stateOperationInProgress:
				self._notifyStateChanged()
			if onDone is not None:
				onDone(success, message)

		def abandon() -> None:
			"""Release the manual-send guard when wx cannot accept its completion."""
			self._manualSendLock.release()

		def work() -> None:
			self._getSdk().uploadText(text)

		def success(_result: object) -> str:
			# Translators: Confirmation after manually sending clipboard text.
			return _("Sent to Tiantan Cloud Clipboard")

		self._runSdkOperation(
			work,
			success,
			# Translators: Error prefix for a failed Tiantan cloud clipboard send.
			_("Send failed: {error}"),
			finish,
			announce=announce,
			onDispatchFailed=abandon,
		)

	def fetchToClipboard(
		self,
		clipboardWriter: ClipboardWriter,
		onDone: CompletionCallback | None = None,
		onDispatchFailed: Callable[[], None] | None = None,
	) -> None:
		"""Fetch cloud text and pass it to a main-thread clipboard writer."""
		if self._rejectConcurrentStateOperation(onDone, announce=False):
			return
		stateAtStart = self._stateOperationId
		# Translators: Error prefix for a failed Tiantan cloud clipboard receive.
		failureTemplate = _("Receive failed: {error}")

		def worker() -> None:
			with self._sdkOperationLock:
				if self._terminated or stateAtStart != self._stateOperationId:
					if not self._queueMainThread(self._finishStaleOperation, onDone):
						self._abandonSdkOperation(None, onDispatchFailed)
					return
				try:
					text = self._getSdk().fetchText()
				except Exception as error:
					if not self._queueMainThread(
						self._finishFailedOperation,
						error,
						failureTemplate,
						onDone,
						False,
						stateAtStart,
						None,
					):
						self._abandonSdkOperation(None, onDispatchFailed)
					return
			if not self._queueMainThread(
				self._finishFetch,
				text,
				clipboardWriter,
				onDone,
				stateAtStart,
			):
				self._abandonSdkOperation(None, onDispatchFailed)

		thread = Thread(target=worker, name="nvdaClipboard.cloudFetch", daemon=True)
		try:
			thread.start()
		except RuntimeError as error:
			self._finishFailedOperation(
				error,
				failureTemplate,
				onDone,
				False,
				stateAtStart,
				None,
			)

	def onClipboardTextChanged(self, text: str) -> None:
		"""Queue a new local clipboard value for automatic upload."""
		if not self._autoUploadEnabled or self._terminated or self._stateOperationInProgress:
			return
		try:
			validateUploadText(text)
		except CloudClipboardError as error:
			if error.message == TEXT_EXCEEDS_MAX_BYTES_ERROR:
				ui.message(self._formatUploadValidationError(error))
			return
		with self._uploadLock:
			if not self._autoUploadEnabled or self._terminated or self._stateOperationInProgress:
				return
			stateAtStart = self._stateOperationId
			if self._uploadInProgress:
				self._pendingUploadText = text
				return
			self._uploadInProgress = True
		thread = Thread(
			target=self._autoUploadWorker,
			args=(text, stateAtStart),
			name="nvdaClipboard.cloudAutoUpload",
			daemon=True,
		)
		try:
			thread.start()
		except RuntimeError:
			with self._uploadLock:
				self._uploadInProgress = False
				self._pendingUploadText = None
			if not self._autoUploadFailureReported:
				self._autoUploadFailureReported = True
				log.exception("Could not start automatic ClipDataCloud upload.")

	def _getSdk(self) -> CloudClipboardSdk:
		if self._sdk is not None:
			return self._sdk
		raise CloudClipboardError(None, self._loadError or _getSdkUnavailableMessage())

	def _beginStateOperation(self) -> int:
		"""Return a new identifier that invalidates older login-state results."""
		self._stateOperationId += 1
		self._stateOperationInProgress = True
		with self._uploadLock:
			self._pendingUploadText = None
		return self._stateOperationId

	def _rejectConcurrentStateOperation(
		self,
		onDone: CompletionCallback | None,
		announce: bool = True,
	) -> bool:
		"""Reject work that could race with a login-state operation."""
		if not self._stateOperationInProgress:
			return False
		# Translators: Message shown when another cloud account action is still running.
		self._finish(
			False,
			_("Another Tiantan Cloud Clipboard account action is in progress"),
			onDone,
			announce,
		)
		return True

	def _runSdkOperation(
		self,
		work: Callable[[], object],
		success: Callable[[object], str],
		failureTemplate: str,
		onDone: CompletionCallback | None,
		announce: bool = True,
		stateOperationId: int | None = None,
		onDispatchFailed: Callable[[], None] | None = None,
	) -> None:
		"""Run one native operation and finish it on NVDA's main thread."""
		stateAtStart = self._stateOperationId
		operationId = stateOperationId if stateOperationId is not None else stateAtStart

		def worker() -> None:
			with self._sdkOperationLock:
				if self._terminated or operationId != self._stateOperationId:
					if not self._queueMainThread(self._finishStaleOperation, onDone):
						self._abandonSdkOperation(stateOperationId, onDispatchFailed)
					return
				try:
					result = work()
				except Exception as error:
					if not self._queueMainThread(
						self._finishFailedOperation,
						error,
						failureTemplate,
						onDone,
						announce,
						stateAtStart,
						stateOperationId,
					):
						self._abandonSdkOperation(stateOperationId, onDispatchFailed)
					return
			if not self._queueMainThread(
				self._finishSuccessfulOperation,
				result,
				success,
				onDone,
				announce,
				stateAtStart,
				stateOperationId,
			):
				self._abandonSdkOperation(stateOperationId, onDispatchFailed)

		thread = Thread(target=worker, name="nvdaClipboard.cloudOperation", daemon=True)
		try:
			thread.start()
		except RuntimeError as error:
			self._finishFailedOperation(
				error,
				failureTemplate,
				onDone,
				announce,
				stateAtStart,
				stateOperationId,
			)

	def _queueMainThread(self, callback: Callable[..., None], *args: object) -> bool:
		"""Queue a GUI-thread callback and report only a live-manager scheduling failure."""
		try:
			wx.CallAfter(callback, *args)
		except RuntimeError:
			if not self._terminated:
				log.debugWarning("Could not schedule a ClipDataCloud result callback.", exc_info=True)
			return False
		return True

	def _abandonSdkOperation(
		self,
		stateOperationId: int | None,
		onDispatchFailed: Callable[[], None] | None,
	) -> None:
		"""Release operation state after wx rejects a worker result."""
		if stateOperationId is not None and stateOperationId == self._stateOperationId:
			self._stateOperationInProgress = False
		if onDispatchFailed is not None:
			try:
				onDispatchFailed()
			except Exception:
				log.exception("ClipDataCloud dispatch-failure cleanup failed.")

	def _finishSuccessfulOperation(
		self,
		result: object,
		success: Callable[[object], str],
		onDone: CompletionCallback | None,
		announce: bool,
		stateAtStart: int,
		stateOperationId: int | None,
	) -> None:
		"""Apply a successful SDK result on the main thread."""
		if self._terminated:
			return
		operationId = stateOperationId if stateOperationId is not None else stateAtStart
		if operationId != self._stateOperationId:
			self._finishStaleOperation(onDone)
			return
		if stateOperationId is not None:
			self._stateOperationInProgress = False
		try:
			message = success(result)
		except Exception as error:
			log.exception("Could not apply a successful ClipDataCloud result.", exc_info=error)
			self._finish(False, "", onDone, announce=False)
			return
		self._finish(True, message, onDone, announce)

	def _finishFailedOperation(
		self,
		error: Exception,
		failureTemplate: str,
		onDone: CompletionCallback | None,
		announce: bool,
		stateAtStart: int,
		stateOperationId: int | None,
	) -> None:
		"""Apply a failed SDK result on the main thread."""
		if self._terminated:
			return
		operationId = stateOperationId if stateOperationId is not None else stateAtStart
		if operationId != self._stateOperationId:
			self._finishStaleOperation(onDone)
			return
		if isinstance(error, CloudClipboardError) and error.code is None:
			if self._loadError is None:
				log.debugWarning("ClipDataCloud SDK became unavailable.", exc_info=error)
			self._markSdkUnavailable(error.message)
			self._finish(False, _getSdkUnavailableMessage(), onDone, announce)
			return
		if not isinstance(error, CloudClipboardError):
			log.exception(
				"Unexpected ClipDataCloud operation failure.",
				exc_info=error,
			)
		if stateOperationId is not None:
			self._stateOperationInProgress = False
		if isinstance(error, CloudClipboardError) and error.code == USER_NOT_LOGGED_IN:
			# Translators: Cloud clipboard status reported by the native SDK.
			self._setLoggedOut(_("Not signed in"))
		message = failureTemplate.format(
			error=self._formatError(error),
		)
		self._finish(False, message, onDone, announce)

	def _finishStaleOperation(self, onDone: CompletionCallback | None) -> None:
		if not self._terminated:
			self._invokeCompletion(onDone, False, "")

	def _finish(
		self,
		success: bool,
		message: str,
		onDone: CompletionCallback | None = None,
		announce: bool = True,
	) -> None:
		"""Store and optionally announce an asynchronous operation result."""
		if self._terminated:
			return
		if message:
			self._statusMessage = message
			if announce:
				try:
					ui.message(message)
				except Exception:
					log.exception("Could not announce a ClipDataCloud result.")
		self._notifyStateChanged()
		self._invokeCompletion(onDone, success, message)

	def _invokeCompletion(
		self,
		callback: CompletionCallback | None,
		success: bool,
		message: str,
	) -> None:
		"""Invoke one completion callback without disrupting manager cleanup."""
		if callback is None:
			return
		try:
			callback(success, message)
		except Exception:
			log.exception("CloudSyncManager completion callback failed.")

	def _finishFetch(
		self,
		text: str,
		clipboardWriter: ClipboardWriter,
		onDone: CompletionCallback | None,
		stateAtStart: int,
	) -> None:
		"""Write fetched text through the supplied local writer."""
		if self._terminated:
			return
		if stateAtStart != self._stateOperationId:
			self._finishStaleOperation(onDone)
			return
		if not text:
			# Translators: Message shown when the cloud clipboard contains no text.
			self._finish(False, _("Tiantan Cloud Clipboard is empty"), onDone, announce=False)
			return
		try:
			clipboardWriter(text)
		except CloudClipboardWriteError as error:
			self._finish(False, str(error), onDone, announce=False)
			return
		except Exception as error:
			log.exception("Failed to write cloud clipboard text locally.", exc_info=error)
			# Translators: Error shown when downloaded cloud text cannot be written locally.
			message = _("Could not update the clipboard: {error}").format(error=error)
			self._finish(False, message, onDone, announce=False)
			return
		# Translators: Confirmation after downloading text from cloud clipboard.
		self._finish(True, _("Received from Tiantan Cloud Clipboard"), onDone, announce=False)

	def _autoUploadWorker(self, text: str, stateAtStart: int) -> None:
		"""Upload clipboard changes sequentially while retaining only the latest pending value."""
		currentText = text
		while not self._terminated:
			stopUpload = False
			uploadSucceeded = False
			with self._sdkOperationLock:
				if not self._isAutoUploadStateCurrent(stateAtStart):
					with self._uploadLock:
						pendingText = self._pendingUploadText
						self._pendingUploadText = None
						if pendingText is not None and self._isAutoUploadStateCurrent(self._stateOperationId):
							currentText = pendingText
							stateAtStart = self._stateOperationId
							continue
						self._uploadInProgress = False
						return
				try:
					self._getSdk().uploadText(currentText)
					uploadSucceeded = True
					self._autoUploadFailureReported = False
				except Exception as error:
					stopUpload = self._handleAutoUploadError(error, stateAtStart)
				with self._uploadLock:
					if not self._isAutoUploadStateCurrent(stateAtStart):
						self._pendingUploadText = None
						self._uploadInProgress = False
						return
					if stopUpload:
						self._pendingUploadText = None
						self._uploadInProgress = False
						return
					if self._pendingUploadText is not None and (
						self._pendingUploadText != currentText or not uploadSucceeded
					):
						currentText = self._pendingUploadText
						self._pendingUploadText = None
						continue
					self._pendingUploadText = None
					self._uploadInProgress = False
					return

	def _isAutoUploadStateCurrent(self, stateId: int) -> bool:
		return (
			not self._terminated
			and self._autoUploadEnabled
			and not self._stateOperationInProgress
			and stateId == self._stateOperationId
		)

	def _handleAutoUploadError(self, error: Exception, stateAtStart: int) -> bool:
		"""Handle automatic upload failure without repeated speech."""
		if isinstance(error, CloudClipboardError) and error.code is None:
			self._queueMainThread(self._handleSdkUnavailableForAutoUpload, error, stateAtStart)
			return True
		if not self._autoUploadFailureReported:
			if isinstance(error, CloudClipboardError):
				log.debugWarning(
					f"Automatic ClipDataCloud upload failed with SDK error code {error.code}.",
				)
			else:
				log.exception(
					"Unexpected automatic ClipDataCloud upload failure.",
					exc_info=error,
				)
			self._autoUploadFailureReported = True
		if isinstance(error, CloudClipboardError) and error.code == USER_NOT_LOGGED_IN:
			self._queueMainThread(self._handleNotLoggedInForAutoUpload, stateAtStart)
			return True
		return False

	def _handleNotLoggedInForAutoUpload(self, stateAtStart: int) -> None:
		"""Disable automatic upload after the SDK reports a signed-out account."""
		if self._terminated or stateAtStart != self._stateOperationId:
			return
		# Translators: Status shown when automatic upload stops because the account signed out.
		message = _("Tiantan Cloud Clipboard signed out. Automatic sync stopped.")
		self._setLoggedOut(message)
		self._notifyStateChanged()
		if not self._notLoggedInNotified:
			self._notLoggedInNotified = True
			ui.message(message)

	def _handleSdkUnavailableForAutoUpload(self, error: CloudClipboardError, stateAtStart: int) -> None:
		"""Disable cloud state after an automatic upload detects an unusable SDK."""
		if self._terminated or stateAtStart != self._stateOperationId:
			return
		if self._loadError is None:
			log.debugWarning("ClipDataCloud SDK became unavailable.", exc_info=error)
		self._markSdkUnavailable(error.message)
		self._notifyStateChanged()

	def _getInvalidUploadReason(self, text: str) -> str | None:
		try:
			validateUploadText(text)
		except CloudClipboardError as error:
			return self._formatUploadValidationError(error)
		return None

	def _formatUploadValidationError(self, error: CloudClipboardError) -> str:
		if error.message == TEXT_EMPTY_ERROR:
			# Translators: Message shown when there is no clipboard text to upload.
			return _("There is no clipboard text to send")
		if error.message == TEXT_CONTAINS_NUL_ERROR:
			# Translators: Message shown when clipboard text contains a NUL character.
			return _("Text contains a NUL character and was not sent")
		if error.message == TEXT_EXCEEDS_MAX_BYTES_ERROR:
			# Translators: Message shown when clipboard text exceeds the cloud size limit.
			return _("Text exceeds 1 MB and was not sent")
		if error.message == TEXT_INVALID_UTF8_ERROR:
			# Translators: Message shown when clipboard text contains invalid Unicode data.
			return _("Text contains invalid Unicode data and was not sent")
		return self._formatError(error)

	def _formatError(self, error: Exception) -> str:
		if isinstance(error, CloudClipboardError):
			if error.code is None:
				return _getSdkUnavailableMessage()
			if error.code == INVALID_ARGUMENT:
				# Translators: Cloud SDK error description.
				return _("Invalid argument")
			if error.code == BUFFER_TOO_SMALL:
				# Translators: Cloud SDK error description.
				return _("The output buffer is too small")
			if error.code == USER_NOT_LOGGED_IN:
				# Translators: Cloud SDK error description.
				return _("The account is not signed in")
			# Translators: Fallback description when the cloud SDK returns no error text.
			message = error.message or _("Unknown Tiantan Cloud Clipboard error")
			# Translators: Generic cloud SDK error with a numeric error code.
			return _("{message} (error code {code})").format(message=message, code=error.code)
		return str(error)

	def _setLoggedIn(self, nickname: str | None, isVip: bool | None) -> None:
		self._isLoggedIn = True
		self._nickname = nickname or None
		self._isVip = isVip
		self._autoUploadEnabled = self.isAutoSyncEnabled()
		self._notLoggedInNotified = False
		self._statusMessage = self._formatLoggedInStatus()

	def _setLoggedOut(self, statusMessage: str) -> None:
		self._stateOperationId += 1
		self._isLoggedIn = False
		self._nickname = None
		self._isVip = None
		self._autoUploadEnabled = False
		self._statusMessage = statusMessage
		with self._uploadLock:
			self._pendingUploadText = None

	def _markSdkUnavailable(self, errorMessage: str) -> None:
		self._sdk = None
		self._loadError = errorMessage
		self._stateOperationInProgress = False
		self._setLoggedOut(_getSdkUnavailableMessage())

	def _formatLoggedInStatus(self) -> str:
		if self._autoUploadEnabled:
			# Translators: Tiantan Cloud Clipboard automatic synchronization status when enabled.
			return _("Auto sync: on")
		# Translators: Tiantan Cloud Clipboard automatic synchronization status when disabled.
		return _("Auto sync: off")

	def _applyAutoSyncSetting(self) -> None:
		"""Apply the configured automatic synchronization state."""
		self._autoUploadEnabled = self._isLoggedIn and self.isAutoSyncEnabled() and not self._terminated
		if not self._autoUploadEnabled:
			with self._uploadLock:
				self._pendingUploadText = None
		if self._isLoggedIn:
			self._statusMessage = self._formatLoggedInStatus()
		self._notifyStateChanged()

	def _onConfigProfileSwitch(self) -> None:
		"""Apply automatic synchronization after an NVDA configuration profile switch."""
		self._applyAutoSyncSetting()

	def _notifyStateChanged(self) -> None:
		"""Notify the state observer without allowing it to interrupt producers."""
		if self._onStateChanged is not None and not self._terminated:
			try:
				self._onStateChanged()
			except Exception:
				log.exception("CloudSyncManager state callback failed.")
