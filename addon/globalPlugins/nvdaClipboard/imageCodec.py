# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Decode and encode bounded clipboard images with Pillow."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from time import sleep
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
	from PIL import Image

from .clipboardData import (
	MAX_DECODED_IMAGE_BYTES,
	MAX_IMAGE_BYTES,
	getPngImageInfo,
	isImageSizeSafe,
)


_PACKED_DIB_HEADER_BYTES = 40
_PREDOMINANT_PIXEL_PERCENTAGE = 95
_IMAGE_ANALYSIS_CHUNK_PIXELS = 64 * 1024
_PNG_SAMPLE_DEPTH_OFFSET = 24


@dataclass(frozen=True, slots=True)
class ImageProperties:
	"""Describe trustworthy pixel properties obtained from a fully decoded image."""

	color: tuple[int, int, int] | None = None
	colorPercentage: float = 0.0
	transparentPercentage: float = 0.0


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


def getImageProperties(
	imageFormat: str | None,
	imageData: bytes,
	expectedInfo: tuple[int, int, int],
) -> ImageProperties | None:
	"""Fully decode an image and return exact common-format properties, or ``None`` on failure."""
	pillowFormat = "PNG" if imageFormat == "PNG" else "DIB" if imageFormat in ("DIB", "DIBV5") else None
	if pillowFormat is None:
		return None
	image = _decodeImage(
		imageData,
		expectedInfo,
		imageFormat=pillowFormat,
		maximumBytes=MAX_IMAGE_BYTES if pillowFormat == "PNG" else MAX_DECODED_IMAGE_BYTES,
	)
	if image is None:
		return None
	try:
		if getattr(image, "n_frames", 1) != 1:
			return ImageProperties()
		if pillowFormat == "PNG":
			sampleDepth = imageData[_PNG_SAMPLE_DEPTH_OFFSET]
			if sampleDepth > 8 or image.mode not in ("1", "L", "LA", "P", "RGB", "RGBA"):
				return ImageProperties()
			if image.mode == "P":
				palette = image.getpalette()
				if (
					not palette
					or len(palette) % 3
					or cast(tuple[int, int], image.getextrema())[1] >= len(palette) // 3
				):
					return ImageProperties()
			if "transparency" in image.info:
				transparencyData = _getPngTransparencyData(imageData)
				if transparencyData is None or not _preparePngTransparency(
					image,
					sampleDepth,
					transparencyData,
				):
					return ImageProperties()
		else:
			headerSize = int.from_bytes(imageData[:4], "little")
			if (
				image.mode not in ("RGB", "RGBA")
				or expectedInfo[2] not in (24, 32)
				or (
					headerSize != 12
					and (len(imageData) < 36 or int.from_bytes(imageData[32:36], "little") != 0)
				)
			):
				return ImageProperties()
		solidColor = _getSolidColor(image)
		alphaExtrema, transparentPixels = _getAlphaProperties(image)
		pixelCount = image.width * image.height
		if alphaExtrema == (0, 0):
			return ImageProperties(transparentPercentage=100.0)
		hasMixedAlpha = alphaExtrema is not None and alphaExtrema != (255, 255)
		if hasMixedAlpha and _isPredominant(transparentPixels, pixelCount):
			return ImageProperties(
				transparentPercentage=_getPercentage(transparentPixels, pixelCount),
			)
		if solidColor is not None and not hasMixedAlpha:
			return ImageProperties(color=solidColor, colorPercentage=100.0)
		predominantColor = _getPredominantBlackOrWhite(image)
		if predominantColor is not None:
			color, colorPixels = predominantColor
			return ImageProperties(
				color=color,
				colorPercentage=_getPercentage(colorPixels, pixelCount),
			)
		return ImageProperties()
	finally:
		image.close()


def _getSolidColor(image: Image.Image) -> tuple[int, int, int] | None:
	"""Return one RGB value when every non-alpha channel is constant."""
	if image.mode == "P":
		palette = image.getpalette()
		if not palette:
			return None
		colors = {
			tuple(palette[index * 3 : index * 3 + 3])
			for index, count in enumerate(image.histogram())
			if count
		}
		return cast(tuple[int, int, int], next(iter(colors))) if len(colors) == 1 else None
	extrema = image.getextrema()
	channelExtrema = (
		(cast(tuple[int, int], extrema),)
		if isinstance(extrema[0], int)
		else cast(tuple[tuple[int, int], ...], extrema)
	)
	colorChannelCount = len(channelExtrema) - ("A" in image.getbands())
	if any(minimum != maximum for minimum, maximum in channelExtrema[:colorChannelCount]):
		return None
	with image.crop((0, 0, 1, 1)).convert("RGB") as pixelImage:
		return cast(tuple[int, int, int], pixelImage.getpixel((0, 0)))


