# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Monitor and restore the fixed clipboard formats supported by the add-on."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
from ctypes import WinError, create_unicode_buffer, memmove, string_at, windll
import ctypes.wintypes as w
from dataclasses import dataclass, replace
from enum import Enum
import struct
from threading import Lock, Thread
from time import sleep

from logHandler import log
from winBindings import gdi32, kernel32, user32
from windowUtils import CustomWindow
import wx

from .clipboardData import (
	MAX_DECODED_IMAGE_BYTES,
	MAX_IMAGE_BYTES,
	MAX_RICH_FORMAT_BYTES,
	MAX_TEXT_BYTES,
	getPngImageInfo,
	isImageSizeSafe,
)
from .imageCodec import isPngImageDecodable, pngToPackedDib

__all__ = [
	"ClipboardContentType",
	"ClipboardMonitor",
	"ClipboardSequenceChangedError",
	"ClipboardSnapshot",
	"ClipboardSnapshotCallback",
	"ClipboardWriteError",
]


class ClipboardContentType(Enum):
	"""Identify the clipboard content categories understood by the add-on."""

	TEXT = "text"
	FORMATTED_TEXT = "formattedText"
	TEXT_AND_IMAGE = "textAndImage"
	FILES = "files"
	IMAGE = "image"
	EMPTY = "empty"
	PROTECTED = "protected"
	UNSUPPORTED = "unsupported"
	ERROR = "error"


@dataclass(frozen=True, slots=True)
class ClipboardSnapshot:
	"""Contain one immutable read of the supported Windows clipboard formats.

	PNG data has passed structural validation and its image metadata matches the payload.
	Representations which cannot be retained may be omitted while the remaining fields stay writable.
	"""

	contentType: ClipboardContentType
	sequenceNumber: int = 0
	text: str = ""
	html: bytes | None = None
	rtf: bytes | None = None
	imageFormat: str | None = None
	imageData: bytes | None = None
	imageWidth: int = 0
	imageHeight: int = 0
	imageBitDepth: int = 0
	files: tuple[str, ...] = ()
	preferredDropEffect: int | None = None
	canIncludeInHistory: bool = True
	canUpload: bool = True
	richFormatsDropped: bool = False
	error: str = ""


ClipboardSnapshotCallback = Callable[[ClipboardSnapshot], None]

CF_BITMAP = 0x2
CF_DIB = 0x8
CF_UNICODETEXT = 0xD
CF_HDROP = 0xF
CF_DIBV5 = 0x11
WM_CLIPBOARDUPDATE = 0x031D
HWND_MESSAGE = -3
DRAG_QUERY_FILE_COUNT = 0xFFFFFFFF

DEFAULT_OPEN_RETRIES = 10
DEFAULT_OPEN_RETRY_INTERVAL = 0.05
_FAILED_OPEN_RETRY_INTERVAL = 0.1
_OPEN_CLIPBOARD_ERROR = "OpenClipboard failed"

_MAX_FILE_COUNT = 10_000
_MAX_FILE_PATH_CHARACTERS = 1_000_000
_SUPPORTED_DIB_HEADER_SIZES = frozenset((12, 40, 52, 56, 108, 124))
_SUPPORTED_DIB_BIT_DEPTHS = frozenset((1, 4, 8, 16, 24, 32))
_BI_RGB = 0
_BI_BITFIELDS = 3
_BI_ALPHABITFIELDS = 6
_DIB_RGB_COLORS = 0
_GMEM_MOVEABLE = 0x0002
_DROPEFFECT_COPY = 1
_DROPEFFECT_MOVE = 2
_DROPEFFECT_LINK = 4
_DROPEFFECT_MASK = _DROPEFFECT_COPY | _DROPEFFECT_MOVE | _DROPEFFECT_LINK
_SEQUENCE_NUMBER_MASK = 0xFFFFFFFF

_HTML_FORMAT_NAME = "HTML Format"
_RTF_FORMAT_NAME = "Rich Text Format"
_PNG_FORMAT_NAME = "PNG"
_PREFERRED_DROP_EFFECT_FORMAT_NAME = "Preferred DropEffect"
_EXCLUDE_MONITOR_FORMAT_NAME = "ExcludeClipboardContentFromMonitorProcessing"
_CAN_INCLUDE_HISTORY_FORMAT_NAME = "CanIncludeInClipboardHistory"
_CAN_UPLOAD_FORMAT_NAME = "CanUploadToCloudClipboard"


def _isNewerClipboardSequence(candidate: int, previous: int) -> bool:
	"""Return whether a nonzero DWORD sequence follows another, including wraparound."""
	difference = (candidate - previous) & _SEQUENCE_NUMBER_MASK
	return 0 < difference < 0x80000000


class _ClipboardDataLimitError(ValueError):
	pass


class ClipboardSequenceChangedError(RuntimeError):
	"""Indicate that the clipboard changed before a local write was confirmed."""


class ClipboardWriteError(RuntimeError):
	"""Indicate that clipboard replacement failed after modifying the clipboard."""

	def __init__(self, sequenceNumber: int) -> None:
		super().__init__(sequenceNumber)
		self.sequenceNumber = sequenceNumber


class _DROPFILES(ctypes.Structure):
	"""Describe the header preceding a CF_HDROP path list."""

	_fields_ = (
		("pFiles", w.DWORD),
		("ptX", w.LONG),
		("ptY", w.LONG),
		("fNC", w.BOOL),
		("fWide", w.BOOL),
	)


_MAX_DROPFILES_BYTES = ctypes.sizeof(_DROPFILES) + 2 * (_MAX_FILE_PATH_CHARACTERS + _MAX_FILE_COUNT + 2)


