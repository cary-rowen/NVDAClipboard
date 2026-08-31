# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Calculate Unicode text statistics without blocking NVDA's main thread."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Any

from logHandler import log


__all__ = [
	"TextStatistics",
	"TextStatisticsCalculation",
	"TextStatisticsProgress",
	"calculateTextStatistics",
]

_UBRK_CHARACTER = 0
_U_UNASSIGNED = 0
_UCHAR_IDEOGRAPHIC = 17
_USCRIPT_HAN = 17
_USCRIPT_UNKNOWN = 103
_CANCEL_POLL_INTERVAL = 256
_POSSIBLE_WHITESPACE_CATEGORIES = frozenset((12, 13, 14, 15))
_PUNCTUATION_CATEGORIES = frozenset((19, 20, 21, 22, 23, 28, 29))
_SYMBOL_CATEGORIES = frozenset((24, 25, 26, 27))
_LINE_BREAK_CHARACTERS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
_ASCII_INLINE_WHITESPACE = " \t"
_ASCII_PUNCTUATION = "!\"#%&'()*,-./:;?@[\\]_{}"
_ASCII_SYMBOLS = "$+<=>^`|~"
_icuUnavailableReported = False


@dataclass(frozen=True, slots=True)
class TextStatistics:
	"""Describe exact statistics available for one Unicode text value.

	A ``None`` category was not classifiable with the runtime ICU data and must not be reported.
	"""

	lineCount: int
	characterCount: int
	nonWhitespaceCharacterCount: int
	hanCharacterCount: int | None
	punctuationCount: int
	symbolCount: int


@dataclass(frozen=True, slots=True)
class TextStatisticsProgress:
	"""Describe the immutable result state of a text statistics calculation."""

	statistics: TextStatistics | None = None
	isComplete: bool = False


@dataclass(frozen=True, slots=True)
class _IcuBindings:
	"""Hold the ICU functions required in addition to NVDA's public bindings."""

	core: Any
	first: Callable[..., int]
	next: Callable[..., int]
	charType: Callable[..., int]
	isWhitespace: Callable[..., int]
	getScript: Callable[..., int] | None
	hasBinaryProperty: Callable[..., int] | None


class TextStatisticsCalculation:
	"""Calculate text statistics once in a cancellable background thread."""

	def __init__(self, text: str) -> None:
		"""Create a calculation that remains idle until :meth:`start` is called."""
		self._text = text
		self._cancelEvent = Event()
		self._progressLock = Lock()
		self._progress = TextStatisticsProgress()
		self._hasStarted = False

	def start(
		self,
		onComplete: Callable[[TextStatisticsCalculation], None] | None = None,
	) -> bool:
		"""Start the calculation once and return whether its worker was started."""
		with self._progressLock:
			if self._hasStarted:
				return False
			self._hasStarted = True
		thread = Thread(
			target=self._run,
			args=(onComplete,),
			name="nvdaClipboard.textStatistics",
			daemon=True,
		)
		try:
			thread.start()
		except RuntimeError:
			log.exception("Unable to start clipboard text statistics calculation")
			self._publish(None)
			return False
		return True

	def getProgress(self) -> TextStatisticsProgress:
		"""Return an immutable snapshot of the latest calculation state."""
		with self._progressLock:
			return self._progress

	def cancel(self) -> None:
		"""Request cancellation without waiting for the worker thread."""
		self._cancelEvent.set()

	def _run(self, onComplete: Callable[[TextStatisticsCalculation], None] | None) -> None:
		"""Calculate and publish one result from the worker thread."""
		statistics = calculateTextStatistics(self._text, self._cancelEvent)
		self._publish(statistics)
		if onComplete is not None:
			try:
				onComplete(self)
			except Exception:
				log.exception("Unable to report clipboard text statistics completion")

	def _publish(self, statistics: TextStatistics | None) -> None:
		"""Atomically replace the result state visible to callers."""
		with self._progressLock:
			self._progress = TextStatisticsProgress(statistics=statistics, isComplete=True)


