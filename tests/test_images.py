# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

from __future__ import annotations

import ctypes
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from PIL import Image

from tests._module_loader import loadAddonModule
from tests.test_imageCodec import clipboardData, imageCodec


_MODULE_DIRECTORY = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard"


class NavigatorImageTests(unittest.TestCase):
	def setUp(self) -> None:
		self.curtain = Mock(enabled=True)
		self.imageInfo = Mock()
		geometry = Mock()
		geometry.GetLeft.return_value = geometry.GetTop.return_value = 0
		geometry.GetWidth.return_value = geometry.GetHeight.return_value = 1
		display = Mock()
		display.GetCount.return_value = 1
		display.return_value.GetGeometry.return_value = geometry
		self.wgc = Mock()
		self.wgc.isSupported.return_value = True
		self.wgc.captureImage.return_value = ctypes.create_string_buffer(b"\x01\x02\x03\x00", 4)
		self.gdi = Mock()
		self.images = loadAddonModule(
			"tests.navigatorImages",
			_MODULE_DIRECTORY / "images.py",
			injectedModules={
				"addonHandler": Mock(),
				"api": SimpleNamespace(getNavigatorObject=lambda: SimpleNamespace(location=(0, 0, 1, 1))),
				"contentRecog": SimpleNamespace(RecogImageInfo=self.imageInfo, _wgcCapture=self.wgc),
				"gui.message": Mock(),
				"screenCurtain": SimpleNamespace(screenCurtain=self.curtain),
				"screenBitmap": SimpleNamespace(ScreenBitmap=self.gdi),
				"wx": SimpleNamespace(Display=display),
				"tests.clipboardData": clipboardData,
				"tests.imageCodec": imageCodec,
			},
		)

	def testScreenCurtainCaptureProducesPng(self) -> None:
		pngData = self.images.captureNavigatorObjectPng()
		with Image.open(BytesIO(pngData)) as image:
			self.assertEqual((1, 1), image.size)
			self.assertEqual((3, 2, 1), image.getpixel((0, 0)))
		self.imageInfo.assert_called_once_with(0, 0, 1, 1, 1)
		self.wgc.captureImage.assert_called_once_with(self.imageInfo.return_value)
		self.gdi.assert_not_called()
		self.curtain.disable.assert_not_called()

	def testScreenCurtainCaptureErrorsDoNotFallBackToGdi(self) -> None:
		for supported, errorType in (
			(False, self.images.ScreenCurtainCaptureUnavailableError),
			(True, RuntimeError),
		):
			with self.subTest(supported=supported):
				self.wgc.reset_mock()
				self.wgc.isSupported.return_value = supported
				self.wgc.captureImage.side_effect = RuntimeError("capture failed")
				with self.assertRaises(errorType):
					self.images.captureNavigatorObjectPng()
				self.assertEqual(int(supported), self.wgc.captureImage.call_count)
				self.gdi.assert_not_called()
				self.curtain.disable.assert_not_called()