class _BITMAP(ctypes.Structure):
	"""Describe dimensions and color depth returned for a GDI bitmap handle."""

	_fields_ = (
		("bmType", w.LONG),
		("bmWidth", w.LONG),
		("bmHeight", w.LONG),
		("bmWidthBytes", w.LONG),
		("bmPlanes", w.WORD),
		("bmBitsPixel", w.WORD),
		("bmBits", w.LPVOID),
	)


_user32 = windll.user32
_kernel32 = windll.kernel32
_shell32 = windll.shell32
_gdi32 = windll.gdi32

_addClipboardFormatListener = _user32.AddClipboardFormatListener
_addClipboardFormatListener.argtypes = (w.HWND,)
_addClipboardFormatListener.restype = w.BOOL

_removeClipboardFormatListener = _user32.RemoveClipboardFormatListener
_removeClipboardFormatListener.argtypes = (w.HWND,)
_removeClipboardFormatListener.restype = w.BOOL

_openClipboard = user32.OpenClipboard
_closeClipboard = user32.CloseClipboard
_getClipboardData = user32.GetClipboardData
_setClipboardData = user32.SetClipboardData
_emptyClipboard = user32.EmptyClipboard

_isClipboardFormatAvailable = _user32.IsClipboardFormatAvailable
_isClipboardFormatAvailable.argtypes = (w.UINT,)
_isClipboardFormatAvailable.restype = w.BOOL

_countClipboardFormats = _user32.CountClipboardFormats
_countClipboardFormats.argtypes = ()
_countClipboardFormats.restype = ctypes.c_int

_registerClipboardFormat = _user32.RegisterClipboardFormatW
_registerClipboardFormat.argtypes = (w.LPCWSTR,)
_registerClipboardFormat.restype = w.UINT

_getClipboardSequenceNumber = _user32.GetClipboardSequenceNumber
_getClipboardSequenceNumber.argtypes = ()
_getClipboardSequenceNumber.restype = w.DWORD

_getClipboardOwner = _user32.GetClipboardOwner
_getClipboardOwner.argtypes = ()
_getClipboardOwner.restype = w.HWND

_globalLock = kernel32.GlobalLock
_globalUnlock = kernel32.GlobalUnlock

_globalSize = _kernel32.GlobalSize
_globalSize.argtypes = (w.HGLOBAL,)
_globalSize.restype = ctypes.c_size_t

_globalAlloc = kernel32.GlobalAlloc
_globalFree = kernel32.GlobalFree

_getObject = _gdi32.GetObjectW
_getObject.argtypes = (w.HANDLE, ctypes.c_int, w.LPVOID)
_getObject.restype = ctypes.c_int

_dragQueryFile = _shell32.DragQueryFileW
_dragQueryFile.argtypes = (w.HANDLE, w.UINT, w.LPWSTR, w.UINT)
_dragQueryFile.restype = w.UINT


@dataclass(frozen=True, slots=True)
class _RegisteredFormats:
	html: int
	rtf: int
	png: int
	preferredDropEffect: int
	excludeMonitor: int
	canIncludeHistory: int
	canUpload: int


def _registerFormats() -> _RegisteredFormats:
	return _RegisteredFormats(
		html=int(_registerClipboardFormat(_HTML_FORMAT_NAME)),
		rtf=int(_registerClipboardFormat(_RTF_FORMAT_NAME)),
		png=int(_registerClipboardFormat(_PNG_FORMAT_NAME)),
		preferredDropEffect=int(_registerClipboardFormat(_PREFERRED_DROP_EFFECT_FORMAT_NAME)),
		excludeMonitor=int(_registerClipboardFormat(_EXCLUDE_MONITOR_FORMAT_NAME)),
		canIncludeHistory=int(_registerClipboardFormat(_CAN_INCLUDE_HISTORY_FORMAT_NAME)),
		canUpload=int(_registerClipboardFormat(_CAN_UPLOAD_FORMAT_NAME)),
	)


