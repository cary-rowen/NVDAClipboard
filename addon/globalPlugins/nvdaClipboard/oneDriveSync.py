# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Synchronize permitted clipboard history and categories with OneDrive."""

from __future__ import annotations

from base64 import b64decode
import binascii
from collections.abc import Callable, Mapping
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, replace
from functools import partial
from hashlib import sha1
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread, Timer
from time import monotonic
from typing import Any, cast
from urllib.parse import quote

import addonHandler
import fileUtils
from gui.message import MessageDialog, ReturnCode
from logHandler import log
import requests
import wx

from .oneDriveProtocol import (
	MAX_ITEM_FILE_BYTES,
	MAX_MANIFEST_BYTES,
	OneDriveError,
	_ItemSizeLimitError,
	_ManifestSizeLimitError,
	_INVALID_SYNC_DATA_MESSAGE,
	_MAX_SYNC_CLOCK,
	_applyNormalization,
	_cleanSnapshot,
	_cloudSnapshot,
	_compactSnapshot,
	_decodeItem,
	_decodeManifest,
	_emptySnapshot,
	_encodeItem,
	_encodeManifest,
	_generationRebaseStates,
	_livePayloadHashes,
	_maximumClock,
	_mergeSnapshots,
	_normalizationTargets,
	_rebaseLocalSnapshot,
	_tombstoneCount,
)
from .storage import (
	ClipboardStorage,
	ImageStorageLimitError,
	OneDriveAccountMismatchError,
	OneDriveLocalChangeError,
	StorageError,
	StorageFormatError,
)
from .storageModels import (
	ClipboardItem,
	OneDriveSyncSnapshot,
)


addonHandler.initTranslation()

_VENDOR_PATH = Path(__file__).with_name("_vendor").resolve()

CLIENT_ID = "99ce63cb-1d1e-436c-85d8-e78073d3e2d0"
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["Files.ReadWrite.AppFolder"]
MANIFEST_FILE_NAME = "nvdaClipboard.sync.manifest.json"
ITEM_FILE_PREFIX = "nvdaClipboard.sync.item."
ITEM_FILE_SUFFIX = ".json"
_SYNC_DEBOUNCE_SECONDS = 30
_SYNC_POLL_SECONDS = 5 * 60
# ponytail: hourly full scans bound payload repair; use Graph delta tokens only if this scan cost matters.
_SYNC_FULL_SCAN_INTERVAL_SECONDS = 60 * 60
_PROGRESS_UPDATE_SECONDS = 2.0
_HTTP_TIMEOUT = (10, 30)
_MAX_RETRY_AFTER_SECONDS = 30
_MANIFEST_RETRIES = 3
_MAX_TOMBSTONES_BEFORE_COMPACTION = 4096
_MAX_ORPHAN_DELETIONS_PER_SYNC = 100
_QUICK_XOR_HASH_BYTES = 20
_QUICK_XOR_SHIFT = 11
_QUICK_XOR_BLOCK_BYTES = _QUICK_XOR_HASH_BYTES * 8 * _QUICK_XOR_SHIFT
_TOKEN_CACHE_FILE_NAME = "nvdaClipboard.oneDriveTokenCache.dat"
_ITEM_FILE_RE = re.compile(
	rf"^{re.escape(ITEM_FILE_PREFIX)}[0-9a-f]{{64}}{re.escape(ITEM_FILE_SUFFIX)}$",
)

# Translators: Error shown when bundled Microsoft sign-in support cannot load.
_ERROR_AUTH_UNAVAILABLE = _("Microsoft sign-in support is unavailable")
# Translators: Error shown when a downloaded OneDrive manifest or item file is too large.
_ERROR_FILE_TOO_LARGE = _("A OneDrive synchronization file exceeds the supported size")
# Translators: Error shown when OneDrive returns malformed app-folder file-list data.
_ERROR_INVALID_FILE_LIST = _("OneDrive returned an invalid file list")
# Translators: Error used when another device changes the OneDrive manifest during synchronization.
_ERROR_MANIFEST_CHANGED = _("The OneDrive synchronization data changed on another device")
# Translators: Error shown when Microsoft sign-in can no longer authorize OneDrive.
_ERROR_SIGN_IN_EXPIRED = _("Sign in expired")
# Translators: Generic error shown when Microsoft sign-in fails.
_ERROR_SIGN_IN_FAILED = _("Sign in failed")
# Translators: Error shown when OneDrive synchronization is requested without signing in.
_ERROR_SIGN_IN_REQUIRED = _("Sign in to OneDrive before synchronizing")

# Translators: OneDrive status while cached Microsoft sign-in is checked.
_STATUS_CHECKING = _("Checking...")
# Translators: OneDrive status when no cached Microsoft account is usable.
_STATUS_NOT_SIGNED_IN = _("Not signed in")
# Translators: OneDrive progress while cloud synchronization metadata is read.
_STATUS_READING_SYNC_DATA = _("Reading OneDrive synchronization data...")
# Translators: OneDrive status while cached Microsoft credentials are removed.
_STATUS_SIGNING_OUT = _("Signing out...")
# Translators: OneDrive status while synchronization is running or about to start.
_STATUS_SYNCHRONIZING = _("Synchronizing...")

_CompletionCallback = Callable[[bool, str], None]
_DeviceCodeCallback = Callable[[str, str], None]


class _ManifestConflictError(OneDriveError):
	"""Report that another client changed the OneDrive manifest."""


class _AuthenticationUnavailableError(OneDriveError):
	"""Report that bundled Microsoft authentication support cannot load."""


class _OperationCancelledError(OneDriveError):
	"""Report an expected user cancellation without logging it as a failure."""


@dataclass(frozen=True, slots=True)
class OneDriveSyncState:
	"""Describe the current OneDrive state for user interfaces."""

	isAvailable: bool
	isLoggedIn: bool
	isBusy: bool
	isSigningOut: bool
	accountName: str
	statusMessage: str


@dataclass(frozen=True, slots=True)
class _AccountToken:
	"""Pair an access token with its MSAL account metadata."""

	accessToken: str
	accountId: str
	accountName: str
	account: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _RemoteManifest:
	"""Pair a decoded cloud snapshot with the current file ETag."""

	snapshot: OneDriveSyncSnapshot
	etag: str | None
	isMissing: bool


@dataclass(frozen=True, slots=True)
class _RemoteItemFile:
	"""Describe one content-addressed file listed in the OneDrive app folder."""

	size: int | None
	sha1Hash: str | None
	quickXorHash: bytes | None = None
	etag: str | None = None


class _ItemStage:
	"""Store downloaded immutable payloads on disk and load only one at a time."""

	def __init__(self, path: Path) -> None:
		"""Create a payload stage inside an operation-owned temporary directory."""
		self.path = path

	def store(self, payloadHash: bytes, data: bytes) -> None:
		"""Store one already validated bounded payload."""
		if len(payloadHash) != 32 or len(data) > MAX_ITEM_FILE_BYTES:
			raise ValueError(payloadHash)
		self._path(payloadHash).write_bytes(data)

	def load(self, payloadHash: bytes) -> ClipboardItem | None:
		"""Load and validate one staged payload, or return ``None`` when absent."""
		data = self.read(payloadHash)
		return _decodeItem(data, payloadHash) if data is not None else None

	def read(self, payloadHash: bytes) -> bytes | None:
		"""Read one staged payload, or return ``None`` when absent."""
		path = self._path(payloadHash)
		return path.read_bytes() if path.exists() else None

	def _path(self, payloadHash: bytes) -> Path:
		"""Return the private staging path for one validated payload hash."""
		if len(payloadHash) != 32:
			raise ValueError(payloadHash)
		return self.path / payloadHash.hex()


@dataclass(frozen=True, slots=True)
class _SyncResult:
	"""Describe one synchronization result and its observed manifest ETag."""

	uploaded: int
	downloaded: int
	changedLocally: bool
	manifestEtag: str | None


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


