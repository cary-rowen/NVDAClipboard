"""Tests for bounded Pillow clipboard image encoding."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import random
import struct
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from tests._module_loader import loadAddonModule


_MODULE_DIRECTORY = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard"
_PACKAGE_NAME = "nvdaClipboardImageCodecTests"
_PACKAGE = ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_MODULE_DIRECTORY)]
sys.modules[_PACKAGE_NAME] = _PACKAGE


clipboardData = loadAddonModule(f"{_PACKAGE_NAME}.clipboardData", _MODULE_DIRECTORY / "clipboardData.py")
imageCodec = loadAddonModule(f"{_PACKAGE_NAME}.imageCodec", _MODULE_DIRECTORY / "imageCodec.py")


def _buildCanonicalDib() -> bytes:
	"""Build one complete 1-by-1 24-bit packed DIB."""
	pixelData = b"\x00\x01\x02\x00"
	return (
		struct.pack(
			"<IiiHHIIiiII",
			40,
			1,
			1,
			1,
			24,
			0,
			len(pixelData),
			0,
			0,
			0,
			0,
		)
		+ pixelData
	)


class ImageCodecTests(unittest.TestCase):
	"""Verify bounded Pillow conversion without loading NVDA."""

	def testImportDefersPillow(self) -> None:
		"""Keep Pillow out of the codec module until an image operation needs it."""
		self.assertNotIn("Image", vars(imageCodec))

	def testCorrelatedPixelsFitAfterPngFiltering(self) -> None:
		"""Use Pillow filtering so correlated pixels remain below the encoded byte limit."""
		width = 256
		height = 128
		randomGenerator = random.Random(1234)
		pixelData = bytearray(width * height * 3)
		previous = [128, 128, 128]
		offset = 0
		for _row in range(height):
			for _column in range(width):
				for channel in range(3):
					previous[channel] = (previous[channel] + randomGenerator.randrange(-2, 3)) & 0xFF
					pixelData[offset + channel] = previous[channel]
				offset += 3

		with patch.object(imageCodec, "MAX_IMAGE_BYTES", 50_000):
			pngData = imageCodec.encodeBgrPixelsToPng(
				pixelData,
				width,
				height,
				bytesPerPixel=3,
				rowStride=width * 3,
			)

		self.assertIsNotNone(pngData)
		self.assertEqual((width, height, 24), clipboardData.getPngImageInfo(pngData))

	def testDibV5NormalizationUsesPillow(self) -> None:
		"""Normalize a valid DIBV5 payload without requiring a 40-byte DIB header."""
		dibData = bytearray(128)
		struct.pack_into("<IiiHHII", dibData, 0, 124, 1, 1, 1, 32, 0, 4)

		pngData = imageCodec.normalizeImageDataToPng("DIBV5", bytes(dibData), (1, 1, 32))

		self.assertIsNotNone(pngData)
		self.assertEqual((1, 1, 24), clipboardData.getPngImageInfo(pngData))

	def testImageNormalizationReportsEncodedSizeLimit(self) -> None:
		"""Distinguish an oversized normalized PNG from invalid DIB input."""
		with (
			patch.object(imageCodec, "MAX_IMAGE_BYTES", 1),
			self.assertRaises(imageCodec.ImageDataTooLargeError),
		):
			imageCodec.normalizeImageDataToPng("DIB", _buildCanonicalDib(), (1, 1, 24))

	def testPackedDibLimitIncludesItsHeader(self) -> None:
		"""Allow a packed DIB header in addition to the decoded pixel limit."""
		from PIL import Image

		output = BytesIO()
		with Image.new("RGBA", (2, 2), (1, 2, 3, 4)) as image:
			image.save(output, format="PNG")

		with patch.object(imageCodec, "MAX_DECODED_IMAGE_BYTES", 16):
			dibData = imageCodec.pngToPackedDib(output.getvalue(), (2, 2, 32))

		self.assertIsNotNone(dibData)
		self.assertEqual(56, len(dibData))

	def testImagePropertiesRemainTrustworthy(self) -> None:
		"""Report predominant, palette, and validated transparency properties."""
		from PIL import Image

		output = BytesIO()
		with Image.new("RGBA", (100, 1), (0, 0, 0, 255)) as image:
			image.paste((0, 0, 0, 0), (0, 0, 4, 1))
			image.save(output, format="PNG")
		self.assertEqual(
			imageCodec.ImageProperties(color=(0, 0, 0), colorPercentage=96.0),
			imageCodec.getImageProperties("PNG", output.getvalue(), (100, 1, 32)),
		)

		output = BytesIO()
		with Image.new("P", (1, 1), 0) as image:
			image.putpalette([255, 0, 0])
			image.save(output, format="PNG", transparency=0)
		self.assertEqual(
			imageCodec.ImageProperties(transparentPercentage=100.0),
			imageCodec.getImageProperties("PNG", output.getvalue(), (1, 1, 1)),
		)

		output = BytesIO()
		with Image.new("P", (2, 1)) as image:
			image.putpalette([255, 0, 0, 255, 0, 0])
			image.putdata((0, 1))
			image.save(output, format="PNG")
		self.assertEqual(
			imageCodec.ImageProperties(color=(255, 0, 0), colorPercentage=100.0),
			imageCodec.getImageProperties("PNG", output.getvalue(), (2, 1, 1)),
		)

		output = BytesIO()
		with Image.new("P", (1, 1), 1) as image:
			image.putpalette([255, 0, 0])
			image.save(output, format="PNG")
		self.assertEqual(
			imageCodec.ImageProperties(),
			imageCodec.getImageProperties("PNG", output.getvalue(), (1, 1, 1)),
		)

		output = BytesIO()
		with Image.new("1", (1, 1), 1) as image:
			image.save(output, format="PNG", transparency=255)
		self.assertEqual(
			imageCodec.ImageProperties(),
			imageCodec.getImageProperties("PNG", output.getvalue(), (1, 1, 1)),
		)