# Keep the format-specific bounds checks together so this trust boundary remains auditable.
def _parseDibInfo(data: bytes) -> tuple[int, int, int] | None:  # noqa: C901
	"""Return information for a complete, bounded, uncompressed packed DIB."""
	if len(data) < 12:
		return None
	headerSize = struct.unpack_from("<I", data)[0]
	if headerSize not in _SUPPORTED_DIB_HEADER_SIZES or headerSize > len(data):
		return None
	if headerSize == 12:
		width, height, planes, bitDepth = struct.unpack_from("<HHHH", data, 4)
		compression = _BI_RGB
		sizeImage = 0
		colorCount = 1 << bitDepth if bitDepth <= 8 else 0
		paletteEntrySize = 3
		extraMaskBytes = 0
	else:
		width, signedHeight, planes, bitDepth = struct.unpack_from("<iiHH", data, 4)
		if signedHeight == 0:
			return None
		height = abs(signedHeight)
		compression, sizeImage = struct.unpack_from("<II", data, 16)
		colorCount = struct.unpack_from("<I", data, 32)[0]
		paletteEntrySize = 4
		if bitDepth <= 8:
			maximumColorCount = 1 << bitDepth
			if colorCount == 0:
				colorCount = maximumColorCount
			elif colorCount > maximumColorCount:
				return None
		extraMaskBytes = 0
		if headerSize == 40 and compression in (_BI_BITFIELDS, _BI_ALPHABITFIELDS):
			extraMaskBytes = 16 if compression == _BI_ALPHABITFIELDS else 12
		elif headerSize == 52 and compression == _BI_ALPHABITFIELDS:
			extraMaskBytes = 4
	if (
		planes != 1
		or bitDepth not in _SUPPORTED_DIB_BIT_DEPTHS
		or compression not in (_BI_RGB, _BI_BITFIELDS, _BI_ALPHABITFIELDS)
		or (compression in (_BI_BITFIELDS, _BI_ALPHABITFIELDS) and bitDepth not in (16, 32))
		or (headerSize == 12 and bitDepth not in (1, 4, 8, 24))
		or not isImageSizeSafe(width, height, bitDepth)
	):
		return None
	colorTableOffset = headerSize + extraMaskBytes
	if colorCount > (len(data) - colorTableOffset) // paletteEntrySize:
		return None
	pixelOffset = colorTableOffset + colorCount * paletteEntrySize
	stride = ((width * bitDepth + 31) // 32) * 4
	minimumImageBytes = stride * height
	if sizeImage and sizeImage < minimumImageBytes:
		return None
	pixelBytes = max(sizeImage, minimumImageBytes)
	pixelEnd = pixelOffset + pixelBytes
	if pixelEnd > len(data):
		return None
	if headerSize == 124:
		profileOffset, profileSize = struct.unpack_from("<II", data, 112)
		if bool(profileOffset) != bool(profileSize):
			return None
		if profileSize and (profileOffset < pixelEnd or profileOffset + profileSize > len(data)):
			return None
	return width, height, bitDepth


def _bitmapHandleToDib(handle: int, imageInfo: tuple[int, int, int]) -> bytes | None:
	"""Copy a bounded GDI bitmap into a canonical 24-bit packed DIB."""
	width, height, _bitDepth = imageInfo
	rowStride = ((width * 3 + 3) // 4) * 4
	pixelBytes = rowStride * height
	if not isImageSizeSafe(width, height, 24) or 40 + pixelBytes > MAX_DECODED_IMAGE_BYTES:
		return None
	bitmapInfo = gdi32.BITMAPINFO()
	bitmapInfo.bmiHeader.biSize = ctypes.sizeof(gdi32.BITMAPINFOHEADER)
	bitmapInfo.bmiHeader.biWidth = width
	bitmapInfo.bmiHeader.biHeight = height
	bitmapInfo.bmiHeader.biPlanes = 1
	bitmapInfo.bmiHeader.biBitCount = 24
	bitmapInfo.bmiHeader.biCompression = _BI_RGB
	bitmapInfo.bmiHeader.biSizeImage = pixelBytes
	packedDib = ctypes.create_string_buffer(40 + pixelBytes)
	deviceContext = gdi32.CreateCompatibleDC(None)
	if not deviceContext:
		return None
	try:
		linesCopied = gdi32.GetDIBits(
			deviceContext,
			handle,
			0,
			height,
			ctypes.byref(packedDib, 40),
			ctypes.byref(bitmapInfo),
			_DIB_RGB_COLORS,
		)
	finally:
		gdi32.DeleteDC(deviceContext)
	if linesCopied != height:
		return None
	header = struct.pack(
		"<IiiHHIIiiII",
		40,
		width,
		height,
		1,
		24,
		_BI_RGB,
		pixelBytes,
		0,
		0,
		0,
		0,
	)
	memmove(packedDib, header, len(header))
	return packedDib.raw


def _buildDropFilesData(files: tuple[str, ...]) -> bytes:
	"""Build a Unicode DROPFILES block with a double-NUL-terminated path list."""
	if (
		not files
		or len(files) > _MAX_FILE_COUNT
		or any(not path or "\0" in path for path in files)
		or sum(len(path) for path in files) > _MAX_FILE_PATH_CHARACTERS
	):
		raise ValueError("Invalid clipboard file list")
	header = _DROPFILES(pFiles=ctypes.sizeof(_DROPFILES), ptX=0, ptY=0, fNC=False, fWide=True)
	headerData = string_at(ctypes.addressof(header), ctypes.sizeof(header))
	return headerData + ("\0".join(files) + "\0\0").encode("utf-16-le", errors="surrogatepass")


def _isValidDropFilesData(data: bytes) -> bool:
	"""Return whether bounded CF_HDROP data has a safe DROPFILES path list."""
	headerSize = ctypes.sizeof(_DROPFILES)
	if len(data) < headerSize:
		return False
	header = _DROPFILES.from_buffer_copy(data[:headerSize])
	pathOffset = int(header.pFiles)
	isWide = bool(header.fWide)
	minimumPathBytes = 4 if isWide else 2
	if pathOffset < headerSize or pathOffset > len(data) - minimumPathBytes or (isWide and pathOffset % 2):
		return False
	pathData = data[pathOffset:]
	if isWide:
		terminatorOffset = pathData.find(b"\0\0\0\0")
		while terminatorOffset >= 0:
			if terminatorOffset % 2 == 0:
				return True
			terminatorOffset = pathData.find(b"\0\0\0\0", terminatorOffset + 1)
		return False
	return b"\0\0" in pathData


class ClipboardMonitor:
	"""Monitor clipboard changes and read or restore the supported fixed formats.

	Call :meth:`start`, :meth:`stop`, and every write method from NVDA's main
	thread. Clipboard reads are performed on a worker and dispatched through wx.
	"""

	def __init__(
		self,
		onSnapshot: ClipboardSnapshotCallback | None = None,
		openRetries: int = DEFAULT_OPEN_RETRIES,
		openRetryInterval: float = DEFAULT_OPEN_RETRY_INTERVAL,
	) -> None:
		self._onSnapshot = onSnapshot
		self._openRetries = openRetries
		self._openRetryInterval = openRetryInterval
		self._formats = _registerFormats()
		if not all(
			(
				self._formats.html,
				self._formats.rtf,
				self._formats.png,
				self._formats.preferredDropEffect,
				self._formats.excludeMonitor,
				self._formats.canIncludeHistory,
				self._formats.canUpload,
			),
		):
			raise WinError()
		self._window: _ClipboardMessageWindow | None = None
		self._lock = Lock()
		self._cancelToken = 0
		self._lastDispatchedSequence = 0
		self._readInProgress = False
		self._readAgain = False
		self._isRunning = False

	def start(self) -> None:
		"""Start listening for Windows clipboard changes."""
		if self._window is not None:
			return
		window = _createClipboardMessageWindow(self)
		if not _addClipboardFormatListener(window.handle):
			error = WinError()
			window.destroy()
			raise error
		self._window = window
		with self._lock:
			self._isRunning = True
			self._lastDispatchedSequence = 0

	def stop(self) -> None:
		"""Stop listening and destroy the clipboard message window."""
		with self._lock:
			self._isRunning = False
			self._cancelToken += 1
			self._readAgain = False
		if self._window is None:
			return
		if not _removeClipboardFormatListener(self._window.handle):
			log.debugWarning("Could not remove clipboard format listener.", exc_info=WinError())
		self._window.destroy()
		self._window = None

	def readNow(self, *, decodePng: bool = False) -> ClipboardSnapshot:
		"""Read the current clipboard state, optionally fully decoding PNG data after close."""
		try:
			return self._readSnapshot(decodePng=decodePng)
		except Exception as error:
			log.debugWarning("ClipboardMonitor failed to read clipboard.", exc_info=True)
			return ClipboardSnapshot(ClipboardContentType.ERROR, error=str(error))

	def readBitmapDib(
		self,
		expectedSequenceNumber: int,
	) -> tuple[bytes, tuple[int, int, int]] | None:
		"""Return a canonical bitmap DIB while the clipboard sequence still matches."""
		if not expectedSequenceNumber or self.getSequenceNumber() != expectedSequenceNumber:
			return None
		if not self._openClipboardWithRetry(None):
			return None
		try:
			if self.getSequenceNumber() != expectedSequenceNumber:
				return None
			data, imageInfo = self._readBitmapFromOpenClipboard()
			return (data, imageInfo) if data is not None and imageInfo is not None else None
		finally:
			if not _closeClipboard():
				log.debugWarning(
					"ClipboardMonitor failed to close clipboard after reading a bitmap fallback.",
					exc_info=WinError(),
				)

	# Keep HGLOBAL ownership transfer and cleanup in one scope to prevent resource leaks.
	def writeSnapshot(
		self,
		snapshot: ClipboardSnapshot,
		ownerHandle: int,
		expectedSequenceNumber: int | None = None,
		*,
		preparedPngDib: bytes | None = None,
	) -> int:
		"""Replace the clipboard if its sequence matches, reusing an optional prepared PNG DIB."""
		if not ownerHandle:
			raise ValueError("A non-null clipboard owner is required when setting data")
		formats = self._buildWritableFormats(snapshot, preparedPngDib=preparedPngDib)
		if not formats:
			raise ValueError(snapshot.contentType)
		allocated: list[tuple[int, int]] = []
		writeFailure: OSError | None = None
		try:
			for formatId, data in formats:
				allocated.append((formatId, self._allocateGlobalData(data)))
			if not self._openClipboardWithRetry(ownerHandle):
				raise OSError(_OPEN_CLIPBOARD_ERROR)
			try:
				if expectedSequenceNumber is not None and self.getSequenceNumber() != expectedSequenceNumber:
					raise ClipboardSequenceChangedError(expectedSequenceNumber)
				if not _emptyClipboard():
					raise WinError()
				for index, (formatId, handle) in enumerate(allocated):
					if not _setClipboardData(formatId, handle):
						writeFailure = WinError()
						break
					allocated[index] = (formatId, 0)
			finally:
				if not _closeClipboard():
					log.debugWarning(
						"ClipboardMonitor failed to close clipboard after writing.",
						exc_info=WinError(),
					)
			# The final sequence is only observable after Windows publishes all formats on close.
			sequenceNumber = self.getSequenceNumber()
			if self.getOwnerHandle() != ownerHandle:
				raise ClipboardSequenceChangedError(sequenceNumber)
			if writeFailure is not None:
				raise ClipboardWriteError(sequenceNumber) from writeFailure
		finally:
			for _formatId, handle in allocated:
				if handle:
					_globalFree(handle)
		return sequenceNumber

	def clearClipboard(self, ownerHandle: int, expectedSequenceNumber: int | None = None) -> int:
		"""Remove every format if the sequence still matches an optional expectation."""
		if not ownerHandle:
			raise ValueError("A non-null clipboard owner is required when clearing data")
		if not self._openClipboardWithRetry(ownerHandle):
			raise OSError(_OPEN_CLIPBOARD_ERROR)
		try:
			if expectedSequenceNumber is not None and self.getSequenceNumber() != expectedSequenceNumber:
				raise ClipboardSequenceChangedError(expectedSequenceNumber)
			if not _emptyClipboard():
				raise WinError()
		finally:
			if not _closeClipboard():
				log.debugWarning(
					"ClipboardMonitor failed to close clipboard after clearing.",
					exc_info=WinError(),
				)
		sequenceNumber = self.getSequenceNumber()
		if self.getOwnerHandle() != ownerHandle:
			raise ClipboardSequenceChangedError(sequenceNumber)
		return sequenceNumber

	def getSequenceNumber(self) -> int:
		"""Return the current Windows clipboard sequence number."""
		return int(_getClipboardSequenceNumber())

	def getOwnerHandle(self) -> int:
		"""Return the current Windows clipboard owner handle, or zero when there is none."""
		return int(_getClipboardOwner() or 0)

	def invalidatePendingSnapshots(self) -> None:
		"""Discard snapshots queued before a synchronous local clipboard write."""
		with self._lock:
			self._cancelToken += 1
			self._readAgain = False

	def handleClipboardUpdate(self) -> None:
		"""Handle WM_CLIPBOARDUPDATE from the private message window."""
		with self._lock:
			shouldRead = self._isRunning
			cancelToken = self._cancelToken
			if shouldRead and self._readInProgress:
				self._readAgain = True
				shouldRead = False
			elif shouldRead:
				self._readInProgress = True
		if not shouldRead:
			return
		thread = Thread(
			target=self._readAndDispatchSnapshot,
			args=(cancelToken,),
			name="nvdaClipboard.clipboardRead",
			daemon=True,
		)
		try:
			thread.start()
		except RuntimeError:
			with self._lock:
				self._readInProgress = False
				self._readAgain = False
			log.exception("ClipboardMonitor failed to start clipboard reader thread.")

	def _queueMainThread(self, callback: Callable[..., None], *args: object) -> bool:
		"""Queue one reader result and release reader state if wx is no longer accepting work."""
		try:
			wx.CallAfter(callback, *args)
		except RuntimeError:
			with self._lock:
				isRunning = self._isRunning
				self._readInProgress = False
				self._readAgain = False
			if isRunning:
				log.debugWarning("Could not schedule a clipboard reader callback.", exc_info=True)
			return False
		return True

	def _readAndDispatchSnapshot(self, cancelToken: int) -> None:
		"""Read and queue completed snapshots until pending updates are consumed."""
		reportedOpenFailure = False
		while True:
			snapshot = self.readNow(decodePng=True)
			shouldRestart = False
			shouldRetry = False
			shouldReadAgain = False
			with self._lock:
				if not self._isRunning or cancelToken != self._cancelToken:
					shouldRestart = self._isRunning and self._readAgain
					self._readInProgress = False
					self._readAgain = False
				else:
					shouldRetry = (
						snapshot.contentType == ClipboardContentType.ERROR
						and snapshot.error == _OPEN_CLIPBOARD_ERROR
					)
					if not shouldRetry:
						shouldReadAgain = self._readAgain
						self._readAgain = False
			if shouldRestart:
				self._queueMainThread(self.handleClipboardUpdate)
				return
			if shouldRetry:
				if not reportedOpenFailure:
					log.debugWarning(
						f"Could not open the clipboard after {self._openRetries} attempts; retrying.",
					)
					reportedOpenFailure = True
				sleep(_FAILED_OPEN_RETRY_INTERVAL)
				continue
			if self._onSnapshot is not None and not self._queueMainThread(
				self._dispatchSnapshot,
				cancelToken,
				snapshot,
			):
				return
			with self._lock:
				if not self._isRunning or cancelToken != self._cancelToken:
					shouldRestart = self._isRunning and self._readAgain
					self._readInProgress = False
					self._readAgain = False
					shouldReadAgain = False
				else:
					shouldReadAgain = shouldReadAgain or self._readAgain
					self._readAgain = False
					if not shouldReadAgain:
						self._readInProgress = False
			if shouldRestart:
				self._queueMainThread(self.handleClipboardUpdate)
				return
			if not shouldReadAgain:
				return

	def _dispatchSnapshot(self, cancelToken: int, snapshot: ClipboardSnapshot) -> None:
		"""Invoke the snapshot callback when the queued result is still applicable."""
		with self._lock:
			if cancelToken != self._cancelToken or not self._isRunning:
				return
			if snapshot.sequenceNumber:
				if self._lastDispatchedSequence and not _isNewerClipboardSequence(
					snapshot.sequenceNumber,
					self._lastDispatchedSequence,
				):
					return
				self._lastDispatchedSequence = snapshot.sequenceNumber
			callback = self._onSnapshot
		if callback is not None:
			try:
				callback(snapshot)
			except Exception:
				log.exception("ClipboardMonitor callback failed.")

	def _readSnapshot(self, *, decodePng: bool) -> ClipboardSnapshot:
		"""Read one internally consistent snapshot, decoding copied PNG data after close."""
		if not self._openClipboardWithRetry(None):
			return ClipboardSnapshot(ClipboardContentType.ERROR, error=_OPEN_CLIPBOARD_ERROR)
		imageSnapshot: ClipboardSnapshot | None = None
		try:
			if self._isFormatAvailable(self._formats.excludeMonitor):
				return ClipboardSnapshot(
					ClipboardContentType.PROTECTED,
					sequenceNumber=self.getSequenceNumber(),
					canIncludeInHistory=False,
					canUpload=False,
				)
			canIncludeInHistory = self._readPolicy(self._formats.canIncludeHistory)
			canUpload = self._readPolicy(self._formats.canUpload)
			if self._isFormatAvailable(CF_HDROP):
				try:
					files = tuple(self._readFilesFromOpenClipboard())
				except _ClipboardDataLimitError:
					return ClipboardSnapshot(
						ClipboardContentType.UNSUPPORTED,
						sequenceNumber=self.getSequenceNumber(),
						canIncludeInHistory=canIncludeInHistory,
						canUpload=canUpload,
					)
				return ClipboardSnapshot(
					ClipboardContentType.FILES if files else ClipboardContentType.UNSUPPORTED,
					sequenceNumber=self.getSequenceNumber(),
					files=files,
					preferredDropEffect=self._readPreferredDropEffect(),
					canIncludeInHistory=canIncludeInHistory,
					canUpload=canUpload,
				)
			text = self._readTextIfAvailable()
			html, htmlDropped = self._readOptionalFormat(self._formats.html, MAX_RICH_FORMAT_BYTES)
			richBytesRemaining = MAX_RICH_FORMAT_BYTES - len(html or b"")
			rtf, rtfDropped = self._readOptionalFormat(self._formats.rtf, richBytesRemaining)
			imageFormat, imageData, imageInfo, imageDropped = self._readImageFromOpenClipboard()
			hasAdvertisedImage = imageData is not None or imageDropped
			if hasAdvertisedImage:
				width, height, bitDepth = imageInfo or (0, 0, 0)
				imageSnapshot = ClipboardSnapshot(
					ClipboardContentType.TEXT_AND_IMAGE if text else ClipboardContentType.IMAGE,
					sequenceNumber=self.getSequenceNumber(),
					text=text or "",
					html=html,
					rtf=rtf,
					imageFormat=imageFormat,
					imageData=imageData,
					imageWidth=width,
					imageHeight=height,
					imageBitDepth=bitDepth,
					canIncludeInHistory=canIncludeInHistory,
					canUpload=canUpload,
					richFormatsDropped=htmlDropped or rtfDropped,
				)
			elif text:
				return ClipboardSnapshot(
					ClipboardContentType.FORMATTED_TEXT
					if html is not None or rtf is not None
					else ClipboardContentType.TEXT,
					sequenceNumber=self.getSequenceNumber(),
					text=text,
					html=html,
					rtf=rtf,
					canIncludeInHistory=canIncludeInHistory,
					canUpload=canUpload,
					richFormatsDropped=htmlDropped or rtfDropped,
				)
			else:
				formatCount = _countClipboardFormats()
				policyFormatCount = sum(
					self._isFormatAvailable(formatId)
					for formatId in (self._formats.canIncludeHistory, self._formats.canUpload)
				)
				contentType = (
					ClipboardContentType.EMPTY
					if formatCount == 0 or (text == "" and formatCount == policyFormatCount + 1)
					else ClipboardContentType.UNSUPPORTED
				)
				return ClipboardSnapshot(
					contentType,
					sequenceNumber=self.getSequenceNumber(),
					canIncludeInHistory=canIncludeInHistory,
					canUpload=canUpload,
					richFormatsDropped=htmlDropped or rtfDropped,
				)
		finally:
			if not _closeClipboard():
				log.debugWarning("ClipboardMonitor failed to close clipboard.", exc_info=WinError())
		assert imageSnapshot is not None
		if (
			decodePng
			and imageSnapshot.imageFormat == "PNG"
			and imageSnapshot.imageData is not None
			and not isPngImageDecodable(
				imageSnapshot.imageData,
				(imageSnapshot.imageWidth, imageSnapshot.imageHeight, imageSnapshot.imageBitDepth),
			)
		):
			imageFormat, imageData, imageInfo, _imageDropped = self._readDibImageForSequence(
				imageSnapshot.sequenceNumber,
			)
			if imageData is None or imageInfo is None:
				return replace(
					imageSnapshot,
					imageFormat=None,
					imageData=None,
				)
			return replace(
				imageSnapshot,
				imageFormat=imageFormat,
				imageData=imageData,
				imageWidth=imageInfo[0],
				imageHeight=imageInfo[1],
				imageBitDepth=imageInfo[2],
			)
		return imageSnapshot

	def _readTextIfAvailable(self) -> str | None:
		"""Return bounded CF_UNICODETEXT data, distinguishing an empty value from absence."""
		if not self._isFormatAvailable(CF_UNICODETEXT):
			return None
		try:
			data = self._readGlobalData(CF_UNICODETEXT, MAX_TEXT_BYTES)
		except _ClipboardDataLimitError:
			return None
		except OSError:
			log.debugWarning("Could not read bounded Unicode clipboard text.", exc_info=True)
			return None
		text = data[: len(data) - (len(data) % 2)].decode("utf-16-le", errors="replace")
		return text.partition("\0")[0]

	def _readOptionalFormat(self, formatId: int, maximumBytes: int) -> tuple[bytes | None, bool]:
		"""Read one optional HGLOBAL format and report whether it was dropped."""
		if not self._isFormatAvailable(formatId):
			return None, False
		try:
			return self._readGlobalData(formatId, maximumBytes), False
		except _ClipboardDataLimitError:
			return None, True
		except OSError:
			log.debugWarning(f"Could not retain clipboard format {formatId}.", exc_info=True)
			return None, True

	def _readImageFromOpenClipboard(
		self,
	) -> tuple[str | None, bytes | None, tuple[int, int, int] | None, bool]:
		"""Read the richest valid bounded image representation from the clipboard."""
		advertised = self._isFormatAvailable(self._formats.png)
		if advertised:
			try:
				data = self._readGlobalData(self._formats.png, MAX_IMAGE_BYTES)
			except (_ClipboardDataLimitError, OSError):
				pass
			else:
				info = getPngImageInfo(data)
				if info is not None:
					return "PNG", data, info, False
		return self._readDibImageFromOpenClipboard(advertised=advertised)

	def _readDibImageFromOpenClipboard(
		self,
		*,
		advertised: bool = False,
	) -> tuple[str | None, bytes | None, tuple[int, int, int] | None, bool]:
		"""Read original DIB bytes, falling back to a canonical bitmap conversion."""
		dibImage: tuple[str, bytes, tuple[int, int, int]] | None = None
		for formatId, formatName in ((CF_DIBV5, "DIBV5"), (CF_DIB, "DIB")):
			if not self._isFormatAvailable(formatId):
				continue
			advertised = True
			try:
				data = self._readGlobalData(formatId, MAX_DECODED_IMAGE_BYTES)
			except (_ClipboardDataLimitError, OSError):
				continue
			info = _parseDibInfo(data)
			if info is not None:
				dibImage = (formatName, data, info)
				break
		bitmapInfo = None
		if dibImage is None and self._isFormatAvailable(CF_BITMAP):
			advertised = True
			data, bitmapInfo = self._readBitmapFromOpenClipboard()
			if data is not None and bitmapInfo is not None:
				dibImage = ("DIB", data, bitmapInfo)
		if dibImage is None:
			return None, None, bitmapInfo, advertised
		imageFormat, imageData, imageInfo = dibImage
		return imageFormat, imageData, imageInfo, False

	def _readDibImageForSequence(
		self,
		expectedSequenceNumber: int,
	) -> tuple[str | None, bytes | None, tuple[int, int, int] | None, bool]:
		"""Read a DIB fallback only while the clipboard still has the expected sequence."""
		dropped = (None, None, None, True)
		if not expectedSequenceNumber or self.getSequenceNumber() != expectedSequenceNumber:
			return dropped
		if not self._openClipboardWithRetry(None):
			return dropped
		try:
			if self.getSequenceNumber() != expectedSequenceNumber:
				return dropped
			return self._readDibImageFromOpenClipboard()
		finally:
			if not _closeClipboard():
				log.debugWarning(
					"ClipboardMonitor failed to close clipboard after reading a DIB fallback.",
					exc_info=WinError(),
				)

	def _readBitmapFromOpenClipboard(self) -> tuple[bytes | None, tuple[int, int, int] | None]:
		"""Return a bounded canonical DIB and metadata for an available bitmap."""
		handle = _getClipboardData(CF_BITMAP)
		if not handle:
			return None, None
		bitmap = _BITMAP()
		if not _getObject(handle, ctypes.sizeof(bitmap), ctypes.byref(bitmap)):
			return None, None
		width = int(bitmap.bmWidth)
		height = abs(int(bitmap.bmHeight))
		bitDepth = int(bitmap.bmPlanes) * int(bitmap.bmBitsPixel)
		if not isImageSizeSafe(width, height, bitDepth):
			return None, None
		data = _bitmapHandleToDib(int(handle), (width, height, bitDepth))
		return (data, (width, height, 24)) if data is not None else (None, (width, height, bitDepth))

	def _readPolicy(self, formatId: int) -> bool:
		"""Read a privacy DWORD, failing closed when present but malformed."""
		if not self._isFormatAvailable(formatId):
			return True
		try:
			handle = _getClipboardData(formatId)
			if not handle or int(_globalSize(handle)) < 4:
				return False
			address = _globalLock(handle)
			if not address:
				return False
			try:
				return struct.unpack("<I", string_at(address, 4))[0] == 1
			finally:
				_globalUnlock(handle)
		except OSError:
			return False

	def _readPreferredDropEffect(self) -> int:
		"""Return the preferred copy, move, or link effect for a file group."""
		if not self._isFormatAvailable(self._formats.preferredDropEffect):
			return _DROPEFFECT_COPY
		try:
			handle = _getClipboardData(self._formats.preferredDropEffect)
			if not handle or int(_globalSize(handle)) < 4:
				return _DROPEFFECT_COPY
			address = _globalLock(handle)
			if not address:
				return _DROPEFFECT_COPY
			try:
				dropEffect = struct.unpack("<I", string_at(address, 4))[0] & _DROPEFFECT_MASK
				return dropEffect or _DROPEFFECT_COPY
			finally:
				_globalUnlock(handle)
		except OSError:
			return _DROPEFFECT_COPY

	def _readGlobalData(self, formatId: int, maximumBytes: int) -> bytes:
		"""Copy bounded HGLOBAL clipboard data while the clipboard remains open."""
		handle = _getClipboardData(formatId)
		if not handle:
			raise WinError()
		size = int(_globalSize(handle))
		if size <= 0:
			raise WinError()
		if size > maximumBytes:
			raise _ClipboardDataLimitError(size)
		address = _globalLock(handle)
		if not address:
			raise WinError()
		try:
			return string_at(address, size)
		finally:
			_globalUnlock(handle)

	def _readFilesFromOpenClipboard(self) -> list[str]:
		"""Read bounded CF_HDROP file-system paths while the clipboard is open."""
		try:
			handle = _getClipboardData(CF_HDROP)
			if not handle:
				raise WinError()
			data = self._readGlobalData(CF_HDROP, _MAX_DROPFILES_BYTES)
		except (_ClipboardDataLimitError, OSError):
			return []
		if not _isValidDropFilesData(data):
			return []
		fileCount = int(_dragQueryFile(handle, DRAG_QUERY_FILE_COUNT, None, 0))
		if fileCount > _MAX_FILE_COUNT:
			raise _ClipboardDataLimitError(fileCount)
		files: list[str] = []
		totalCharacters = 0
		for index in range(fileCount):
			fileNameLength = int(_dragQueryFile(handle, index, None, 0))
			if fileNameLength <= 0:
				return []
			totalCharacters += fileNameLength
			if totalCharacters > _MAX_FILE_PATH_CHARACTERS:
				raise _ClipboardDataLimitError(totalCharacters)
			buffer = create_unicode_buffer(fileNameLength + 1)
			if int(_dragQueryFile(handle, index, buffer, len(buffer))) != fileNameLength:
				return []
			files.append(
				buffer.value.encode("utf-16-le", errors="surrogatepass").decode(
					"utf-16-le",
					errors="replace",
				),
			)
		return files

	def _buildWritableImageFormats(
		self,
		snapshot: ClipboardSnapshot,
		*,
		preparedPngDib: bytes | None = None,
	) -> list[tuple[int, bytes]]:
		"""Return validated Win32 image formats for one snapshot."""
		imageData = snapshot.imageData
		if preparedPngDib is not None and (snapshot.imageFormat != "PNG" or imageData is None):
			raise ValueError(snapshot.imageFormat)
		if imageData is None:
			return []
		maximumBytes = MAX_IMAGE_BYTES if snapshot.imageFormat == "PNG" else MAX_DECODED_IMAGE_BYTES
		if len(imageData) > maximumBytes:
			raise ValueError(snapshot.imageFormat)
		imageInfo = (snapshot.imageWidth, snapshot.imageHeight, snapshot.imageBitDepth)
		if snapshot.imageFormat == "PNG":
			if getPngImageInfo(imageData) != imageInfo:
				raise ValueError(snapshot.imageFormat)
			if preparedPngDib is None:
				dibData = pngToPackedDib(imageData, imageInfo)
			else:
				dibInfo = _parseDibInfo(preparedPngDib)
				if (
					len(preparedPngDib) > MAX_DECODED_IMAGE_BYTES + max(_SUPPORTED_DIB_HEADER_SIZES)
					or dibInfo is None
					or dibInfo[:2] != imageInfo[:2]
					or dibInfo[2] not in (24, 32)
				):
					raise ValueError(snapshot.imageFormat)
				dibData = preparedPngDib
			if dibData is None:
				raise ValueError(snapshot.imageFormat)
			return [(self._formats.png, imageData), (CF_DIB, dibData)]
		if snapshot.imageFormat in ("DIB", "DIBV5"):
			if _parseDibInfo(imageData) != imageInfo:
				raise ValueError(snapshot.imageFormat)
			formatId = CF_DIBV5 if snapshot.imageFormat == "DIBV5" else CF_DIB
			return [(formatId, imageData)]
		raise ValueError(snapshot.imageFormat)

	def _buildWritableFormats(
		self,
		snapshot: ClipboardSnapshot,
		*,
		preparedPngDib: bytes | None = None,
	) -> list[tuple[int, bytes]]:
		"""Convert one snapshot into ordered Win32 format payloads."""
		formats: list[tuple[int, bytes]] = []
		if len(snapshot.html or b"") + len(snapshot.rtf or b"") > MAX_RICH_FORMAT_BYTES:
			raise ValueError(MAX_RICH_FORMAT_BYTES)
		# Write restrictive policy formats before content so a later failure remains fail-closed.
		if not snapshot.canIncludeInHistory:
			formats.append((self._formats.canIncludeHistory, struct.pack("<I", 0)))
		if not snapshot.canUpload:
			formats.append((self._formats.canUpload, struct.pack("<I", 0)))
		if snapshot.files:
			formats.append((CF_HDROP, _buildDropFilesData(snapshot.files)))
			dropEffect = (snapshot.preferredDropEffect or _DROPEFFECT_COPY) & _DROPEFFECT_MASK
			formats.append(
				(
					self._formats.preferredDropEffect,
					struct.pack("<I", dropEffect or _DROPEFFECT_COPY),
				),
			)
		else:
			formats.extend(
				self._buildWritableImageFormats(snapshot, preparedPngDib=preparedPngDib),
			)
			if snapshot.html is not None:
				formats.append((self._formats.html, snapshot.html))
			if snapshot.rtf is not None:
				formats.append((self._formats.rtf, snapshot.rtf))
			if snapshot.text:
				if "\0" in snapshot.text:
					raise ValueError(CF_UNICODETEXT)
				textData = snapshot.text.encode("utf-16-le", errors="surrogatepass") + b"\0\0"
				if len(textData) > MAX_TEXT_BYTES:
					raise ValueError(CF_UNICODETEXT)
				formats.append((CF_UNICODETEXT, textData))
		return formats

	def _allocateGlobalData(self, data: bytes) -> int:
		"""Allocate and fill one movable global-memory clipboard payload."""
		if not data:
			raise ValueError(data)
		handle = _globalAlloc(_GMEM_MOVEABLE, len(data))
		if not handle:
			raise WinError()
		address = _globalLock(handle)
		if not address:
			_globalFree(handle)
			raise WinError()
		try:
			memmove(address, data, len(data))
		finally:
			_globalUnlock(handle)
		return int(handle)

	def _isFormatAvailable(self, formatId: int) -> bool:
		return bool(_isClipboardFormatAvailable(formatId))

	def _openClipboardWithRetry(self, ownerHandle: int | None) -> bool:
		"""Open the clipboard, retrying briefly while another process owns it."""
		for attempt in range(1, self._openRetries + 1):
			if _openClipboard(ownerHandle):
				return True
			if attempt < self._openRetries:
				sleep(self._openRetryInterval)
		return False


class _ClipboardMessageWindow(CustomWindow):
	className = f"{__name__}.Window"

	def __init__(self, monitor: ClipboardMonitor) -> None:
		self._monitor = monitor
		super().__init__(windowName=self.className, parent=HWND_MESSAGE)

	def windowProc(self, hwnd: int, msg: int, wParam: int, lParam: int) -> int | None:
		if msg == WM_CLIPBOARDUPDATE:
			self._monitor.handleClipboardUpdate()
			return 0
		return None


def _createClipboardMessageWindow(monitor: ClipboardMonitor) -> _ClipboardMessageWindow:
	windowClass = type(
		"_ClipboardMessageWindow_{:x}".format(id(monitor)),
		(_ClipboardMessageWindow,),
		{"className": "{}.{:x}".format(_ClipboardMessageWindow.className, id(monitor))},
	)
	return windowClass(monitor)
