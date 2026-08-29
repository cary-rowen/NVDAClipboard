# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Wrap the native ClipDataCloud C API with validated Python operations."""

from __future__ import annotations

from ctypes import Array, CDLL, POINTER, byref, c_char, c_char_p, c_int, c_void_p, create_string_buffer
from dataclasses import dataclass
import platform
from threading import Lock

from ._nativeDeps import getNativeFilePath


_SDK_DLL_NAME_BY_MACHINE = {
	"ARM64": "ClipDataCloud.SDK.ARM.dll",
}
_DEFAULT_SDK_DLL_NAME = "ClipDataCloud.SDK.dll"
SUCCESS = 0
INVALID_ARGUMENT = 1001
BUFFER_TOO_SMALL = 1008
USER_NOT_LOGGED_IN = 1009
MAX_TEXT_BYTES = 1024 * 1024
ERROR_BUFFER_SIZE = 2048
TOKEN_BUFFER_SIZE = 4096
NICKNAME_BUFFER_SIZE = 4096
FETCH_BUFFER_SIZE = MAX_TEXT_BYTES + 1
C_STRING_CONTAINS_NUL_ERROR = "C string argument contains NUL character."
TEXT_CONTAINS_NUL_ERROR = "Text contains NUL character."
TEXT_EMPTY_ERROR = "Text must not be empty."
TEXT_EXCEEDS_MAX_BYTES_ERROR = "Text exceeds 1 MB."
TEXT_INVALID_UTF8_ERROR = "Text is not valid UTF-8."


@dataclass(frozen=True)
class CloudLoginResult:
	"""Result returned by a successful ClipDataCloud login."""

	nickname: str
	isVip: bool


class CloudClipboardError(Exception):
	"""Error raised when the ClipDataCloud SDK reports a failed operation."""

	def __init__(self, code: int | None, message: str) -> None:
		super().__init__(message)
		self.code = code
		self.message = message


def validateUploadText(text: str) -> bytes:
	"""Return UTF-8 bytes for upload after validating C API text constraints."""
	if not text:
		raise CloudClipboardError(INVALID_ARGUMENT, TEXT_EMPTY_ERROR)
	if "\0" in text:
		raise CloudClipboardError(INVALID_ARGUMENT, TEXT_CONTAINS_NUL_ERROR)
	textBytes = _encodeUtf8(text)
	if len(textBytes) > MAX_TEXT_BYTES:
		raise CloudClipboardError(INVALID_ARGUMENT, TEXT_EXCEEDS_MAX_BYTES_ERROR)
	return textBytes


def _encodeUtf8(text: str) -> bytes:
	"""Encode text for the C API or raise a stable cloud validation error."""
	try:
		return text.encode("utf-8")
	except UnicodeEncodeError as error:
		raise CloudClipboardError(INVALID_ARGUMENT, TEXT_INVALID_UTF8_ERROR) from error


def _encodeCStringArgument(text: str) -> bytes:
	"""Return UTF-8 bytes for a NUL-terminated C API input string."""
	if "\0" in text:
		raise CloudClipboardError(INVALID_ARGUMENT, C_STRING_CONTAINS_NUL_ERROR)
	return _encodeUtf8(text)


class CloudClipboardSdk:
	"""Thin ctypes wrapper around the ClipDataCloud native SDK exports."""

	def __init__(self) -> None:
		dllPath = getNativeFilePath(_getSdkDllName())
		if not dllPath.exists():
			raise CloudClipboardError(None, f"SDK DLL not found: {dllPath}") from None
		try:
			self._sdk = CDLL(str(dllPath))
		except OSError as error:
			raise CloudClipboardError(None, f"SDK DLL load failed: {dllPath}: {error}") from error
		try:
			self._configureFunctions()
		except AttributeError as error:
			raise CloudClipboardError(None, f"SDK DLL missing required export: {error}") from error
		self._lock = Lock()

	def _configureFunctions(self) -> None:
		self._sdk.Login.argtypes = [
			c_char_p,
			c_char_p,
			c_void_p,
			c_int,
			c_void_p,
			c_int,
			POINTER(c_int),
			c_void_p,
			c_int,
		]
		self._sdk.Login.restype = c_int
		self._sdk.UploadText.argtypes = [c_char_p, c_char_p, c_void_p, c_int]
		self._sdk.UploadText.restype = c_int
		self._sdk.FetchText.argtypes = [c_char_p, c_void_p, c_int, c_void_p, c_int]
		self._sdk.FetchText.restype = c_int
		self._sdk.Logout.argtypes = [c_char_p, c_void_p, c_int]
		self._sdk.Logout.restype = c_int
		self._sdk.IsUserLoggedIn.argtypes = []
		self._sdk.IsUserLoggedIn.restype = c_int

	def login(self, phone: str, password: str) -> CloudLoginResult:
		"""Log in with a phone number and password."""
		errorBuffer = create_string_buffer(ERROR_BUFFER_SIZE)
		tokenBuffer = create_string_buffer(TOKEN_BUFFER_SIZE)
		nicknameBuffer = create_string_buffer(NICKNAME_BUFFER_SIZE)
		svip = c_int(0)
		phoneBytes = _encodeCStringArgument(phone)
		passwordBytes = _encodeCStringArgument(password)
		with self._lock:
			code = self._sdk.Login(
				phoneBytes,
				passwordBytes,
				tokenBuffer,
				len(tokenBuffer),
				nicknameBuffer,
				len(nicknameBuffer),
				byref(svip),
				errorBuffer,
				len(errorBuffer),
			)
		self._raiseForError(code, errorBuffer)
		return CloudLoginResult(
			nickname=nicknameBuffer.value.decode("utf-8", errors="replace"),
			isVip=svip.value > 0,
		)

	def uploadText(self, text: str) -> None:
		"""Upload text to the cloud clipboard."""
		textBytes = validateUploadText(text)
		errorBuffer = create_string_buffer(ERROR_BUFFER_SIZE)
		with self._lock:
			code = self._sdk.UploadText(None, textBytes, errorBuffer, len(errorBuffer))
		self._raiseForError(code, errorBuffer)

	def fetchText(self) -> str:
		"""Fetch the latest cloud clipboard text."""
		errorBuffer = create_string_buffer(ERROR_BUFFER_SIZE)
		textBuffer = create_string_buffer(FETCH_BUFFER_SIZE)
		with self._lock:
			code = self._sdk.FetchText(None, textBuffer, len(textBuffer), errorBuffer, len(errorBuffer))
		self._raiseForError(code, errorBuffer)
		return textBuffer.value.decode("utf-8", errors="replace")

	def logout(self) -> None:
		"""Log out of the persisted SDK session."""
		errorBuffer = create_string_buffer(ERROR_BUFFER_SIZE)
		with self._lock:
			code = self._sdk.Logout(None, errorBuffer, len(errorBuffer))
		self._raiseForError(code, errorBuffer)

	def isUserLoggedIn(self) -> bool:
		"""Return whether the SDK currently has a persisted login state."""
		with self._lock:
			return self._sdk.IsUserLoggedIn() == 1

	def _raiseForError(self, code: int, errorBuffer: Array[c_char]) -> None:
		if code == SUCCESS:
			return
		message = errorBuffer.value.decode("utf-8", errors="replace")
		raise CloudClipboardError(code, message)


def _getSdkDllName() -> str:
	"""Return the SDK DLL name for the current Windows CPU architecture."""
	return _SDK_DLL_NAME_BY_MACHINE.get(platform.machine().upper(), _DEFAULT_SDK_DLL_NAME)
