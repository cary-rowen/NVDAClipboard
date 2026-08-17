"""Tests for the standalone ICU-based clipboard text statistics calculator."""

from __future__ import annotations

import ctypes
import importlib.util
from pathlib import Path
import sys
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import unicodedata


_MODULE_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "textStats.py"
_SPEC = importlib.util.spec_from_file_location("nvdaClipboardTextStats", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
textStats = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = textStats
with patch.dict(sys.modules, {"logHandler": SimpleNamespace(log=Mock())}):
	_SPEC.loader.exec_module(textStats)


class _FakeIterator:
	"""Iterate over predetermined ICU boundary positions."""

	def __init__(self, boundaries: tuple[int, ...]) -> None:
		"""Store ordered boundary positions."""
		self.boundaries = boundaries
		self.index = -1


class _FakeIcu:
	"""Provide the subset of NVDA's ICU binding used by the calculator."""

	UBRK_DONE = -1
	UErrorCode = ctypes.c_int32

	def __init__(self, characterBoundaries: tuple[int, ...]) -> None:
		"""Store character boundaries for one test text."""
		self._characterBoundaries = characterBoundaries

	@staticmethod
	def U_FAILURE(code: int) -> bool:
		"""Return whether a fake ICU status is an error."""
		return code > 0

	def ubrk_open(
		self,
		_kind: int,
		_locale: bytes,
		_buffer: ctypes.Array[ctypes.c_wchar],
		_length: int,
		_status: object,
	) -> _FakeIterator:
		"""Return a character boundary iterator."""
		return _FakeIterator(self._characterBoundaries)

	@staticmethod
	def ubrk_close(_iterator: _FakeIterator) -> None:
		"""Accept closing a fake break iterator."""


class TextStatisticsTests(unittest.TestCase):
	"""Verify accurate aggregation without loading NVDA."""

	def testMixedUnicodeStatistics(self) -> None:
		"""Count lines, Han characters, punctuation, and one family emoji."""
		text = "你好，world\u00a0123.45 👨‍👩‍👧‍👦！\r\n第二行。"
		characterSegments = tuple(text[:16]) + (
			"👨‍👩‍👧‍👦",
			"！",
			"\r\n",
			"第",
			"二",
			"行",
			"。",
		)
		bindings = self._createBindings(characterSegments)

		with patch.object(textStats, "_loadIcuBindings", return_value=bindings):
			statistics = textStats.calculateTextStatistics(text)

		self.assertEqual(
			textStats.TextStatistics(
				lineCount=2,
				characterCount=22,
				nonWhitespaceCharacterCount=20,
				hanCharacterCount=5,
				punctuationCount=4,
				symbolCount=1,
			),
			statistics,
		)

	def testAsciiFastPathPreservesLineAndCharacterSemantics(self) -> None:
		"""Count ASCII text exactly while treating CRLF as one trailing line break."""
		text = "one\r\ntwo\n"
		statistics = textStats.calculateTextStatistics(text)

		self.assertIsNotNone(statistics)
		assert statistics is not None
		self.assertEqual(2, statistics.lineCount)
		self.assertEqual(6, statistics.characterCount)
		self.assertEqual(6, statistics.nonWhitespaceCharacterCount)
		self.assertEqual(0, statistics.hanCharacterCount)
		controlStatistics = textStats.calculateTextStatistics("\x1f \t")
		self.assertIsNotNone(controlStatistics)
		assert controlStatistics is not None
		self.assertEqual(3, controlStatistics.characterCount)
		self.assertEqual(1, controlStatistics.nonWhitespaceCharacterCount)

	def testUnavailableIcuAndCancellationReturnNoStatistics(self) -> None:
		"""Omit statistics rather than inventing counts when ICU cannot classify text."""
		with patch.object(textStats, "_loadIcuBindings", return_value=None):
			self.assertIsNone(textStats.calculateTextStatistics("é"))
		text = "你\U0002ebf0"
		bindings = self._createBindings(tuple(text), unassignedCodePoints={0x2EBF0})
		with patch.object(textStats, "_loadIcuBindings", return_value=bindings):
			self.assertIsNone(textStats.calculateTextStatistics(text))
		cancelEvent = Event()
		cancelEvent.set()
		with patch.object(textStats, "_loadIcuBindings") as loadBindings:
			self.assertIsNone(textStats.calculateTextStatistics("text", cancelEvent))
		loadBindings.assert_not_called()

	def testCancellationWithinLongGraphemeCluster(self) -> None:
		"""Poll cancellation while classifying one unusually long grapheme cluster."""
		text = "a" + "\u0301" * (textStats._CANCEL_POLL_INTERVAL * 4)
		bindings = self._createBindings((text,))
		cancelEvent = Mock()
		cancelEvent.is_set.side_effect = (False, False, False, True)

		with patch.object(textStats, "_loadIcuBindings", return_value=bindings):
			self.assertIsNone(textStats.calculateTextStatistics(text, cancelEvent))

		self.assertEqual(4, cancelEvent.is_set.call_count)

	def _createBindings(
		self,
		characterSegments: tuple[str, ...],
		unassignedCodePoints: set[int] | None = None,
	) -> textStats._IcuBindings:
		"""Create fake bindings for predetermined character segments."""
		unassignedCodePoints = unassignedCodePoints or set()
		characterBoundaries = self._segmentsToBoundaries(characterSegments)
		core = _FakeIcu(characterBoundaries)

		def first(iterator: _FakeIterator) -> int:
			"""Reset a fake iterator and return the first boundary."""
			iterator.index = -1
			return 0

		def nextBoundary(iterator: _FakeIterator) -> int:
			"""Advance a fake iterator and return its next boundary."""
			iterator.index += 1
			if iterator.index >= len(iterator.boundaries):
				return core.UBRK_DONE
			return iterator.boundaries[iterator.index]

		def charType(codePoint: int) -> int:
			"""Return representative ICU general category values."""
			if codePoint in unassignedCodePoints:
				return 0
			category = unicodedata.category(chr(codePoint))
			if category == "Zs":
				return 12
			if category == "Cc":
				return 15
			if category.startswith("P"):
				return 19
			if category.startswith("S"):
				return 24
			if category == "Cn":
				return 0
			return 1

		def isWhitespace(codePoint: int) -> int:
			"""Apply the ICU White_Space property to test characters."""
			return int(chr(codePoint).isspace() and not 0x1C <= codePoint <= 0x1F)

		def getScript(codePoint: int, _status: object) -> int:
			"""Classify the scripts used by the test values."""
			character = chr(codePoint)
			if "\u3400" <= character <= "\u9fff":
				return 17
			return 0

		def hasBinaryProperty(codePoint: int, propertyCode: int) -> int:
			"""Classify basic CJK ideographs for the fake ICU data."""
			character = chr(codePoint)
			return int(propertyCode == 17 and "\u3400" <= character <= "\u9fff")

		return textStats._IcuBindings(
			core=core,
			first=first,
			next=nextBoundary,
			charType=charType,
			isWhitespace=isWhitespace,
			getScript=getScript,
			hasBinaryProperty=hasBinaryProperty,
		)

	@staticmethod
	def _segmentsToBoundaries(segments: tuple[str, ...]) -> tuple[int, ...]:
		"""Convert text segments to platform-native ``c_wchar`` offsets."""
		boundaries: list[int] = []
		end = 0
		for segment in segments:
			end += len(ctypes.create_unicode_buffer(segment)) - 1
			boundaries.append(end)
		return tuple(boundaries)
