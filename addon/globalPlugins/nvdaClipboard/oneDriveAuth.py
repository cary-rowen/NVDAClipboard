# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.


"""Acquire Microsoft sign-in tokens and keep their cache encrypted with DPAPI."""

from __future__ import annotations

from collections.abc import Mapping
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, cast

import addonHandler
import fileUtils
from logHandler import log
import NVDAState

from .oneDriveGraph import HTTP_TIMEOUT
from .oneDriveProtocol import OneDriveError


addonHandler.initTranslation()

CLIENT_ID = "99ce63cb-1d1e-436c-85d8-e78073d3e2d0"
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["Files.ReadWrite.AppFolder"]
TOKEN_CACHE_FILE_NAME = "nvdaClipboard.oneDriveTokenCache.dat"

# Translators: Error shown when bundled Microsoft sign-in support cannot load.
ERROR_AUTH_UNAVAILABLE = _("Microsoft sign-in support is unavailable")


class AuthenticationUnavailableError(OneDriveError):
	"""Report that bundled Microsoft authentication support cannot load."""


@dataclass(frozen=True, slots=True)
class AccountToken:
	"""Pair an access token with its MSAL account metadata."""

	accessToken: str
	accountId: str
	accountName: str
	account: Mapping[str, Any]


class _DATA_BLOB(ctypes.Structure):
	"""Describe a byte buffer passed to the Windows data protection API."""

	_fields_ = [
		("cbData", wintypes.DWORD),
		("pbData", ctypes.POINTER(ctypes.c_ubyte)),
	]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_crypt32.CryptProtectData.argtypes = (
	ctypes.POINTER(_DATA_BLOB),
	wintypes.LPCWSTR,
	ctypes.POINTER(_DATA_BLOB),
	ctypes.c_void_p,
	ctypes.c_void_p,
	wintypes.DWORD,
	ctypes.POINTER(_DATA_BLOB),
)
_crypt32.CryptProtectData.restype = wintypes.BOOL
_crypt32.CryptUnprotectData.argtypes = (
	ctypes.POINTER(_DATA_BLOB),
	ctypes.POINTER(wintypes.LPWSTR),
	ctypes.POINTER(_DATA_BLOB),
	ctypes.c_void_p,
	ctypes.c_void_p,
	wintypes.DWORD,
	ctypes.POINTER(_DATA_BLOB),
)
_crypt32.CryptUnprotectData.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
_kernel32.LocalFree.restype = ctypes.c_void_p
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_TOKEN_CACHE_ENTROPY = b"nvdaClipboard.oneDriveTokenCache.v1"


def _dataBlob(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array[ctypes.c_ubyte]]:
	"""Return a DATA_BLOB and the buffer that keeps its memory alive."""
	buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
	return _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _protectData(data: bytes) -> bytes:
	"""Encrypt bytes for the current Windows user with DPAPI."""
	inputBlob, inputBuffer = _dataBlob(data)
	entropyBlob, entropyBuffer = _dataBlob(_TOKEN_CACHE_ENTROPY)
	outputBlob = _DATA_BLOB()
	if not _crypt32.CryptProtectData(
		ctypes.byref(inputBlob),
		"NVDA Clipboard OneDrive token cache",
		ctypes.byref(entropyBlob),
		None,
		None,
		_CRYPTPROTECT_UI_FORBIDDEN,
		ctypes.byref(outputBlob),
	):
		raise ctypes.WinError(ctypes.get_last_error())
	try:
		return ctypes.string_at(outputBlob.pbData, outputBlob.cbData)
	finally:
		_kernel32.LocalFree(outputBlob.pbData)
		del inputBuffer, entropyBuffer


def _unprotectData(data: bytes) -> bytes:
	"""Decrypt bytes protected for the current Windows user with DPAPI."""
	inputBlob, inputBuffer = _dataBlob(data)
	entropyBlob, entropyBuffer = _dataBlob(_TOKEN_CACHE_ENTROPY)
	outputBlob = _DATA_BLOB()
	if not _crypt32.CryptUnprotectData(
		ctypes.byref(inputBlob),
		None,
		ctypes.byref(entropyBlob),
		None,
		None,
		_CRYPTPROTECT_UI_FORBIDDEN,
		ctypes.byref(outputBlob),
	):
		raise ctypes.WinError(ctypes.get_last_error())
	try:
		return ctypes.string_at(outputBlob.pbData, outputBlob.cbData)
	finally:
		_kernel32.LocalFree(outputBlob.pbData)
		del inputBuffer, entropyBuffer