def _shouldWriteToDisk() -> bool:
	"""Return whether the current NVDA instance permits persistent writes."""
	try:
		import NVDAState

		return NVDAState.shouldWriteToDisk()
	except (ImportError, AttributeError):
		log.debugWarning(
			"Could not determine whether the OneDrive token cache may be written.",
			exc_info=True,
		)
		return False


class _AuthManager:
	"""Acquire Microsoft tokens and persist the MSAL cache with DPAPI."""

	def __init__(self, cachePath: Path) -> None:
		"""Create an MSAL client backed by the encrypted token cache."""
		try:
			from ._vendor import msal
			from ._vendor.msal.oauth2cli import AuthCodeReceiver

			modulePath = getattr(msal, "__file__", None)
			if (
				not isinstance(modulePath, str)
				or not Path(modulePath).resolve().is_relative_to(_VENDOR_PATH)
				or getattr(msal, "__version__", None) != "1.37.0"
			):
				raise ImportError(f"Unexpected {msal.__name__} module")
		except Exception as error:
			raise _AuthenticationUnavailableError(_ERROR_AUTH_UNAVAILABLE) from error
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
			timeout=_HTTP_TIMEOUT,
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
			if _shouldWriteToDisk():
				try:
					self.cachePath.unlink(missing_ok=True)
				except OSError:
					log.debugWarning("Could not remove an unreadable OneDrive token cache.", exc_info=True)

	def _saveCache(self) -> None:
		"""Atomically encrypt and save changed MSAL cache data."""
		if not self.cache.has_state_changed or not _shouldWriteToDisk():
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

	def _accountToken(self, result: Mapping[str, Any], account: Mapping[str, Any]) -> _AccountToken:
		"""Build a stable account/token result from MSAL values."""
		accessToken = result.get("access_token")
		accountId = account.get("home_account_id")
		if not isinstance(accessToken, str) or not accessToken or not isinstance(accountId, str):
			raise OneDriveError(
				# Translators: Error shown when Microsoft sign-in did not return a usable account.
				_("Microsoft sign-in did not return a usable account"),
			)
		accountName = account.get("name") or account.get("username") or accountId
		return _AccountToken(accessToken, accountId, str(accountName), account)

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
	) -> _AccountToken | None:
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

	def loginInteractive(self, parentWindowHandle: int | None) -> _AccountToken:
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

	def completeDeviceFlow(self, flow: dict[str, Any]) -> _AccountToken:
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
		if _shouldWriteToDisk():
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


class _GraphClient:
	"""Perform bounded Microsoft Graph operations in the OneDrive app folder."""

	_ROOT = "https://graph.microsoft.com/v1.0/me/drive/special/approot"

	def __init__(self, accessToken: str, cancelEvent: Event, session: requests.Session) -> None:
		"""Create a Graph client for one cancellable synchronization operation."""
		self.accessToken = accessToken
		self.cancelEvent = cancelEvent
		self.session = session

	def ensureAppFolder(self) -> None:
		"""Ensure the app folder exists and is accessible."""
		self._request("GET", self._ROOT, expected=(200,))

	def getManifest(self) -> _RemoteManifest:
		"""Download the versioned sync manifest or return an empty cloud state."""
		etag = self._getEtag(MANIFEST_FILE_NAME)
		if etag is None:
			return _RemoteManifest(_emptySnapshot(), None, True)
		response = self._request(
			"GET",
			self._contentUrl(MANIFEST_FILE_NAME),
			expected=(200, 404),
			stream=True,
		)
		if response.status_code == 404:
			response.close()
			raise _ManifestConflictError(
				_ERROR_MANIFEST_CHANGED,
				statusCode=412,
			)
		data = self._readBoundedResponse(response, MAX_MANIFEST_BYTES)
		return _RemoteManifest(_decodeManifest(data), etag, False)

	def isManifestUnchanged(self, etag: str) -> bool:
		"""Return whether a conditional manifest metadata request reports no change."""
		if not etag or "\r" in etag or "\n" in etag:
			raise ValueError(etag)
		response = self._request(
			"GET",
			f"{self._itemUrl(MANIFEST_FILE_NAME)}?$select=eTag",
			expected=(200, 304, 404),
			headers={"If-None-Match": etag},
		)
		try:
			return response.status_code == 304
		finally:
			response.close()

	def putManifest(self, data: bytes, remote: _RemoteManifest) -> str:
		"""Conditionally replace or create the synchronization manifest."""
		headers = {"Content-Type": "application/json; charset=utf-8"}
		if remote.etag:
			headers["If-Match"] = remote.etag
		elif remote.isMissing:
			headers["If-None-Match"] = "*"
		else:
			raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
		try:
			response = self._request(
				"PUT",
				self._contentUrl(MANIFEST_FILE_NAME),
				expected=(200, 201),
				headers=headers,
				data=data,
				stream=True,
			)
		except OneDriveError as error:
			if error.statusCode == 412:
				raise _ManifestConflictError(str(error), statusCode=412) from error
			raise
		try:
			payload = json.loads(self._readBoundedResponse(response, MAX_MANIFEST_BYTES))
		except (UnicodeDecodeError, json.JSONDecodeError) as error:
			raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE) from error
		etag = payload.get("eTag") if isinstance(payload, Mapping) else None
		if not isinstance(etag, str) or not etag or "\r" in etag or "\n" in etag:
			raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
		return etag

	def getItemData(self, payloadHash: bytes) -> bytes:
		"""Download one bounded content-addressed clipboard item file."""
		response = self._request(
			"GET",
			self._contentUrl(_itemFileName(payloadHash)),
			expected=(200,),
			stream=True,
		)
		return self._readBoundedResponse(response, MAX_ITEM_FILE_BYTES)

	def putItemData(self, payloadHash: bytes, data: bytes) -> None:
		"""Upload one immutable content-addressed clipboard item."""
		self._request(
			"PUT",
			self._contentUrl(_itemFileName(payloadHash)),
			expected=(200, 201),
			headers={"Content-Type": "application/json; charset=utf-8"},
			data=data,
		)

	def listItemFiles(
		self,
		relevantPayloads: set[bytes],
	) -> tuple[dict[bytes, _RemoteItemFile], tuple[tuple[bytes, str], ...]]:
		"""List relevant item metadata and a bounded batch of unreferenced files."""
		url = f"{self._ROOT}/children?$select=name,size,file,eTag&$top=1000"
		result: dict[bytes, _RemoteItemFile] = {}
		orphans: list[tuple[bytes, str]] = []
		seenUrls: set[str] = set()
		while url:
			if url in seenUrls:
				raise OneDriveError(_ERROR_INVALID_FILE_LIST)
			seenUrls.add(url)
			response = self._request("GET", url, expected=(200,))
			try:
				payload = response.json()
			except ValueError as error:
				raise OneDriveError(_ERROR_INVALID_FILE_LIST) from error
			finally:
				response.close()
			if not isinstance(payload, Mapping) or not isinstance(payload.get("value"), list):
				raise OneDriveError(_ERROR_INVALID_FILE_LIST)
			for child in payload["value"]:
				name = child.get("name") if isinstance(child, Mapping) else None
				if not isinstance(name, str) or not _ITEM_FILE_RE.fullmatch(name):
					continue
				payloadHash = bytes.fromhex(name[len(ITEM_FILE_PREFIX) : -len(ITEM_FILE_SUFFIX)])
				fileValue = child.get("file")
				if not isinstance(fileValue, Mapping):
					continue
				sizeValue = child.get("size")
				size = sizeValue if isinstance(sizeValue, int) and not isinstance(sizeValue, bool) else None
				hashes = fileValue.get("hashes")
				sha1Value = hashes.get("sha1Hash") if isinstance(hashes, Mapping) else None
				quickXorValue = hashes.get("quickXorHash") if isinstance(hashes, Mapping) else None
				if sha1Value is not None and not (
					isinstance(sha1Value, str) and re.fullmatch(r"[0-9A-Fa-f]{40}", sha1Value)
				):
					sha1Value = None
				etagValue = child.get("eTag")
				etag = (
					etagValue
					if isinstance(etagValue, str)
					and etagValue
					and "\r" not in etagValue
					and "\n" not in etagValue
					else None
				)
				itemFile = _RemoteItemFile(
					size=size,
					sha1Hash=sha1Value.lower() if isinstance(sha1Value, str) else None,
					quickXorHash=_quickXorHashFromGraph(quickXorValue),
					etag=etag,
				)
				if payloadHash in relevantPayloads:
					result[payloadHash] = itemFile
				elif etag is not None and len(orphans) < _MAX_ORPHAN_DELETIONS_PER_SYNC:
					orphans.append((payloadHash, etag))
			nextUrl = payload.get("@odata.nextLink")
			if nextUrl is None:
				url = ""
			elif isinstance(nextUrl, str) and nextUrl.startswith("https://graph.microsoft.com/v1.0/"):
				url = nextUrl
			else:
				raise OneDriveError(_ERROR_INVALID_FILE_LIST)
		return result, tuple(orphans)

	def deleteItemData(self, payloadHash: bytes, etag: str) -> None:
		"""Delete one still-unreferenced item without racing a concurrent reuse."""
		try:
			response = self._request(
				"DELETE",
				self._itemUrl(_itemFileName(payloadHash)),
				expected=(204, 404),
				headers={"If-Match": etag},
			)
		except OneDriveError as error:
			if error.statusCode == 412:
				return
			raise
		response.close()

	def _getEtag(self, fileName: str) -> str | None:
		"""Fetch an existing file's ETag, or return ``None`` when it is absent."""
		response = self._request(
			"GET",
			f"{self._itemUrl(fileName)}?$select=eTag",
			expected=(200, 404),
		)
		if response.status_code == 404:
			response.close()
			return None
		try:
			payload = response.json()
		except ValueError as error:
			raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE) from error
		finally:
			response.close()
		etag = payload.get("eTag") if isinstance(payload, Mapping) else None
		if not isinstance(etag, str) or not etag or "\r" in etag or "\n" in etag:
			raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
		return etag

	def _contentUrl(self, fileName: str) -> str:
		"""Return a Graph content URL for one app-folder file."""
		return f"{self._ROOT}:/{quote(fileName)}:/content"

	def _itemUrl(self, fileName: str) -> str:
		"""Return a Graph item URL for one app-folder file."""
		return f"{self._ROOT}:/{quote(fileName)}"

	def _request(
		self,
		method: str,
		url: str,
		*,
		expected: tuple[int, ...],
		headers: Mapping[str, str] | None = None,
		**kwargs: Any,
	) -> requests.Response:
		"""Send one authenticated Graph request with finite timeouts."""
		requestHeaders = dict(headers or {})
		requestHeaders["Authorization"] = f"Bearer {self.accessToken}"
		requestHeaders.setdefault("Accept", "application/json")
		for attempt in range(2):
			self._checkCancelled()
			try:
				response = self.session.request(
					method,
					url,
					headers=requestHeaders,
					timeout=_HTTP_TIMEOUT,
					**kwargs,
				)
			except requests.RequestException as error:
				raise OneDriveError(
					# Translators: Error shown when OneDrive cannot be reached.
					_("Could not connect to OneDrive"),
				) from error
			if self.cancelEvent.is_set():
				response.close()
				self._checkCancelled()
			if response.status_code in expected:
				return response
			error = _graphError(response)
			response.close()
			if attempt == 0 and response.status_code in (429, 503) and error.retryAfter is not None:
				# ponytail: retry once with a bounded delay; add cancellable backoff if throttling is common.
				if self.cancelEvent.wait(min(error.retryAfter, _MAX_RETRY_AFTER_SECONDS)):
					self._checkCancelled()
				continue
			raise error
		raise AssertionError

	def _checkCancelled(self) -> None:
		"""Abort a Graph operation after the user requests sign-out."""
		if self.cancelEvent.is_set():
			raise _OperationCancelledError("")

	def _readBoundedResponse(self, response: requests.Response, limit: int) -> bytes:
		"""Read a streamed response without accepting an oversized body."""
		try:
			contentLength = response.headers.get("Content-Length")
			try:
				contentLengthValue = (
					int(contentLength) if contentLength and contentLength.isdecimal() else None
				)
			except ValueError:
				contentLengthValue = None
			if contentLengthValue is not None and contentLengthValue > limit:
				raise OneDriveError(_ERROR_FILE_TOO_LARGE)
			data = bytearray()
			for chunk in response.iter_content(64 * 1024):
				self._checkCancelled()
				data.extend(chunk)
				if len(data) > limit:
					raise OneDriveError(_ERROR_FILE_TOO_LARGE)
			return bytes(data)
		except requests.RequestException as error:
			raise OneDriveError(
				# Translators: Error shown when OneDrive cannot be reached.
				_("Could not connect to OneDrive"),
			) from error
		finally:
			response.close()