def calculateTextStatistics(
	text: str,
	cancelEvent: Event | None = None,
) -> TextStatistics | None:
	"""Return exact Unicode statistics, or ``None`` when calculation is unavailable."""
	if cancelEvent is not None and cancelEvent.is_set():
		return None
	if text.isascii():
		(
			lineCount,
			characterCount,
			nonWhitespaceCharacterCount,
			punctuationCount,
			symbolCount,
		) = _calculateAsciiCharacterStatistics(text)
		return TextStatistics(
			lineCount=lineCount,
			characterCount=characterCount,
			nonWhitespaceCharacterCount=nonWhitespaceCharacterCount,
			hanCharacterCount=0,
			punctuationCount=punctuationCount,
			symbolCount=symbolCount,
		)
	bindings = _loadIcuBindings()
	if bindings is None:
		return None
	try:
		buffer = ctypes.create_unicode_buffer(text)
		return _calculateTextStatistics(text, buffer, bindings, cancelEvent)
	except Exception:
		log.exception("Unable to calculate clipboard text statistics")
		return None


def _calculateTextStatistics(
	text: str,
	buffer: ctypes.Array[ctypes.c_wchar],
	bindings: _IcuBindings,
	cancelEvent: Event | None,
) -> TextStatistics | None:
	"""Aggregate ICU character boundaries into one result."""
	lineBreakCount = 0
	characterCount = 0
	nonWhitespaceCharacterCount = 0
	hanCharacterCount: int | None = 0
	punctuationCount = 0
	symbolCount = 0
	with _breakIterator(bindings, _UBRK_CHARACTER, buffer) as iterator:
		start = bindings.first(iterator)
		while True:
			if cancelEvent is not None and cancelEvent.is_set():
				return None
			end = bindings.next(iterator)
			if end == bindings.core.UBRK_DONE:
				break
			segment = buffer[start:end]
			# ICU character boundaries isolate line breaks except for a CRLF pair.
			if segment == "\r\n" or (len(segment) == 1 and segment in _LINE_BREAK_CHARACTERS):
				lineBreakCount += 1
				start = end
				continue
			characterCount += 1
			isWhitespace = True
			hasPunctuation = False
			hasSymbol = False
			pollCancellation = cancelEvent is not None and len(segment) > _CANCEL_POLL_INTERVAL
			for index, character in enumerate(segment):
				if pollCancellation and index % _CANCEL_POLL_INTERVAL == 0 and cancelEvent.is_set():
					return None
				codePoint = ord(character)
				if hanCharacterCount is not None:
					isHanCharacter = _isHanCharacter(character, bindings)
					if isHanCharacter is None:
						hanCharacterCount = None
					elif isHanCharacter:
						hanCharacterCount += 1
						isWhitespace = False
						continue
				category = bindings.charType(codePoint)
				if category == _U_UNASSIGNED:
					return None
				if isWhitespace:
					isWhitespace = category in _POSSIBLE_WHITESPACE_CATEGORIES and bool(
						bindings.isWhitespace(codePoint),
					)
				hasPunctuation = hasPunctuation or category in _PUNCTUATION_CATEGORIES
				hasSymbol = hasSymbol or category in _SYMBOL_CATEGORIES
			if not isWhitespace:
				nonWhitespaceCharacterCount += 1
			punctuationCount += hasPunctuation
			symbolCount += hasSymbol
			start = end

	lineCount = max(1, lineBreakCount + int(not text.endswith(tuple(_LINE_BREAK_CHARACTERS))))
	return TextStatistics(
		lineCount=lineCount,
		characterCount=characterCount,
		nonWhitespaceCharacterCount=nonWhitespaceCharacterCount,
		hanCharacterCount=hanCharacterCount,
		punctuationCount=punctuationCount,
		symbolCount=symbolCount,
	)


def _calculateAsciiCharacterStatistics(text: str) -> tuple[int, int, int, int, int]:
	"""Return character statistics that are exact without an ICU character iterator."""
	carriageReturnLineFeedCount = text.count("\r\n")
	lineBreakCount = sum(text.count(character) for character in _LINE_BREAK_CHARACTERS if character.isascii())
	lineBreakCount -= carriageReturnLineFeedCount
	lineCount = max(1, lineBreakCount + int(not text.endswith(tuple(_LINE_BREAK_CHARACTERS))))
	characterCount = len(text) - lineBreakCount - carriageReturnLineFeedCount
	nonWhitespaceCharacterCount = characterCount - sum(
		text.count(character) for character in _ASCII_INLINE_WHITESPACE
	)
	punctuationCount = sum(text.count(character) for character in _ASCII_PUNCTUATION)
	symbolCount = sum(text.count(character) for character in _ASCII_SYMBOLS)
	return lineCount, characterCount, nonWhitespaceCharacterCount, punctuationCount, symbolCount


