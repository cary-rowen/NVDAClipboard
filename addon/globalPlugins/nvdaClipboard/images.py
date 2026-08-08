# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Provide the clipboard image operations used by NVDA Clipboard."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import addonHandler
import api
from gui.message import displayDialogAsModal
import screenCurtain
import screenBitmap
import wx

from .clipboardData import getPngImageInfo, isImageSizeSafe
from .imageCodec import ImageDataTooLargeError, encodeBgrPixelsToPng, isPngImageDecodable


addonHandler.initTranslation()


def isScreenCurtainEnabled() -> bool:
	"""Return whether NVDA screen curtain is currently active."""
	controller = screenCurtain.screenCurtain
	return controller is not None and controller.enabled


def captureNavigatorObjectPng() -> bytes | None:
	"""Capture the visible navigator object and return it as bounded PNG data."""
	location = cast(tuple[int, int, int, int] | None, getattr(api.getNavigatorObject(), "location", None))
	if location is None:
		return None
	left, top, width, height = location
	if width <= 0 or height <= 0:
		return None
	right = left + width
	bottom = top + height
	visibleRects: list[tuple[int, int, int, int]] = []
	for index in range(wx.Display.GetCount()):
		display = wx.Display(index).GetGeometry()
		displayLeft = display.GetLeft()
		displayTop = display.GetTop()
		visibleLeft = max(left, displayLeft)
		visibleTop = max(top, displayTop)
		visibleRight = min(right, displayLeft + display.GetWidth())
		visibleBottom = min(bottom, displayTop + display.GetHeight())
		if visibleLeft < visibleRight and visibleTop < visibleBottom:
			visibleRects.append((visibleLeft, visibleTop, visibleRight, visibleBottom))
	if not visibleRects:
		return None
	left = min(rect[0] for rect in visibleRects)
	top = min(rect[1] for rect in visibleRects)
	right = max(rect[2] for rect in visibleRects)
	bottom = max(rect[3] for rect in visibleRects)
	width = right - left
	height = bottom - top
	if not isImageSizeSafe(width, height, 32):
		return None
	bitmap = screenBitmap.ScreenBitmap(width, height)
	try:
		pixels = memoryview(bitmap.captureImage(left, top, width, height)).cast("B")
	finally:
		del bitmap
	try:
		return encodeBgrPixelsToPng(
			pixels,
			width,
			height,
			bytesPerPixel=4,
			rowStride=width * 4,
		)
	except ImageDataTooLargeError:
		return None


def savePngImage(parent: wx.Window, pngData: bytes, title: str) -> bool | None:
	"""Validate and save PNG bytes without re-encoding them.

	``True`` indicates success, ``False`` indicates that the user cancelled the
	dialog, and ``None`` indicates invalid PNG data.
	"""
	imageInfo = getPngImageInfo(pngData)
	if imageInfo is None or not isPngImageDecodable(pngData, imageInfo):
		return None
	path = _getPngSavePath(parent, title)
	if path is None:
		return False
	Path(path).write_bytes(pngData)
	return True


def _getPngSavePath(parent: wx.Window, title: str) -> str | None:
	"""Prompt for a PNG destination path, or return ``None`` when cancelled."""
	dialog = wx.FileDialog(
		parent,
		title,
		# Translators: File type filter in the save clipboard image dialog.
		wildcard=_("PNG images (*.png)|*.png"),
		style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
	)
	try:
		if displayDialogAsModal(dialog) != wx.ID_OK:
			return None
		path = dialog.GetPath()
		if not path.casefold().endswith(".png"):
			path += ".png"
	finally:
		dialog.Destroy()
	return path