def _graphError(response: requests.Response) -> OneDriveError:
	"""Convert a Graph response to a localized error without exposing content."""
	errorCode = ""
	try:
		payload = response.json()
		error = payload.get("error") if isinstance(payload, Mapping) else None
		if isinstance(error, Mapping) and isinstance(error.get("code"), str):
			errorCode = cast(str, error["code"])
	except ValueError:
		pass
	retryAfterValue = response.headers.get("Retry-After")
	try:
		retryAfter = int(retryAfterValue) if retryAfterValue and retryAfterValue.isdecimal() else None
	except ValueError:
		retryAfter = None
	if response.status_code == 401:
		message = _ERROR_SIGN_IN_EXPIRED
	elif response.status_code == 403:
		# Translators: Error shown when the Microsoft account denies OneDrive app-folder access.
		message = _("Microsoft denied access to the OneDrive application folder")
	elif response.status_code == 404:
		# Translators: Error shown when a file referenced by the OneDrive manifest is missing.
		message = _("OneDrive synchronization data is incomplete")
	elif response.status_code == 412:
		message = _ERROR_MANIFEST_CHANGED
	elif response.status_code == 429:
		# Translators: Error shown when Microsoft asks the add-on to reduce request frequency.
		message = _("OneDrive is temporarily limiting synchronization requests")
	elif response.status_code in (507,):
		# Translators: Error shown when the user's OneDrive has insufficient storage.
		message = _("OneDrive does not have enough free storage")
	else:
		# Translators: Generic Microsoft Graph failure. {status} is an HTTP status and {code} a safe code.
		message = _("OneDrive request failed ({status}{code})").format(
			status=response.status_code,
			code=f", {errorCode}" if errorCode else "",
		)
	return OneDriveError(message, statusCode=response.status_code, retryAfter=retryAfter)


def _itemFileName(payloadHash: bytes) -> str:
	"""Return the deterministic OneDrive filename for a payload hash."""
	if len(payloadHash) != 32:
		raise ValueError(payloadHash)
	return f"{ITEM_FILE_PREFIX}{payloadHash.hex()}{ITEM_FILE_SUFFIX}"


def _quickXorHash(data: bytes) -> bytes:
	"""Return Microsoft OneDrive's 160-bit QuickXorHash for immutable file data."""
	widthInBits = _QUICK_XOR_HASH_BYTES * 8
	mask = (1 << widthInBits) - 1
	foldedData = 0
	for offset in range(0, len(data), _QUICK_XOR_BLOCK_BYTES):
		foldedData ^= int.from_bytes(data[offset : offset + _QUICK_XOR_BLOCK_BYTES], "little")
	result = 0
	for index, value in enumerate(foldedData.to_bytes(_QUICK_XOR_BLOCK_BYTES, "little")):
		shift = index * _QUICK_XOR_SHIFT % widthInBits
		result ^= ((value << shift) | (value >> (widthInBits - shift))) & mask
	result ^= len(data) << ((_QUICK_XOR_HASH_BYTES - 8) * 8)
	return (result & mask).to_bytes(_QUICK_XOR_HASH_BYTES, "little")


