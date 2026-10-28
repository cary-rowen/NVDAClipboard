# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.


"""Access the OneDrive application folder through Microsoft Graph."""

from __future__ import annotations

from base64 import b64decode
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha1
import json
import re
from threading import Event
from typing import Any, cast
from urllib.parse import quote

import addonHandler
import requests

from .oneDriveProtocol import (
	MAX_ITEM_FILE_BYTES,
	MAX_MANIFEST_BYTES,
	OneDriveError,
	INVALID_SYNC_DATA_MESSAGE,
	_decodeManifest,
	_emptySnapshot,
)
from .storageModels import OneDriveSyncSnapshot


addonHandler.initTranslation()

MANIFEST_FILE_NAME = "nvdaClipboard.sync.manifest.json"
ITEM_FILE_PREFIX = "nvdaClipboard.sync.item."
ITEM_FILE_SUFFIX = ".json"
HTTP_TIMEOUT = (10, 30)
_MAX_RETRY_AFTER_SECONDS = 30
_MAX_ORPHAN_DELETIONS_PER_SYNC = 100
_QUICK_XOR_HASH_BYTES = 20
_QUICK_XOR_SHIFT = 11
_QUICK_XOR_BLOCK_BYTES = _QUICK_XOR_HASH_BYTES * 8 * _QUICK_XOR_SHIFT
_ITEM_FILE_RE = re.compile(
	rf"^{re.escape(ITEM_FILE_PREFIX)}[0-9a-f]{{64}}{re.escape(ITEM_FILE_SUFFIX)}$",
)

# Translators: Error shown when a downloaded OneDrive manifest or item file is too large.
_ERROR_FILE_TOO_LARGE = _("A OneDrive synchronization file exceeds the supported size")
# Translators: Error shown when OneDrive returns malformed app-folder file-list data.
_ERROR_INVALID_FILE_LIST = _("OneDrive returned an invalid file list")
# Translators: Error used when another device changes the OneDrive manifest during synchronization.
_ERROR_MANIFEST_CHANGED = _("The OneDrive synchronization data changed on another device")
# Translators: Error shown when Microsoft sign-in can no longer authorize OneDrive.
ERROR_SIGN_IN_EXPIRED = _("Sign in expired")


class ManifestConflictError(OneDriveError):
	"""Report that another client changed the OneDrive manifest."""


class OperationCancelledError(OneDriveError):
	"""Report an expected user cancellation without logging it as a failure."""


@dataclass(frozen=True, slots=True)
class RemoteManifest:
	"""Pair a decoded cloud snapshot with the current file ETag."""

	snapshot: OneDriveSyncSnapshot
	etag: str | None
	isMissing: bool


@dataclass(frozen=True, slots=True)
class RemoteItemFile:
	"""Describe one content-addressed file listed in the OneDrive app folder."""

	size: int | None
	sha1Hash: str | None
	quickXorHash: bytes | None = None


class GraphClient:
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

	def getManifest(self) -> RemoteManifest:
		"""Download the versioned sync manifest or return an empty cloud state."""
		etag = self._getEtag(MANIFEST_FILE_NAME)
		if etag is None:
			return RemoteManifest(_emptySnapshot(), None, True)
		response = self._request(
			"GET",
			self._contentUrl(MANIFEST_FILE_NAME),
			expected=(200, 404),
			stream=True,
		)
		if response.status_code == 404:
			response.close()
			raise ManifestConflictError(
				_ERROR_MANIFEST_CHANGED,
				statusCode=412,
			)
		data = self._readBoundedResponse(response, MAX_MANIFEST_BYTES)
		return RemoteManifest(_decodeManifest(data), etag, False)

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

	def putManifest(self, data: bytes, remote: RemoteManifest) -> str:
		"""Conditionally replace or create the synchronization manifest."""
		headers = {"Content-Type": "application/json; charset=utf-8"}
		if remote.etag:
			headers["If-Match"] = remote.etag
		elif remote.isMissing:
			headers["If-None-Match"] = "*"
		else:
			raise OneDriveError(INVALID_SYNC_DATA_MESSAGE)
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
				raise ManifestConflictError(str(error), statusCode=412) from error
			raise
		try:
			payload = json.loads(self._readBoundedResponse(response, MAX_MANIFEST_BYTES))
		except (UnicodeDecodeError, json.JSONDecodeError) as error:
			raise OneDriveError(INVALID_SYNC_DATA_MESSAGE) from error
		etag = payload.get("eTag") if isinstance(payload, Mapping) else None
		if not isinstance(etag, str) or not etag or "\r" in etag or "\n" in etag:
			raise OneDriveError(INVALID_SYNC_DATA_MESSAGE)
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
	) -> tuple[dict[bytes, RemoteItemFile], tuple[tuple[bytes, str], ...]]:
		"""List relevant item metadata and a bounded batch of unreferenced files."""
		url = f"{self._ROOT}/children?$select=name,size,file,eTag&$top=1000"
		result: dict[bytes, RemoteItemFile] = {}
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
				itemFile = RemoteItemFile(
					size=size,
					sha1Hash=sha1Value.lower() if isinstance(sha1Value, str) else None,
					quickXorHash=_quickXorHashFromGraph(quickXorValue),
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
			raise OneDriveError(INVALID_SYNC_DATA_MESSAGE) from error
		finally:
			response.close()
		etag = payload.get("eTag") if isinstance(payload, Mapping) else None
		if not isinstance(etag, str) or not etag or "\r" in etag or "\n" in etag:
			raise OneDriveError(INVALID_SYNC_DATA_MESSAGE)
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
		retried = False
		while True:
			self._checkCancelled()
			try:
				response = self.session.request(
					method,
					url,
					headers=requestHeaders,
					timeout=HTTP_TIMEOUT,
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
			if not retried and response.status_code in (429, 503) and error.retryAfter is not None:
				retried = True
				# ponytail: retry once with a bounded delay; add cancellable backoff if throttling is common.
				if self.cancelEvent.wait(min(error.retryAfter, _MAX_RETRY_AFTER_SECONDS)):
					self._checkCancelled()
				continue
			raise error

	def _checkCancelled(self) -> None:
		"""Abort a Graph operation after the user requests sign-out."""
		if self.cancelEvent.is_set():
			raise OperationCancelledError("")

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
		message = ERROR_SIGN_IN_EXPIRED
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


def remoteItemMatches(itemFile: RemoteItemFile, data: bytes) -> bool:
	"""Return whether listed size and an available content hash verify canonical item data."""
	if itemFile.size != len(data):
		return False
	if itemFile.sha1Hash is not None:
		return itemFile.sha1Hash == sha1(data).hexdigest()
	return itemFile.quickXorHash is not None and itemFile.quickXorHash == _quickXorHash(data)