def _getPngTransparencyData(imageData: bytes) -> bytes | None:
	"""Return one pre-IDAT PNG tRNS payload, or ``None`` when its placement is invalid."""
	offset = 8
	transparencyData: bytes | None = None
	hasImageData = False
	while offset + 12 <= len(imageData):
		chunkLength = int.from_bytes(imageData[offset : offset + 4], "big")
		payloadStart = offset + 8
		payloadEnd = payloadStart + chunkLength
		chunkEnd = payloadEnd + 4
		if chunkEnd > len(imageData):
			return None
		chunkType = imageData[offset + 4 : payloadStart]
		if chunkType == b"IDAT":
			hasImageData = True
		elif chunkType == b"tRNS":
			if hasImageData or transparencyData is not None:
				return None
			transparencyData = imageData[payloadStart:payloadEnd]
		elif chunkType == b"IEND":
			return transparencyData if hasImageData else None
		offset = chunkEnd
	return None


def _preparePngTransparency(image: Image.Image, sampleDepth: int, transparencyData: bytes) -> bool:
	"""Validate raw PNG tRNS data and normalize it for Pillow's alpha conversion."""
	if image.mode in ("1", "L") and sampleDepth in (1, 2, 4, 8):
		if len(transparencyData) != 2:
			return False
		transparency = int.from_bytes(transparencyData, "big")
		maximumSample = (1 << sampleDepth) - 1
		if transparency > maximumSample:
			return False
		image.info["transparency"] = transparency * 255 // maximumSample
		return True
	if image.mode == "P" and sampleDepth in (1, 2, 4, 8):
		palette = image.getpalette()
		paletteEntryCount = len(palette) // 3 if palette else 0
		if not 0 < len(transparencyData) <= paletteEntryCount:
			return False
		image.info["transparency"] = transparencyData
		return True
	if image.mode != "RGB" or sampleDepth != 8 or len(transparencyData) != 6:
		return False
	transparency = tuple(
		int.from_bytes(transparencyData[offset : offset + 2], "big") for offset in range(0, 6, 2)
	)
	if any(sample > 255 for sample in transparency):
		return False
	image.info["transparency"] = transparency
	return True


def _getAlphaProperties(image: Image.Image) -> tuple[tuple[int, int] | None, int]:
	"""Return alpha bounds and the number of fully transparent pixels."""
	if "A" in image.getbands():
		with image.getchannel("A") as alphaChannel:
			return cast(tuple[int, int], alphaChannel.getextrema()), alphaChannel.histogram()[0]
	if "transparency" not in image.info:
		return None, 0
	alphaMinimum, alphaMaximum = 255, 0
	transparentPixels = 0
	rowsPerChunk = max(1, _IMAGE_ANALYSIS_CHUNK_PIXELS // image.width)
	for top in range(0, image.height, rowsPerChunk):
		with (
			image.crop((0, top, image.width, min(top + rowsPerChunk, image.height))) as imageChunk,
			imageChunk.convert("RGBA") as rgbaImage,
			rgbaImage.getchannel("A") as alphaChannel,
		):
			chunkMinimum, chunkMaximum = cast(tuple[int, int], alphaChannel.getextrema())
			alphaMinimum = min(alphaMinimum, chunkMinimum)
			alphaMaximum = max(alphaMaximum, chunkMaximum)
			transparentPixels += alphaChannel.histogram()[0]
		sleep(0)
	return (alphaMinimum, alphaMaximum), transparentPixels


def _getPredominantBlackOrWhite(image: Image.Image) -> tuple[tuple[int, int, int], int] | None:
	"""Return black or white when it covers at least the reporting threshold."""
	from PIL import ImageChops

	blackPixels = whitePixels = 0
	rowsPerChunk = max(1, _IMAGE_ANALYSIS_CHUNK_PIXELS // image.width)
	for top in range(0, image.height, rowsPerChunk):
		with (
			image.crop((0, top, image.width, min(top + rowsPerChunk, image.height))) as imageChunk,
			imageChunk.convert("RGBA") as rgbaImage,
		):
			red, green, blue, alpha = rgbaImage.split()
			with red, green, blue, alpha:
				with (
					ImageChops.lighter(red, green) as maximumRedGreen,
					ImageChops.lighter(maximumRedGreen, blue) as maximumChannel,
					ImageChops.invert(alpha) as inverseAlpha,
					ImageChops.lighter(maximumChannel, inverseAlpha) as opaqueMaximum,
				):
					blackPixels += opaqueMaximum.histogram()[0]
				with (
					ImageChops.darker(red, green) as minimumRedGreen,
					ImageChops.darker(minimumRedGreen, blue) as minimumChannel,
					ImageChops.darker(minimumChannel, alpha) as opaqueMinimum,
				):
					whitePixels += opaqueMinimum.histogram()[255]
		sleep(0)
	pixelCount = image.width * image.height
	color, colorPixels = (
		((0, 0, 0), blackPixels) if blackPixels >= whitePixels else ((255, 255, 255), whitePixels)
	)
	return (color, colorPixels) if _isPredominant(colorPixels, pixelCount) else None


def _isPredominant(matchingPixels: int, pixelCount: int) -> bool:
	"""Return whether matching pixels meet the fixed reporting threshold."""
	return matchingPixels * 100 >= pixelCount * _PREDOMINANT_PIXEL_PERCENTAGE


def _getPercentage(matchingPixels: int, pixelCount: int) -> float:
	"""Return a one-decimal percentage truncated so a partial match never becomes 100%."""
	return (matchingPixels * 1000 // pixelCount) / 10


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
