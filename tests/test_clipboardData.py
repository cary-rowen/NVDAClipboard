# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for standalone clipboard payload validation."""

from __future__ import annotations

from pathlib import Path
import struct
import unittest
import zlib

from tests._module_loader import loadAddonModule


_MODULE_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "clipboardData.py"
clipboardData = loadAddonModule("nvdaClipboardClipboardData", _MODULE_PATH)


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