def _isHanCharacter(character: str, bindings: _IcuBindings) -> bool | None:
	"""Return whether a code point is an ICU Han ideograph, or ``None`` when unknown."""
	codePoint = ord(character)
	if codePoint < 0x2E80:
		return False
	if bindings.hasBinaryProperty is None:
		return None
	if not bindings.hasBinaryProperty(codePoint, _UCHAR_IDEOGRAPHIC):
		return False
	script = _getScript(character, bindings)
	return None if script is None else script == _USCRIPT_HAN


def _getScript(character: str, bindings: _IcuBindings) -> int | None:
	"""Return one ICU script code, or ``None`` when script classification fails."""
	if bindings.getScript is None:
		return None
	status = bindings.core.UErrorCode(0)
	script = bindings.getScript(ord(character), ctypes.byref(status))
	if bindings.core.U_FAILURE(status.value) or script < 0 or script == _USCRIPT_UNKNOWN:
		return None
	return script


@contextmanager
def _breakIterator(
	bindings: _IcuBindings,
	kind: int,
	buffer: ctypes.Array[ctypes.c_wchar],
) -> Iterator[object]:
	"""Open one ICU break iterator and close it after use."""
	status = bindings.core.UErrorCode(0)
	iterator = bindings.core.ubrk_open(kind, b"", buffer, len(buffer) - 1, ctypes.byref(status))
	if bindings.core.U_FAILURE(status.value) or not iterator:
		raise RuntimeError(f"ubrk_open failed with status {status.value}")
	try:
		yield iterator
	finally:
		bindings.core.ubrk_close(iterator)


def _loadIcuBindings() -> _IcuBindings | None:
	"""Load NVDA's ICU module and bind the additional character functions."""
	try:
		import winBindings.icu as icu
	except ImportError as error:
		_reportIcuUnavailable(error)
		return None
	try:
		if not icu.ICU_AVAILABLE or icu.dll is None:
			_reportIcuUnavailable()
			return None
	except AttributeError as error:
		_reportIcuUnavailable(error)
		return None
	try:
		first = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p)(("ubrk_first", icu.dll))
		nextBoundary = ctypes.WINFUNCTYPE(ctypes.c_int32, ctypes.c_void_p)(("ubrk_next", icu.dll))
		charType = ctypes.WINFUNCTYPE(ctypes.c_int8, ctypes.c_int32)(("u_charType", icu.dll))
		isWhitespace = ctypes.WINFUNCTYPE(ctypes.c_int8, ctypes.c_int32)(("u_isUWhiteSpace", icu.dll))
	except (AttributeError, OSError) as error:
		_reportIcuUnavailable(error)
		return None
	try:
		getScript = ctypes.WINFUNCTYPE(
			ctypes.c_int32,
			ctypes.c_int32,
			ctypes.POINTER(icu.UErrorCode),
		)(("uscript_getScript", icu.dll))
	except (AttributeError, OSError):
		getScript = None
	try:
		hasBinaryProperty = ctypes.WINFUNCTYPE(
			ctypes.c_int8,
			ctypes.c_int32,
			ctypes.c_int32,
		)(("u_hasBinaryProperty", icu.dll))
	except (AttributeError, OSError):
		hasBinaryProperty = None
	return _IcuBindings(
		core=icu,
		first=first,
		next=nextBoundary,
		charType=charType,
		isWhitespace=isWhitespace,
		getScript=getScript,
		hasBinaryProperty=hasBinaryProperty,
	)


def _reportIcuUnavailable(error: Exception | None = None) -> None:
	"""Log the first non-ASCII statistics request that cannot use NVDA's ICU runtime."""
	global _icuUnavailableReported
	if _icuUnavailableReported:
		return
	_icuUnavailableReported = True
	message = "NVDA ICU is unavailable for non-ASCII clipboard text statistics."
	if error is None:
		log.debugWarning(message)
	else:
		log.debugWarning(message, exc_info=error)