def _quickXorHashFromGraph(value: object) -> bytes | None:
	"""Decode a strict Graph QuickXorHash, or return ``None`` when unavailable."""
	if not isinstance(value, str):
		return None
	try:
		decoded = b64decode(value, validate=True)
	except (ValueError, binascii.Error):
		return None
	return decoded if len(decoded) == _QUICK_XOR_HASH_BYTES else None


def _remoteItemMatches(itemFile: _RemoteItemFile, data: bytes) -> bool:
	"""Return whether listed size and an available content hash verify canonical item data."""
	if itemFile.size != len(data):
		return False
	if itemFile.sha1Hash is not None:
		return itemFile.sha1Hash == sha1(data).hexdigest()
	return itemFile.quickXorHash is not None and itemFile.quickXorHash == _quickXorHash(data)


def _canReuseRemoteItem(itemFile: _RemoteItemFile, data: bytes, isReferenced: bool) -> bool:
	"""Return whether a manifest-referenced immutable item can be reused unchanged."""
	return isReferenced and _remoteItemMatches(itemFile, data)


class OneDriveSyncManager:
	"""Coordinate Microsoft sign-in and serialized background synchronization."""

	def __init__(
		self,
		storage: ClipboardStorage,
		*,
		onStateChanged: Callable[[], None] | None = None,
		onDataChanged: Callable[[], None] | None = None,
	) -> None:
		"""Create a manager around one clipboard database and Microsoft account."""
		self._storage = storage
		self._cachePath = storage.path.with_name(_TOKEN_CACHE_FILE_NAME)
		self._onStateChanged = onStateChanged
		self._onDataChanged = onDataChanged
		self._lock = Lock()
		self._auth: _AuthManager | None = None
		self._accountToken: _AccountToken | None = None
		self._debounceTimer: Timer | None = None
		self._debounceGeneration = 0
		self._pollTimer: Timer | None = None
		self._lastManifestEtag: str | None = None
		self._lastFullSyncAt: float | None = None
		self._hasPendingSync = False
		self._signOutPending = False
		self._pendingSignOutDone: _CompletionCallback | None = None
		self._operationCancelEvent = Event()
		self._lastProgressUpdateAt = 0.0
		self._deviceCodeCancellationRequested = False
		self._isInitialized = False
		self._isTerminated = False
		self._state = OneDriveSyncState(
			isAvailable=True,
			isLoggedIn=False,
			isBusy=False,
			isSigningOut=False,
			accountName="",
			statusMessage=_STATUS_CHECKING,
		)

	def initialize(self) -> None:
		"""Check cached Microsoft login and synchronize silently after NVDA starts."""
		with self._lock:
			if self._isInitialized or self._isTerminated:
				return
			self._isInitialized = True
		busyMessage = _STATUS_CHECKING
		# Translators: Generic error when startup OneDrive initialization fails unexpectedly.
		failureMessage = _("OneDrive setup failed")
		self._startOperation(
			self._initializeWorker,
			name="nvdaClipboard.oneDriveStartup",
			busyMessage=busyMessage,
			failureMessage=failureMessage,
			onDone=None,
		)

	def terminate(self) -> None:
		"""Cancel timers and suppress results from unfinished daemon operations."""
		with self._lock:
			if self._isTerminated:
				return
			self._isTerminated = True
			self._isInitialized = False
			self._cancelTimersLocked()
			self._hasPendingSync = False
			self._signOutPending = False
			self._pendingSignOutDone = None
			self._operationCancelEvent.set()
			auth = self._auth
			self._onStateChanged = None
			self._onDataChanged = None
		if auth is not None:
			auth.cancelDeviceFlow()

	def getState(self) -> OneDriveSyncState:
		"""Return an immutable snapshot of current OneDrive state."""
		with self._lock:
			return self._state

	def loginInteractive(
		self,
		parentWindowHandle: int | None,
		onDone: _CompletionCallback | None = None,
	) -> None:
		"""Sign in through the system browser and synchronize after success."""

		def work() -> tuple[str, bool]:
			"""Complete browser login and initial synchronization in the worker."""
			token = self._getAuth().loginInteractive(parentWindowHandle)
			return self._finishLogin(token)

		# Translators: OneDrive status while the system browser handles Microsoft login.
		busyMessage = _("Signing in...")
		failureMessage = _ERROR_SIGN_IN_FAILED
		self._startOperation(
			work,
			name="nvdaClipboard.oneDriveLogin",
			busyMessage=busyMessage,
			failureMessage=failureMessage,
			onDone=onDone,
		)

	def startDeviceCode(
		self,
		onCode: _DeviceCodeCallback,
		onDone: _CompletionCallback | None = None,
	) -> None:
		"""Start fallback device-code login and report its code on the GUI thread."""

		def work() -> tuple[str, bool]:
			"""Create, display, and complete one Microsoft device-code flow."""
			auth = self._getAuth()
			flow, userCode, verificationUrl = auth.initiateDeviceFlow()
			with self._lock:
				wasCancelled = self._deviceCodeCancellationRequested
			if wasCancelled:
				auth.cancelDeviceFlow()
			else:
				self._postDeviceCode(onCode, userCode, verificationUrl)
			self._ensureActive()
			return self._finishLogin(auth.completeDeviceFlow(flow))

		with self._lock:
			self._deviceCodeCancellationRequested = False
		# Translators: OneDrive status while Microsoft device-code login is running.
		busyMessage = _("Waiting for sign-in...")
		failureMessage = _ERROR_SIGN_IN_FAILED
		self._startOperation(
			work,
			name="nvdaClipboard.oneDriveDeviceLogin",
			busyMessage=busyMessage,
			failureMessage=failureMessage,
			onDone=onDone,
		)

	def cancelDeviceCode(self) -> None:
		"""Cancel any active Microsoft device-code login without blocking the GUI."""
		with self._lock:
			self._deviceCodeCancellationRequested = True
			auth = self._auth
			if self._isTerminated:
				return
		if auth is not None:
			auth.cancelDeviceFlow()

	def syncNow(self, onDone: _CompletionCallback | None = None) -> None:
		"""Synchronize current history and categories immediately."""
		self._requestSync(onDone=onDone, isAutomatic=False)

	def signOut(self, onDone: _CompletionCallback | None = None) -> None:
		"""Cancel current synchronization, then remove cached Microsoft credentials."""
		with self._lock:
			if self._isTerminated or self._state.isSigningOut:
				return
			shouldQueue = self._state.isBusy
			if shouldQueue:
				self._signOutPending = True
				self._pendingSignOutDone = onDone
				self._hasPendingSync = False
				self._cancelTimersLocked()
				self._operationCancelEvent.set()
			if shouldQueue or (self._isInitialized and self._state.isAvailable):
				self._state = replace(
					self._state,
					isSigningOut=True,
					statusMessage=_STATUS_SIGNING_OUT,
				)
		if shouldQueue:
			self._postStateChanged()
			return
		self._startSignOut(onDone)

	def _startSignOut(self, onDone: _CompletionCallback | None) -> None:
		"""Start credential removal after prior work has stopped."""
		self._startOperation(
			self._signOutWorker,
			name="nvdaClipboard.oneDriveSignOut",
			busyMessage=_STATUS_SIGNING_OUT,
			# Translators: Generic error when OneDrive sign-out fails unexpectedly.
			failureMessage=_("Sign out failed"),
			onDone=onDone,
			isSigningOut=True,
		)

	def notifyLocalChange(self) -> None:
		"""Debounce a synchronization after eligible local data changes."""
		with self._lock:
			if self._isTerminated or not self._isInitialized or not self._state.isLoggedIn:
				return
			if self._debounceTimer is not None:
				self._debounceTimer.cancel()
			self._debounceGeneration += 1
			timer = Timer(
				_SYNC_DEBOUNCE_SECONDS,
				self._debounceTimerFired,
				args=(self._debounceGeneration,),
			)
			timer.daemon = True
			self._debounceTimer = timer
			try:
				timer.start()
			except RuntimeError:
				self._debounceTimer = None
				log.exception("Could not start the OneDrive local-change timer.")

	def _initializeWorker(self) -> tuple[str, bool]:
		"""Restore a bound cached account and run the startup synchronization."""
		self._ensureActive()
		if not self._cachePath.is_file() or not _shouldWriteToDisk():
			self._setAccountToken(None)
			return _STATUS_NOT_SIGNED_IN, False
		auth = self._getAuth()
		self._ensureActive()
		accountId = self._storage.getOneDriveSnapshot().accountId
		token = auth.getCachedToken(accountId)
		self._ensureActive()
		if token is None:
			self._setAccountToken(None)
			return _STATUS_NOT_SIGNED_IN, False
		self._storage.bindOneDriveAccount(token.accountId)
		self._setAccountToken(token)
		self._requestSync(onDone=None, isAutomatic=True)
		return _STATUS_SYNCHRONIZING, False

	def _finishLogin(self, token: _AccountToken) -> tuple[str, bool]:
		"""Confirm any account switch, bind it locally, and synchronize."""
		self._ensureActive()
		currentAccountId = self._storage.getOneDriveSnapshot().accountId
		isSwitch = currentAccountId is not None and currentAccountId != token.accountId
		if isSwitch:
			message = _(
				# Translators: Confirmation before linking this NVDA configuration to a different account.
				"This NVDA configuration is linked to another Microsoft account. "
				"Switching to {account} resets its synchronization baseline and merges "
				"the current local history and categories with that account's OneDrive. Continue?",
			).format(account=token.accountName)
			# Translators: Title of the OneDrive account-switch confirmation.
			caption = _("Switch OneDrive account")
			# Translators: Confirmation button that switches the OneDrive account.
			okLabel = _("&Switch account")
			if MessageDialog.confirm(message, caption, okLabel=okLabel) != ReturnCode.OK:
				try:
					self._getAuth().removeAccount(token.account)
				except Exception:
					log.debugWarning("Could not remove the unselected Microsoft account.", exc_info=True)
				# Translators: Status after canceling a OneDrive account switch.
				raise _OperationCancelledError(_("OneDrive account switch was canceled"))
		self._ensureActive()
		self._storage.bindOneDriveAccount(token.accountId, reset=isSwitch)
		self._setAccountToken(token)
		self._requestSync(onDone=None, isAutomatic=True)
		return _STATUS_SYNCHRONIZING, False

	def _signOutWorker(self) -> tuple[str, bool]:
		"""Remove all MSAL accounts and clear runtime login state."""
		with self._lock:
			self._cancelTimersLocked()
			self._hasPendingSync = False
		try:
			self._getAuth().signOut()
		finally:
			with self._lock:
				self._cancelTimersLocked()
				self._hasPendingSync = False
		self._setAccountToken(None)
		# Translators: Status after removing the cached Microsoft account.
		return _("Signed out"), False

	def _requestSync(
		self,
		*,
		onDone: _CompletionCallback | None,
		isAutomatic: bool,
		isPoll: bool = False,
	) -> None:
		"""Start one synchronization or retain an automatic request while busy."""
		with self._lock:
			isLoggedIn = self._state.isLoggedIn
			isBusy = self._state.isBusy
			isTerminated = self._isTerminated
			if isAutomatic and isBusy and isLoggedIn and not isTerminated:
				# Poll timers schedule their successor before requesting synchronization.
				if not isPoll:
					self._hasPendingSync = True
				return
		if not isLoggedIn:
			self._postCompletion(onDone, False, _ERROR_SIGN_IN_REQUIRED)
			return
		busyMessage = _STATUS_SYNCHRONIZING
		# Translators: Generic error when OneDrive synchronization fails unexpectedly.
		failureMessage = _("Synchronization failed")
		self._startOperation(
			partial(self._syncWorker, isPoll=isPoll),
			name="nvdaClipboard.oneDriveSync",
			busyMessage=busyMessage,
			failureMessage=failureMessage,
			onDone=onDone,
			requiresLogin=True,
		)

	def _syncWorker(self, *, isPoll: bool = False) -> tuple[str, bool]:
		"""Run one synchronization and return its user-facing completion state."""
		result = self._synchronizeWithRefresh(isPoll=isPoll)
		if result.uploaded or result.downloaded or result.changedLocally:
			log.debug(
				f"OneDrive synchronization changed data: uploaded={result.uploaded}, "
				f"downloaded={result.downloaded}, localChanged={result.changedLocally}",
			)
		# Translators: Status after a complete OneDrive synchronization succeeds.
		return _("Synchronization complete"), result.changedLocally

	def _synchronizeWithRefresh(self, *, isPoll: bool = False) -> _SyncResult:
		"""Synchronize with one normal and at most one forced silent token refresh."""
		token = self._getAccountToken()
		if token is None:
			raise OneDriveError(_ERROR_SIGN_IN_EXPIRED, statusCode=401)
		auth = self._getAuth()
		cachedToken = auth.getCachedToken(token.accountId)
		if cachedToken is None:
			self._setAccountToken(None)
			raise OneDriveError(_ERROR_SIGN_IN_EXPIRED, statusCode=401)
		self._setAccountToken(cachedToken)
		try:
			return self._synchronize(cachedToken, isPoll=isPoll)
		except OneDriveError as error:
			if error.statusCode != 401:
				raise
		refreshedToken = auth.getCachedToken(cachedToken.accountId, forceRefresh=True)
		if refreshedToken is None:
			self._setAccountToken(None)
			raise OneDriveError(_ERROR_SIGN_IN_EXPIRED, statusCode=401)
		self._setAccountToken(refreshedToken)
		try:
			return self._synchronize(refreshedToken, isPoll=isPoll)
		except OneDriveError as error:
			if error.statusCode == 401:
				self._setAccountToken(None)
			raise

	def _synchronize(self, token: _AccountToken, *, isPoll: bool = False) -> _SyncResult:
		"""Merge, transfer, commit, and apply one account's synchronized state."""
		self._storage.excludeIneligibleOneDriveItems()
		with requests.Session() as session:
			graph = _GraphClient(token.accessToken, self._operationCancelEvent, session)
			with self._lock:
				manifestEtag = self._lastManifestEtag
				lastFullSyncAt = self._lastFullSyncAt
			if (
				isPoll
				and manifestEtag is not None
				and lastFullSyncAt is not None
				and monotonic() - lastFullSyncAt < _SYNC_FULL_SCAN_INTERVAL_SECONDS
				and not self._storage.hasPendingOneDriveChanges()
				and graph.isManifestUnchanged(manifestEtag)
			):
				return _SyncResult(0, 0, False, manifestEtag)
			self._updateProgress(_STATUS_READING_SYNC_DATA, force=True)
			graph.ensureAppFolder()
			with TemporaryDirectory(prefix="nvdaClipboard-oneDrive-") as temporaryDirectory:
				result = self._synchronizeStaged(token, graph, _ItemStage(Path(temporaryDirectory)))
		with self._lock:
			if self._accountToken is not None and self._accountToken.accountId == token.accountId:
				self._lastManifestEtag = result.manifestEtag
				self._lastFullSyncAt = monotonic()
		return result

	def _synchronizeStaged(
		self,
		token: _AccountToken,
		graph: _GraphClient,
		stage: _ItemStage,
	) -> _SyncResult:
		"""Synchronize while retaining downloaded payloads only on temporary storage."""
		downloadedHashes: set[bytes] = set()
		uploadedHashes: set[bytes] = set()
		changedLocally = False
		for attempt in range(_MANIFEST_RETRIES):
			self._ensureActive()
			if attempt:
				self._updateProgress(_STATUS_READING_SYNC_DATA, force=True)
			readResult = self._readAndMergeSnapshots(token, graph)
			if readResult is None:
				continue
			local, remote, merged, itemFiles, orphanFiles = readResult
			downloadResult = self._downloadMissingPayloads(graph, stage, remote.snapshot, merged)
			if downloadResult is None:
				continue
			imageBytes, attemptDownloadedHashes = downloadResult
			downloadedHashes.update(attemptDownloadedHashes)
			manifestResult = self._normalizeAndEncodeManifest(local, merged, imageBytes)
			if manifestResult is None:
				continue
			merged, manifestData = manifestResult
			attemptUploadedHashes, oversizedPayloads, shouldRetry = self._uploadOrReusePayloads(
				graph,
				stage,
				remote.snapshot,
				merged,
				itemFiles,
			)
			uploadedHashes.update(attemptUploadedHashes)
			if shouldRetry:
				continue
			if oversizedPayloads:
				for payloadHash in oversizedPayloads:
					changedLocally = self._storage.excludeOneDrivePayload(payloadHash) or changedLocally
				continue
			# Translators: OneDrive synchronization phase while the cloud index is committed.
			self._updateProgress(_("Saving the OneDrive synchronization index..."), force=True)
			manifestEtag = remote.etag
			try:
				# The conditional manifest write is the cleanup barrier: older writers conflict,
				# while newer writers claim reused payloads by changing their file ETags.
				if remote.isMissing or remote.snapshot != _cloudSnapshot(merged) or orphanFiles:
					manifestEtag = graph.putManifest(manifestData, remote)
			except _ManifestConflictError:
				continue
			self._ensureActive()
			# Translators: OneDrive synchronization phase while merged data is saved on this device.
			self._updateProgress(_("Saving synchronized data on this device..."), force=True)
			changedLocally = (
				self._storage.applyOneDriveMerge(
					merged,
					local,
					stage.load,
				)
				or changedLocally
			)
			self._cleanupOrphanFiles(graph, orphanFiles)
			return _SyncResult(
				uploaded=len(uploadedHashes),
				downloaded=len(downloadedHashes),
				changedLocally=changedLocally,
				manifestEtag=manifestEtag,
			)
		# Translators: Error after repeated local or cloud changes prevent a stable synchronization.
		raise OneDriveError(_("Clipboard or OneDrive data kept changing; try again"))

	def _readAndMergeSnapshots(
		self,
		token: _AccountToken,
		graph: _GraphClient,
	) -> (
		tuple[
			OneDriveSyncSnapshot,
			_RemoteManifest,
			OneDriveSyncSnapshot,
			dict[bytes, _RemoteItemFile],
			tuple[tuple[bytes, str], ...],
		]
		| None
	):
		"""Read remote state, list payload files, and merge snapshots for one attempt."""
		local = self._storage.getOneDriveSnapshot()
		if local.accountId != token.accountId:
			raise OneDriveAccountMismatchError(local.accountId, token.accountId)
		try:
			remote = graph.getManifest()
		except _ManifestConflictError:
			return None
		try:
			self._storage.reserveOneDriveVersions(local, _maximumClock(remote.snapshot), 0)
		except OneDriveLocalChangeError:
			return None
		possiblePayloads = _livePayloadHashes(local) | _livePayloadHashes(remote.snapshot)
		itemFiles, orphanFiles = graph.listItemFiles(possiblePayloads)
		self._ensureActive()
		if remote.snapshot.generation > local.generation:
			rebaseStates = _generationRebaseStates(local, remote.snapshot)
			rebaseCount = sum(len(states) for states in rebaseStates)
			minimumClock = _maximumClock(remote.snapshot)
			if minimumClock + rebaseCount >= _MAX_SYNC_CLOCK:
				raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
			try:
				versions = self._storage.reserveOneDriveVersions(local, minimumClock, rebaseCount)
			except OneDriveLocalChangeError:
				return None
			rebasedLocal = _rebaseLocalSnapshot(
				local,
				remote.snapshot,
				rebaseStates,
				versions,
			)
			merged = _mergeSnapshots(rebasedLocal, remote.snapshot)
		elif local.generation > remote.snapshot.generation:
			merged = _cleanSnapshot(local)
		else:
			merged = _mergeSnapshots(local, remote.snapshot)
		return local, remote, merged, itemFiles, orphanFiles

	def _downloadMissingPayloads(
		self,
		graph: _GraphClient,
		stage: _ItemStage,
		remoteSnapshot: OneDriveSyncSnapshot,
		merged: OneDriveSyncSnapshot,
	) -> tuple[dict[bytes, int], set[bytes]] | None:
		"""Stage missing remote payloads and return image byte counts for one attempt."""
		remotePayloads = _livePayloadHashes(remoteSnapshot)
		imageBytes: dict[bytes, int] = {}
		pendingDownloads: list[bytes] = []
		for payloadHash in sorted(_livePayloadHashes(merged)):
			self._ensureActive()
			imageByteCount = self._storage.getOneDriveItemImageByteCount(payloadHash)
			if imageByteCount is None:
				if payloadHash not in remotePayloads:
					return None
				item = stage.load(payloadHash)
				if item is None:
					pendingDownloads.append(payloadHash)
					continue
				imageByteCount = len(item.imageData or b"")
				del item
			imageBytes[payloadHash] = imageByteCount
		downloadedHashes: set[bytes] = set()
		if pendingDownloads:
			# Translators: OneDrive download progress. {current} and {total} are item counts.
			downloadMessage = _("Downloading OneDrive items ({current}/{total})...")
			self._updateProgress(downloadMessage.format(current=0, total=len(pendingDownloads)), force=True)
			for current, payloadHash in enumerate(pendingDownloads, start=1):
				self._ensureActive()
				data = graph.getItemData(payloadHash)
				item = _decodeItem(data, payloadHash)
				stage.store(payloadHash, data)
				downloadedHashes.add(payloadHash)
				imageBytes[payloadHash] = len(item.imageData or b"")
				del data, item
				self._updateProgress(
					downloadMessage.format(current=current, total=len(pendingDownloads)),
					force=current == len(pendingDownloads),
				)
		return imageBytes, downloadedHashes

	def _normalizeAndEncodeManifest(
		self,
		local: OneDriveSyncSnapshot,
		merged: OneDriveSyncSnapshot,
		imageBytes: Mapping[bytes, int],
	) -> tuple[OneDriveSyncSnapshot, bytes] | None:
		"""Normalize, compact when necessary, and encode one manifest."""
		historyKeys, categoryItemKeys = _normalizationTargets(
			merged,
			imageBytes,
			self._storage.getOneDriveLocalOnlyCategoryImageByteCounts(),
		)
		tombstoneCount = len(historyKeys) + len(categoryItemKeys)
		minimumClock = _maximumClock(merged)
		if minimumClock + tombstoneCount >= _MAX_SYNC_CLOCK:
			raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
		try:
			versions = self._storage.reserveOneDriveVersions(local, minimumClock, tombstoneCount)
		except OneDriveLocalChangeError:
			return None
		merged = _applyNormalization(merged, historyKeys, categoryItemKeys, versions)
		if _tombstoneCount(merged) >= _MAX_TOMBSTONES_BEFORE_COMPACTION:
			try:
				merged = self._compactSnapshotForSync(local, merged)
			except OneDriveLocalChangeError:
				return None
		try:
			manifestData = _encodeManifest(merged)
		except _ManifestSizeLimitError:
			if not _tombstoneCount(merged):
				raise
			try:
				merged = self._compactSnapshotForSync(local, merged)
			except OneDriveLocalChangeError:
				return None
			manifestData = _encodeManifest(merged)
		return merged, manifestData

	def _compactSnapshotForSync(
		self,
		local: OneDriveSyncSnapshot,
		merged: OneDriveSyncSnapshot,
	) -> OneDriveSyncSnapshot:
		"""Compact a manifest after reserving one successor generation."""
		if _maximumClock(merged) >= _MAX_SYNC_CLOCK:
			raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
		generation = self._storage.reserveOneDriveVersions(
			local,
			_maximumClock(merged),
			1,
		)[0]
		return _compactSnapshot(merged, generation)

	def _uploadOrReusePayloads(
		self,
		graph: _GraphClient,
		stage: _ItemStage,
		remoteSnapshot: OneDriveSyncSnapshot,
		merged: OneDriveSyncSnapshot,
		itemFiles: dict[bytes, _RemoteItemFile],
	) -> tuple[set[bytes], tuple[bytes, ...], bool]:
		"""Upload live payloads or reuse matching remote files for one attempt."""
		uploadedHashes: set[bytes] = set()
		oversizedPayloads: list[bytes] = []
		livePayloads = _livePayloadHashes(merged)
		remotePayloads = _livePayloadHashes(remoteSnapshot)
		uploadTotal = len(livePayloads)
		if uploadTotal:
			# Translators: Progress while existing files are verified and missing files are uploaded.
			uploadMessage = _("Ensuring OneDrive items are uploaded ({current}/{total})...")
			self._updateProgress(uploadMessage.format(current=0, total=uploadTotal), force=True)
		for current, payloadHash in enumerate(sorted(livePayloads), start=1):
			self._ensureActive()
			item = self._storage.getOneDriveItem(payloadHash)
			if item is None:
				itemData = stage.read(payloadHash)
				if itemData is None:
					return uploadedHashes, tuple(oversizedPayloads), True
			else:
				try:
					itemData = _encodeItem(item)
				except _ItemSizeLimitError:
					oversizedPayloads.append(payloadHash)
					item = None
					continue
			itemFile = itemFiles.get(payloadHash)
			if itemFile is None or not _canReuseRemoteItem(
				itemFile,
				itemData,
				payloadHash in remotePayloads,
			):
				graph.putItemData(payloadHash, itemData)
				uploadedHashes.add(payloadHash)
				itemFiles[payloadHash] = _RemoteItemFile(
					size=len(itemData),
					sha1Hash=sha1(itemData).hexdigest(),
				)
			self._updateProgress(
				uploadMessage.format(current=current, total=uploadTotal),
				force=current == uploadTotal,
			)
			del itemData
			item = None
		return uploadedHashes, tuple(oversizedPayloads), False

	def _cleanupOrphanFiles(
		self,
		graph: _GraphClient,
		orphanFiles: tuple[tuple[bytes, str], ...],
	) -> None:
		"""Best-effort delete unreferenced OneDrive payload files."""
		if not orphanFiles:
			return
		# Translators: Progress while old unreferenced OneDrive payload files are removed.
		cleanupMessage = _("Cleaning up old OneDrive items ({current}/{total})...")
		cleanupTotal = len(orphanFiles)
		self._updateProgress(cleanupMessage.format(current=0, total=cleanupTotal), force=True)
		try:
			for current, (payloadHash, etag) in enumerate(orphanFiles, start=1):
				graph.deleteItemData(payloadHash, etag)
				self._updateProgress(
					cleanupMessage.format(current=current, total=cleanupTotal),
					force=current == cleanupTotal,
				)
		except _OperationCancelledError:
			pass
		except OneDriveError as error:
			log.debugWarning(
				f"Could not clean up old OneDrive item files; HTTP status={error.statusCode}.",
			)

	def _startOperation(
		self,
		work: Callable[[], tuple[str, bool]],
		*,
		name: str,
		busyMessage: str,
		failureMessage: str,
		onDone: _CompletionCallback | None,
		isSigningOut: bool = False,
		requiresLogin: bool = False,
	) -> bool:
		"""Start one serialized daemon operation and update busy state."""
		with self._lock:
			if self._isTerminated:
				return False
			if not self._isInitialized:
				# Translators: Error when OneDrive is used before its service starts.
				rejectionMessage = _("OneDrive synchronization is not initialized")
			elif not self._state.isAvailable or self._state.isSigningOut and not isSigningOut:
				rejectionMessage = self._state.statusMessage
			elif requiresLogin and not self._state.isLoggedIn:
				rejectionMessage = _ERROR_SIGN_IN_REQUIRED
			elif self._state.isBusy:
				# Translators: Error when another OneDrive action is already running.
				rejectionMessage = _("Another OneDrive operation is already in progress")
			else:
				rejectionMessage = ""
				self._operationCancelEvent.clear()
				self._lastProgressUpdateAt = 0.0
				self._state = replace(
					self._state,
					isBusy=True,
					isSigningOut=isSigningOut,
					statusMessage=busyMessage,
				)
				worker = Thread(
					target=self._operationWorker,
					args=(work, failureMessage, onDone),
					name=name,
					daemon=True,
				)
		if rejectionMessage:
			self._postCompletion(onDone, False, rejectionMessage)
			return False
		self._postStateChanged()
		try:
			worker.start()
		except RuntimeError:
			log.exception("Could not start a OneDrive worker thread.")
			self._finishOperation(False, failureMessage, False, onDone)
			return False
		return True

	def _operationWorker(
		self,
		work: Callable[[], tuple[str, bool]],
		failureMessage: str,
		onDone: _CompletionCallback | None,
	) -> None:
		"""Run one operation and translate all failures before returning to wx."""
		try:
			message, dataChanged = work()
		except _OperationCancelledError as error:
			self._finishOperation(False, str(error), False, onDone)
			return
		except Exception as error:
			message = self._formatFailure(error, failureMessage)
			if isinstance(error, _AuthenticationUnavailableError):
				log.exception("Bundled Microsoft authentication support could not be loaded.", exc_info=error)
			elif isinstance(error, OneDriveError):
				log.debugWarning(
					f"OneDrive operation failed: type={type(error).__name__}, HTTP status={error.statusCode}.",
				)
			elif isinstance(error, StorageError):
				log.debugWarning(f"OneDrive operation failed: type={type(error).__name__}.")
			else:
				log.exception(
					"Unexpected OneDrive operation failure.",
					exc_info=error,
				)
			self._finishOperation(False, message, False, onDone)
			return
		self._finishOperation(True, message, dataChanged, onDone)

	def _finishOperation(
		self,
		success: bool,
		message: str,
		dataChanged: bool,
		onDone: _CompletionCallback | None,
	) -> None:
		"""Clear busy state, schedule follow-up work, and deliver GUI callbacks."""
		with self._lock:
			if self._isTerminated:
				return
			shouldSignOut = self._signOutPending
			if shouldSignOut:
				signOutDone = self._pendingSignOutDone
				self._signOutPending = False
				self._pendingSignOutDone = None
				self._hasPendingSync = False
				self._state = replace(self._state, isBusy=False)
				shouldRunPending = False
			else:
				signOutDone = None
				self._state = replace(
					self._state,
					isBusy=False,
					isSigningOut=False,
					statusMessage=message,
				)
				isLoggedIn = self._state.isLoggedIn
				shouldRunPending = self._hasPendingSync and isLoggedIn
				self._hasPendingSync = False
				if isLoggedIn and self._pollTimer is None:
					self._startPollTimerLocked()
		if shouldSignOut:
			if dataChanged:
				self._postOperationResult(None, success, message, True, False)
			self._startSignOut(signOutDone)
			return
		self._postOperationResult(onDone, success, message, dataChanged, shouldRunPending)

	def _formatFailure(self, error: Exception, fallback: str) -> str:
		"""Return a localized, token-free failure description."""
		if isinstance(error, OneDriveError):
			return str(error)
		if isinstance(error, OneDriveAccountMismatchError):
			# Translators: Error shown when this clipboard database is linked to another account.
			return _("This clipboard database is linked to another Microsoft account")
		if isinstance(error, ImageStorageLimitError):
			# Translators: Error when downloaded OneDrive images cannot fit in local storage.
			return _("OneDrive image data cannot fit in the 256 MB local storage limit")
		if isinstance(error, StorageFormatError):
			return _INVALID_SYNC_DATA_MESSAGE
		if isinstance(error, StorageError):
			# Translators: Error when synchronized data cannot be saved locally.
			return _("Could not update local clipboard storage")
		return fallback

	def _getAuth(self) -> _AuthManager:
		"""Create the single MSAL manager lazily on a worker thread."""
		with self._lock:
			auth = self._auth
		if auth is not None:
			return auth
		try:
			newAuth = _AuthManager(self._cachePath)
		except _AuthenticationUnavailableError:
			with self._lock:
				self._state = replace(
					self._state,
					isAvailable=False,
					statusMessage=_ERROR_AUTH_UNAVAILABLE,
				)
			raise
		with self._lock:
			if self._isTerminated:
				raise _OperationCancelledError("")
			if self._auth is None:
				self._auth = newAuth
			return self._auth

	def _getAccountToken(self) -> _AccountToken | None:
		"""Return the current runtime account token under the state lock."""
		with self._lock:
			return self._accountToken

	def _setAccountToken(self, token: _AccountToken | None) -> None:
		"""Atomically update runtime account identity and public login state."""
		with self._lock:
			if token is None or self._accountToken is None or self._accountToken.accountId != token.accountId:
				self._lastManifestEtag = None
				self._lastFullSyncAt = None
			self._accountToken = token
			self._state = replace(
				self._state,
				isLoggedIn=token is not None,
				accountName="" if token is None else token.accountName,
			)

	def _ensureActive(self) -> None:
		"""Abort work before it touches local state after manager termination."""
		with self._lock:
			if self._isTerminated or self._operationCancelEvent.is_set():
				raise _OperationCancelledError("")

	def _updateProgress(self, message: str, *, force: bool = False) -> None:
		"""Publish one throttled synchronization phase or item count."""
		now = monotonic()
		with self._lock:
			if (
				self._isTerminated
				or not self._state.isBusy
				or self._state.isSigningOut
				or self._operationCancelEvent.is_set()
				or message == self._state.statusMessage
			):
				return
			if not force and now - self._lastProgressUpdateAt < _PROGRESS_UPDATE_SECONDS:
				return
			self._lastProgressUpdateAt = now
			self._state = replace(self._state, statusMessage=message)
		self._postStateChanged()

	def _debounceTimerFired(self, generation: int) -> None:
		"""Consume the local-change timer and request automatic synchronization."""
		with self._lock:
			if generation != self._debounceGeneration:
				return
			self._debounceTimer = None
		self._requestSync(onDone=None, isAutomatic=True)

	def _pollTimerFired(self) -> None:
		"""Schedule the next poll and request one automatic synchronization."""
		with self._lock:
			self._pollTimer = None
			if self._isTerminated or not self._state.isLoggedIn:
				return
			self._startPollTimerLocked()
		self._requestSync(onDone=None, isAutomatic=True, isPoll=True)

	def _startPollTimerLocked(self) -> None:
		"""Start the five-minute polling timer while the state lock is held."""
		timer = Timer(_SYNC_POLL_SECONDS, self._pollTimerFired)
		timer.daemon = True
		self._pollTimer = timer
		try:
			timer.start()
		except RuntimeError:
			self._pollTimer = None
			log.exception("Could not start the OneDrive polling timer.")

	def _cancelTimersLocked(self) -> None:
		"""Cancel synchronization timers while the state lock is held."""
		if self._debounceTimer is not None:
			self._debounceTimer.cancel()
			self._debounceTimer = None
		self._debounceGeneration += 1
		if self._pollTimer is not None:
			self._pollTimer.cancel()
			self._pollTimer = None

	def _postStateChanged(self) -> None:
		"""Schedule a state callback on wx's GUI thread."""
		try:
			wx.CallAfter(self._deliverStateChanged)
		except RuntimeError:
			with self._lock:
				isTerminated = self._isTerminated
			if not isTerminated:
				log.debugWarning("Could not schedule a OneDrive state callback.", exc_info=True)

	def _postDeviceCode(
		self,
		callback: _DeviceCodeCallback,
		userCode: str,
		verificationUrl: str,
	) -> None:
		"""Schedule device-code presentation on wx's GUI thread."""
		try:
			wx.CallAfter(self._deliverDeviceCode, callback, userCode, verificationUrl)
		except RuntimeError:
			with self._lock:
				isTerminated = self._isTerminated
			if not isTerminated:
				log.debugWarning("Could not schedule the Microsoft device code.", exc_info=True)

	def _postCompletion(
		self,
		onDone: _CompletionCallback | None,
		success: bool,
		message: str,
	) -> None:
		"""Schedule a rejected operation callback on wx's GUI thread."""
		if onDone is None:
			return
		try:
			wx.CallAfter(self._deliverCompletion, onDone, success, message)
		except RuntimeError:
			with self._lock:
				isTerminated = self._isTerminated
			if not isTerminated:
				log.debugWarning("Could not schedule a OneDrive completion callback.", exc_info=True)

	def _postOperationResult(
		self,
		onDone: _CompletionCallback | None,
		success: bool,
		message: str,
		dataChanged: bool,
		shouldRunPending: bool,
	) -> None:
		"""Schedule ordered state, data, and completion callbacks on wx."""
		try:
			wx.CallAfter(
				self._deliverOperationResult,
				onDone,
				success,
				message,
				dataChanged,
				shouldRunPending,
			)
		except RuntimeError:
			with self._lock:
				isTerminated = self._isTerminated
				if shouldRunPending and not isTerminated and self._state.isLoggedIn:
					self._hasPendingSync = True
			if not isTerminated:
				log.debugWarning("Could not schedule OneDrive result callbacks.", exc_info=True)

	def _deliverStateChanged(self) -> None:
		"""Invoke the current state callback on the GUI thread."""
		with self._lock:
			if self._isTerminated:
				return
			callback = self._onStateChanged
		if callback is not None:
			try:
				callback()
			except Exception:
				log.exception("OneDrive state callback failed.")

	def _deliverDeviceCode(
		self,
		callback: _DeviceCodeCallback,
		userCode: str,
		verificationUrl: str,
	) -> None:
		"""Invoke a device-code callback only while the manager is active."""
		with self._lock:
			if self._isTerminated or self._deviceCodeCancellationRequested:
				return
		try:
			callback(userCode, verificationUrl)
		except Exception:
			log.exception("OneDrive device-code callback failed.")

	def _deliverCompletion(
		self,
		callback: _CompletionCallback,
		success: bool,
		message: str,
	) -> None:
		"""Invoke a completion callback only while the manager is active."""
		with self._lock:
			if self._isTerminated:
				return
		try:
			callback(success, message)
		except Exception:
			log.exception("OneDrive completion callback failed.")

	def _deliverOperationResult(
		self,
		onDone: _CompletionCallback | None,
		success: bool,
		message: str,
		dataChanged: bool,
		shouldRunPending: bool,
	) -> None:
		"""Deliver final callbacks in a stable order on the GUI thread."""
		with self._lock:
			if self._isTerminated:
				return
			onStateChanged = self._onStateChanged
			onDataChanged = self._onDataChanged if dataChanged else None
		try:
			if onStateChanged is not None:
				try:
					onStateChanged()
				except Exception:
					log.exception("OneDrive state callback failed.")
			if onDataChanged is not None:
				try:
					onDataChanged()
				except Exception:
					log.exception("OneDrive data callback failed.")
			if onDone is not None:
				try:
					onDone(success, message)
				except Exception:
					log.exception("OneDrive completion callback failed.")
		finally:
			if shouldRunPending:
				self._requestSync(onDone=None, isAutomatic=True)
