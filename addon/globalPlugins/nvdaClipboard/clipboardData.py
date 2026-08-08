# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Validate bounded clipboard payload data without loading NVDA."""

import struct
import zlib


MAX_TEXT_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_DECODED_IMAGE_BYTES = 128 * 1024 * 1024
MAX_RICH_FORMAT_BYTES = 4 * 1024 * 1024
_MAX_IMAGE_PIXELS = 40_000_000
_MAX_IMAGE_DIMENSION = 100_000


def getPngImageInfo(data: bytes) -> tuple[int, int, int] | None:
	"""Return PNG image information after validating its complete chunk stream."""
	if len(data) < 45 or data[:8] != b"\x89PNG\r\n\x1a\n":
		return None
	dataView = memoryview(data)
	offset = 8
	imageInfo: tuple[int, int, int] | None = None
	isFirstChunk = True
	hasImageData = False
	while offset + 12 <= len(data):
		chunkLength = struct.unpack_from(">I", data, offset)[0]
		payloadStart = offset + 8
		payloadEnd = payloadStart + chunkLength
		chunkEnd = payloadEnd + 4
		if chunkEnd > len(data):
			return None
		chunkType = data[offset + 4 : payloadStart]
		payload = dataView[payloadStart:payloadEnd]
		expectedCrc = struct.unpack_from(">I", data, payloadEnd)[0]
		if zlib.crc32(payload, zlib.crc32(chunkType)) & 0xFFFFFFFF != expectedCrc:
			return None
		if isFirstChunk:
			if chunkType != b"IHDR" or chunkLength != 13:
				return None
			width, height = struct.unpack_from(">II", payload)
			bitDepth = payload[8]
			colorType = payload[9]
			compressionMethod = payload[10]
			filterMethod = payload[11]
			interlaceMethod = payload[12]
			channelCount = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(colorType)
			validBitDepths = {
				0: {1, 2, 4, 8, 16},
				2: {8, 16},
				3: {1, 2, 4, 8},
				4: {8, 16},
				6: {8, 16},
			}.get(colorType, set())
			bitsPerPixel = bitDepth * (channelCount or 0)
			if (
				width <= 0
				or height <= 0
				or channelCount is None
				or bitDepth not in validBitDepths
				or compressionMethod != 0
				or filterMethod != 0
				or interlaceMethod not in (0, 1)
				or not isImageSizeSafe(width, height, bitsPerPixel)
			):
				return None
			imageInfo = (width, height, bitsPerPixel)
		elif chunkType == b"IHDR":
			return None
		elif chunkType == b"IDAT":
			hasImageData = True
		if chunkType == b"IEND":
			if chunkLength != 0 or not hasImageData or chunkEnd != len(data) or imageInfo is None:
				return None
			return imageInfo
		isFirstChunk = False
		offset = chunkEnd
	return None


def isImageSizeSafe(width: int, height: int, bitDepth: int) -> bool:
	"""Return whether image dimensions and decoded pixel storage stay within fixed limits."""
	if width <= 0 or height <= 0 or bitDepth <= 0:
		return False
	pixels = width * height
	bytesPerPixel = max(1, (bitDepth + 7) // 8)
	return (
		max(width, height) <= _MAX_IMAGE_DIMENSION
		and pixels <= _MAX_IMAGE_PIXELS
		and pixels * bytesPerPixel <= MAX_DECODED_IMAGE_BYTES
	)
