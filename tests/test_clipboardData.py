"""Tests for standalone clipboard payload validation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
import sys
import unittest
import zlib


_MODULE_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "clipboardData.py"
_SPEC = importlib.util.spec_from_file_location("nvdaClipboardClipboardData", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
clipboardData = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = clipboardData
_SPEC.loader.exec_module(clipboardData)


def _buildChunk(chunkType: bytes, payload: bytes) -> bytes:
	"""Build one PNG chunk with its length and CRC."""
	return (
		struct.pack(">I", len(payload))
		+ chunkType
		+ payload
		+ struct.pack(">I", zlib.crc32(chunkType + payload))
	)


def _buildPng(width: int = 1, height: int = 1) -> bytes:
	"""Build a minimal RGBA PNG stream for validation tests."""
	header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
	return (
		b"\x89PNG\r\n\x1a\n"
		+ _buildChunk(b"IHDR", header)
		+ _buildChunk(b"IDAT", zlib.compress(b"\0\0\0\0\0"))
		+ _buildChunk(b"IEND", b"")
	)


class ClipboardDataTests(unittest.TestCase):
	"""Verify clipboard payload validation without loading NVDA."""

	def testPngValidation(self) -> None:
		"""Accept a small PNG and reject corrupt, truncated, or oversized data."""
		pngData = _buildPng()
		self.assertEqual((1, 1, 32), clipboardData.getPngImageInfo(pngData))
		self.assertIsNone(clipboardData.getPngImageInfo(pngData[:-12]))
		self.assertIsNone(clipboardData.getPngImageInfo(pngData + b"trailing"))
		self.assertIsNone(
			clipboardData.getPngImageInfo(pngData[:29] + bytes([pngData[29] ^ 1]) + pngData[30:]),
		)
		self.assertIsNone(clipboardData.getPngImageInfo(_buildPng(width=40_000_001)))

	def testImageDimensionsBoundScanlineWork(self) -> None:
		"""Reject narrow images with a pathological number of scanlines."""
		self.assertFalse(clipboardData.isImageSizeSafe(1, 30_000_000, 24))


if __name__ == "__main__":
	unittest.main()