class AuthManager:
	"""Acquire Microsoft tokens and persist the MSAL cache with DPAPI."""

	def __init__(self, cachePath: Path) -> None:
		"""Create an MSAL client backed by the encrypted token cache."""
		try:
			from ._vendor import msal
			from ._vendor.msal.oauth2cli import AuthCodeReceiver
		except Exception as error:
			raise AuthenticationUnavailableError(ERROR_AUTH_UNAVAILABLE) from error
		self.cachePath = cachePath
		self._msal = msal
		self._authCodeReceiver = AuthCodeReceiver
		self.cache = msal.SerializableTokenCache()
		self._activeDeviceFlow: dict[str, Any] | None = None
		self._deviceFlowCancelled = False
		self._loadCache()
		self.app = msal.PublicClientApplication(
			client_id=CLIENT_ID,
			authority=AUTHORITY,
			token_cache=self.cache,
			app_name="clipboard-enhancement",
			timeout=HTTP_TIMEOUT,
		)

	def _loadCache(self) -> None:
		"""Load and decrypt a persisted token cache, discarding invalid data."""
		if not self.cachePath.exists():
			return
		try:
			serialized = _unprotectData(self.cachePath.read_bytes()).decode("utf-8")
			self.cache.deserialize(serialized)
		except (OSError, UnicodeError, json.JSONDecodeError) as error:
			log.warning(f"Discarding an unreadable OneDrive token cache after {type(error).__name__}.")
			if NVDAState.shouldWriteToDisk():
				try:
					self.cachePath.unlink(missing_ok=True)
				except OSError:
					log.debugWarning("Could not remove an unreadable OneDrive token cache.", exc_info=True)

	def _saveCache(self) -> None:
		"""Atomically encrypt and save changed MSAL cache data."""
		if not self.cache.has_state_changed or not NVDAState.shouldWriteToDisk():
			return
		temporaryPath: Path | None = None
		try:
			self.cachePath.parent.mkdir(parents=True, exist_ok=True)
			data = _protectData(self.cache.serialize().encode("utf-8"))
			with fileUtils.FaultTolerantFile(str(self.cachePath)) as cacheFile:
				temporaryPath = Path(cacheFile.name)
				cacheFile.write(data)
		except OSError as error:
			self.cache.has_state_changed = True
			if temporaryPath is not None:
				try:
					temporaryPath.unlink(missing_ok=True)
				except OSError:
					log.debugWarning("Could not remove a temporary OneDrive token cache.", exc_info=True)
			raise OneDriveError(
				# Translators: Error when Microsoft sign-in succeeded but its encrypted cache could not be saved.
				_("Could not save Microsoft sign-in data"),
			) from error

	def _accountToken(self, result: Mapping[str, Any], account: Mapping[str, Any]) -> AccountToken:
		"""Build a stable account/token result from MSAL values."""
		accessToken = result.get("access_token")
		accountId = account.get("home_account_id")
		if not isinstance(accessToken, str) or not accessToken or not isinstance(accountId, str):
			raise OneDriveError(
				# Translators: Error shown when Microsoft sign-in did not return a usable account.
				_("Microsoft sign-in did not return a usable account"),
			)
		accountName = account.get("name") or account.get("username") or accountId
		return AccountToken(accessToken, accountId, str(accountName), account)

	def _findResultAccount(self, result: Mapping[str, Any]) -> Mapping[str, Any]:
		"""Find the cached account created or refreshed by a token result."""
		claims = result.get("id_token_claims")
		if isinstance(claims, Mapping):
			objectId = claims.get("oid")
			tenantId = claims.get("tid")
			if isinstance(objectId, str) and isinstance(tenantId, str):
				expectedAccountId = f"{objectId}.{tenantId}".casefold()
				for account in self.app.get_accounts():
					accountId = account.get("home_account_id")
					if isinstance(accountId, str) and accountId.casefold() == expectedAccountId:
						return cast(Mapping[str, Any], account)
		username = claims.get("preferred_username") if isinstance(claims, Mapping) else None
		accounts = (
			self.app.get_accounts(username=username)
			if isinstance(username, str) and username
			else self.app.get_accounts()
		)
		if len(accounts) != 1:
			raise OneDriveError(
				# Translators: Error shown when Microsoft sign-in did not retain an account.
				_("Microsoft sign-in did not retain the selected account"),
			)
		return cast(Mapping[str, Any], accounts[0])

	def getCachedToken(
		self,
		accountId: str | None = None,
		*,
		forceRefresh: bool = False,
	) -> AccountToken | None:
		"""Return a silent token for the requested or sole cached account."""
		accounts = cast(list[Mapping[str, Any]], self.app.get_accounts())
		if accountId is None:
			if len(accounts) != 1:
				return None
		else:
			accounts = [account for account in accounts if account.get("home_account_id") == accountId]
		for account in accounts:
			result = self.app.acquire_token_silent(
				SCOPES,
				account=account,
				force_refresh=forceRefresh,
			)
			self._saveCache()
			if isinstance(result, Mapping) and "access_token" in result:
				return self._accountToken(result, account)
		return None

	def loginInteractive(self, parentWindowHandle: int | None) -> AccountToken:
		"""Open the system browser and complete Microsoft interactive sign-in."""
		with self._authCodeReceiver(port=0) as receiver:
			# NVDA's global socket timeout must not shorten MSAL's local callback wait to 10 seconds.
			receiver._server.socket.settimeout(None)
			try:
				result = self.app.acquire_token_interactive(
					SCOPES,
					prompt="select_account",
					timeout=300,
					port=receiver.get_port(),
					parent_window_handle=parentWindowHandle,
					auth_code_receiver=receiver,
				)
			except self._msal.BrowserInteractionTimeoutError as error:
				raise OneDriveError(
					# Translators: Error when browser-based Microsoft sign-in is not completed in time.
					_("Microsoft sign-in timed out before it was completed"),
				) from error
		self._saveCache()
		if not isinstance(result, Mapping) or "access_token" not in result:
			raise self._authError(result)
		account = self._findResultAccount(result)
		return self._accountToken(result, account)

	def initiateDeviceFlow(self) -> tuple[dict[str, Any], str, str]:
		"""Start device-code authentication and return its user instructions."""
		flow = self.app.initiate_device_flow(scopes=SCOPES)
		if not isinstance(flow, dict) or not isinstance(flow.get("user_code"), str):
			raise self._authError(flow)
		verificationUrl = flow.get("verification_uri") or flow.get("verification_url")
		if not isinstance(verificationUrl, str) or not verificationUrl:
			raise OneDriveError(
				# Translators: Error shown when Microsoft device-code sign-in cannot start.
				_("Microsoft did not provide a device sign-in address"),
			)
		self._activeDeviceFlow = flow
		self._deviceFlowCancelled = False
		return flow, cast(str, flow["user_code"]), verificationUrl

	def completeDeviceFlow(self, flow: dict[str, Any]) -> AccountToken:
		"""Wait for the user to complete a previously initiated device flow."""
		cacheWasChanged = self.cache.has_state_changed
		cacheState = self.cache.serialize()
		self.cache.has_state_changed = cacheWasChanged
		try:
			result = self.app.acquire_token_by_device_flow(flow)
		finally:
			wasCancelled = self._deviceFlowCancelled
			self._activeDeviceFlow = None
			self._deviceFlowCancelled = False
			if wasCancelled:
				self.cache.deserialize(cacheState)
				self.cache.has_state_changed = cacheWasChanged
			self._saveCache()
		if wasCancelled:
			raise OneDriveError(
				# Translators: Status after the user cancels Microsoft device-code sign-in.
				_("Microsoft sign-in was canceled"),
			)
		if not isinstance(result, Mapping) or "access_token" not in result:
			raise self._authError(result)
		account = self._findResultAccount(result)
		return self._accountToken(result, account)

	def cancelDeviceFlow(self) -> None:
		"""Cause the active device flow to return without waiting for expiry."""
		self._deviceFlowCancelled = True
		if self._activeDeviceFlow is not None:
			self._activeDeviceFlow["expires_at"] = 0

	def removeAccount(self, account: Mapping[str, Any]) -> None:
		"""Remove one newly selected account from the token cache."""
		self.app.remove_account(account)
		self._saveCache()

	def signOut(self) -> None:
		"""Remove every cached Microsoft account and persistent token."""
		for account in self.app.get_accounts():
			self.app.remove_account(account)
		if NVDAState.shouldWriteToDisk():
			try:
				self.cachePath.unlink(missing_ok=True)
			except OSError as error:
				raise OneDriveError(
					# Translators: Error when locally saved Microsoft sign-in data cannot be removed.
					_("Could not remove Microsoft sign-in data"),
				) from error

	def _authError(self, result: object) -> OneDriveError:
		"""Convert an MSAL result to a localized message without token material."""
		if isinstance(result, Mapping):
			code = result.get("error")
			description = result.get("error_description")
			if code or description:
				return OneDriveError(
					# Translators: Microsoft authentication failure. {error} is a Microsoft description or code.
					_("Sign in failed: {error}").format(error=description or code),
				)
		return OneDriveError(
			# Translators: Generic Microsoft authentication failure.
			_("Sign in failed"),
		)
