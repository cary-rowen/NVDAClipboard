# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Decode and encode bounded clipboard images with Pillow."""

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from PIL import Image

from .clipboardData import (
	MAX_DECODED_IMAGE_BYTES,
	MAX_IMAGE_BYTES,
	getPngImageInfo,
	isImageSizeSafe,
)


_PACKED_DIB_HEADER_BYTES = 40


class ImageDataTooLargeError(ValueError):
	"""Indicate that encoded image data exceeds its byte limit."""


class _BoundedBytesIO(BytesIO):
	"""Reject writes which would grow an in-memory image beyond a fixed limit."""

	def __init__(self, maximumBytes: int) -> None:
		super().__init__()
		self._maximumBytes = maximumBytes

	def write(self, data: bytes, /) -> int:
		"""Write bytes without allowing the stream to exceed its limit."""
		if self.tell() + len(data) > self._maximumBytes:
			raise ImageDataTooLargeError(self._maximumBytes)
		return super().write(data)


def _decodeImage(
	data: bytes,
	expectedInfo: tuple[int, int, int],
	*,
	imageFormat: str,
	maximumBytes: int,
) -> Image.Image | None:
	"""Fully decode one bounded image with the expected dimensions."""
	if len(data) > maximumBytes or not isImageSizeSafe(
		expectedInfo[0],
		expectedInfo[1],
		max(32, expectedInfo[2]),
	):
		return None
	from PIL import Image

	image: Image.Image | None = None
	try:
		image = Image.open(BytesIO(data), formats=(imageFormat,))
		image.load()
	except (Image.DecompressionBombError, OSError, SyntaxError, ValueError):
		if image is not None:
			image.close()
		return None
	if image.size != expectedInfo[:2]:
		image.close()
		return None
	return image


def _saveImage(image: Image.Image, imageFormat: str, maximumBytes: int) -> bytes | None:
	"""Encode one image without allowing its output buffer to exceed a fixed limit."""
	output = _BoundedBytesIO(maximumBytes)
	try:
		image.save(output, format=imageFormat)
	except ImageDataTooLargeError:
		raise
	except (OSError, ValueError):
		return None
	return output.getvalue()


def isPngImageDecodable(pngData: bytes, expectedInfo: tuple[int, int, int]) -> bool:
	"""Return whether Pillow fully decodes a bounded PNG with matching dimensions."""
	image = _decodeImage(
		pngData,
		expectedInfo,
		imageFormat="PNG",
		maximumBytes=MAX_IMAGE_BYTES,
	)
	if image is None:
		return False
	image.close()
	return True


def encodeBgrPixelsToPng(
	pixelData: bytes | memoryview,
	width: int,
	height: int,
	*,
	bytesPerPixel: int,
	rowStride: int,
	bottomUp: bool = False,
) -> bytes | None:
	"""Encode bounded BGR or BGRX pixels as an adaptively filtered RGB PNG."""
	expectedStride = ((width * 3 + 3) // 4) * 4 if bytesPerPixel == 3 else width * 4
	if (
		bytesPerPixel not in (3, 4)
		or rowStride != expectedStride
		or not isImageSizeSafe(width, height, bytesPerPixel * 8)
		or len(pixelData) != rowStride * height
	):
		return None
	from PIL import Image

	try:
		image = Image.frombytes(
			"RGB",
			(width, height),
			pixelData,
			"raw",
			"BGR" if bytesPerPixel == 3 else "BGRX",
			rowStride,
			-1 if bottomUp else 1,
		)
	except (OSError, ValueError):
		return None
	try:
		return _saveImage(image, "PNG", MAX_IMAGE_BYTES)
	finally:
		image.close()


def normalizeImageDataToPng(
	imageFormat: str | None,
	imageData: bytes,
	imageInfo: tuple[int, int, int],
	*,
	pngIsDecodable: bool = False,
) -> bytes | None:
	"""Normalize retained PNG or DIB bytes without accessing the clipboard."""
	if not isImageSizeSafe(imageInfo[0], imageInfo[1], max(32, imageInfo[2])):
		return None
	if imageFormat == "PNG":
		return imageData if pngIsDecodable and getPngImageInfo(imageData) == imageInfo else None
	if imageFormat not in ("DIB", "DIBV5"):
		return None
	image = _decodeImage(
		imageData,
		imageInfo,
		imageFormat="DIB",
		maximumBytes=MAX_DECODED_IMAGE_BYTES,
	)
	if image is None:
		return None
	try:
		return _saveImage(image, "PNG", MAX_IMAGE_BYTES)
	finally:
		image.close()


def pngToPackedDib(pngData: bytes, expectedInfo: tuple[int, int, int]) -> bytes | None:
	"""Fully decode a PNG and return a bounded packed DIB for clipboard interoperability."""
	image = _decodeImage(
		pngData,
		expectedInfo,
		imageFormat="PNG",
		maximumBytes=MAX_IMAGE_BYTES,
	)
	if image is None:
		return None
	convertedImage: Image.Image | None = None
	try:
		mode = "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
		convertedImage = image.convert(mode)
		try:
			return _saveImage(
				convertedImage,
				"DIB",
				MAX_DECODED_IMAGE_BYTES + _PACKED_DIB_HEADER_BYTES,
			)
		except ImageDataTooLargeError:
			return None
	finally:
		if convertedImage is not None:
			convertedImage.close()
		image.close()
