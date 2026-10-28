# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for protected clipboard transaction handling without loading NVDA."""

from __future__ import annotations

import ast
import ctypes
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from tests._module_loader import addonSourcePath, createAddonPackage, loadAddonModule


_PACKAGE_NAME = "nvdaClipboardClipboardMonitorTests"
createAddonPackage(_PACKAGE_NAME)
_WX = SimpleNamespace()

with (
	patch.object(ctypes, "WinDLL", side_effect=lambda *_args, **_kwargs: Mock(), create=True),
	patch.object(ctypes, "get_last_error", Mock(return_value=0), create=True),
	patch.object(ctypes, "WinError", lambda *_args: OSError(), create=True),
	patch.dict(
		sys.modules,
		{
			"logHandler": SimpleNamespace(log=Mock()),
			"winBindings": SimpleNamespace(gdi32=Mock(), kernel32=Mock(), user32=Mock()),
			"windowUtils": SimpleNamespace(CustomWindow=type("CustomWindow", (), {})),
			"wx": _WX,
		},
	),
):
	clipboardMonitor = loadAddonModule(
		f"{_PACKAGE_NAME}.clipboard.monitor",
	)


def _buildDibV5() -> bytes:
	"""Build one complete 1-by-1 32-bit DIBV5 payload."""
	data = bytearray(128)
	struct.pack_into("<IiiHHII", data, 0, 124, 1, 1, 1, 32, 0, 4)
	return bytes(data)


def _createImageReadMonitor(image: object) -> object:
	"""Create a monitor whose open-clipboard read returns one image and no other content."""
	monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
	monitor._formats = SimpleNamespace(
		html=101,
		rtf=102,
		excludeMonitor=105,
		canIncludeHistory=106,
		canUpload=107,
	)
	monitor._openClipboardWithRetry = Mock(return_value=True)
	monitor._isFormatAvailable = Mock(return_value=False)
	monitor._readPolicy = Mock(return_value=True)
	monitor._readTextIfAvailable = Mock(return_value=None)
	monitor._readOptionalFormat = Mock(return_value=(None, False))
	monitor._readImageFromOpenClipboard = Mock(return_value=(image, None, False))
	monitor.getSequenceNumber = Mock(return_value=42)
	return monitor


def _buildRawDropFilesData(pathData: bytes, *, isWide: bool, offset: int = 0) -> bytes:
	"""Build CF_HDROP data with an arbitrary path list placed ``offset`` bytes after the header."""
	headerSize = ctypes.sizeof(clipboardMonitor._DROPFILES)
	header = clipboardMonitor._DROPFILES(pFiles=headerSize + offset, ptX=0, ptY=0, fNC=False, fWide=isWide)
	return bytes(header) + pathData


class ClipboardMonitorTests(unittest.TestCase):
	"""Verify best-effort clipboard transaction snapshots."""

	def testMessageWindowsAreUniquePerMonitor(self) -> None:
		"""Allow multiple clipboard monitors to listen concurrently."""
		firstMonitor = Mock()
		secondMonitor = Mock()
		with patch.object(clipboardMonitor._ClipboardMessageWindow, "__init__", return_value=None):
			firstWindow = clipboardMonitor._createClipboardMessageWindow(firstMonitor)
			secondWindow = clipboardMonitor._createClipboardMessageWindow(secondMonitor)
		self.assertIsNot(type(firstWindow), type(secondWindow))
		self.assertNotEqual(firstWindow.className, secondWindow.className)

	def testExplorerCopyDropEffectIsNotLink(self) -> None:
		"""Treat Explorer's combined copy and link flags as an ordinary copy."""
		combinedCopy = clipboardMonitor.ClipboardSnapshot(
			clipboardMonitor.ClipboardContentType.FILES,
			preferredDropEffect=clipboardMonitor._DROPEFFECT_COPY | clipboardMonitor._DROPEFFECT_LINK,
		)
		linkOnly = clipboardMonitor.ClipboardSnapshot(
			clipboardMonitor.ClipboardContentType.FILES,
			preferredDropEffect=clipboardMonitor._DROPEFFECT_LINK,
		)

		self.assertFalse(combinedCopy.filesWereLinked)
		self.assertTrue(linkOnly.filesWereLinked)

	def testProtectedContentStaysOpaqueWhilePartialContentRemainsWritable(self) -> None:
		"""Keep protected payloads opaque while restoring retained nonprotected text."""
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor._formats = SimpleNamespace(
			html=101,
			rtf=102,
			excludeMonitor=105,
			canIncludeHistory=106,
			canUpload=107,
		)
		monitor._openClipboardWithRetry = Mock(return_value=True)
		monitor._isFormatAvailable = Mock(
			side_effect=lambda formatId: formatId == monitor._formats.excludeMonitor,
		)
		monitor.getSequenceNumber = Mock(return_value=42)
		monitor._readTextIfAvailable = Mock(return_value="secret")
		monitor._readOptionalFormat = Mock(side_effect=((None, True), (None, False)))
		monitor._readImageFromOpenClipboard = Mock(return_value=(None, None, True))

		with patch.object(clipboardMonitor, "_closeClipboard", return_value=True):
			protected = monitor.readNow()
			with self.assertRaises(TypeError):
				monitor.readNow(includeExcluded=True)
			monitor._readTextIfAvailable.assert_not_called()
			monitor._isFormatAvailable = Mock(return_value=False)
			partial = monitor.readNow()

		self.assertEqual(clipboardMonitor.ClipboardContentType.PROTECTED, protected.contentType)
		self.assertFalse(protected.canIncludeInHistory)
		self.assertFalse(protected.canUpload)
		self.assertEqual(clipboardMonitor.ClipboardContentType.TEXT_AND_IMAGE, partial.contentType)
		self.assertEqual("secret", partial.text)
		self.assertIsNone(partial.imageData)
		self.assertTrue(partial.richFormatsDropped)
		self.assertEqual(
			[(clipboardMonitor.CF_UNICODETEXT, "secret".encode("utf-16-le") + b"\0\0")],
			monitor._buildWritableFormats(partial),
		)

	def testWritableUnicodeTextUsesWin32LineEndings(self) -> None:
		"""Publish CF_UNICODETEXT with CRLF line endings."""
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor._formats = SimpleNamespace(canIncludeHistory=101, canUpload=102)
		snapshot = clipboardMonitor.ClipboardSnapshot(
			clipboardMonitor.ClipboardContentType.TEXT,
			text="first\nsecond\rthird\r\nfourth",
		)

		self.assertEqual(
			[
				(
					clipboardMonitor.CF_UNICODETEXT,
					"first\r\nsecond\r\nthird\r\nfourth".encode("utf-16-le") + b"\0\0",
				),
			],
			monitor._buildWritableFormats(snapshot),
		)

	def testImageAnalysisRunsOnlyForWorkerReadsAfterClipboardClose(self) -> None:
		"""Keep synchronous DIB reads fast and analyze worker images only after clipboard close."""
		dibData = _buildDibV5()
		monitor = _createImageReadMonitor(clipboardMonitor._ClipboardImage("DIBV5", dibData, (1, 1, 32)))
		monitor._readDibImageFromOpenClipboard = Mock(
			return_value=(clipboardMonitor._ClipboardImage("DIBV5", dibData, (1, 1, 32)), None, True),
		)
		clipboardIsClosed = False

		def closeClipboard() -> bool:
			"""Record that the mocked clipboard was released."""
			nonlocal clipboardIsClosed
			clipboardIsClosed = True
			return True

		def analyzeImage(
			imageFormat: str,
			_data: bytes,
			_info: tuple[int, int, int],
		) -> SimpleNamespace | None:
			"""Reject PNG and describe fallback DIB after verifying that the clipboard is closed."""
			self.assertTrue(clipboardIsClosed)
			return (
				None
				if imageFormat == "PNG"
				else SimpleNamespace(color=(0, 0, 0), colorPercentage=100.0, transparentPercentage=0.0)
			)

		def checkDibHeader(_data: bytes, _info: tuple[int, int, int]) -> bool:
			"""Accept the DIB header after verifying that the clipboard is closed."""
			self.assertTrue(clipboardIsClosed)
			return True

		with (
			patch.object(clipboardMonitor, "_closeClipboard", side_effect=closeClipboard),
			patch.object(clipboardMonitor, "getImageProperties", side_effect=analyzeImage) as analyzer,
			patch.object(clipboardMonitor, "isDibImageSupported", side_effect=checkDibHeader) as headerCheck,
		):
			synchronousSnapshot = monitor.readNow()
			analyzer.assert_not_called()
			headerCheck.assert_called_once_with(dibData, (1, 1, 32))
			monitor._readImageFromOpenClipboard.return_value = (
				clipboardMonitor._ClipboardImage("PNG", b"png", (1, 1, 32)),
				None,
				False,
			)
			clipboardIsClosed = False
			snapshot = monitor.readNow(analyzeImage=True)

		self.assertEqual("DIBV5", synchronousSnapshot.imageFormat)
		self.assertIsNone(synchronousSnapshot.imageColor)
		self.assertEqual("DIBV5", snapshot.imageFormat)
		self.assertEqual(dibData, snapshot.imageData)
		self.assertEqual((0, 0, 0), snapshot.imageColor)
		monitor._readDibImageFromOpenClipboard.assert_called_once_with()

	def testSynchronousReadsVerifyPngBeforeRetainingIt(self) -> None:
		"""Retain decodable PNG and replace undecodable PNG with the same sequence's DIB."""
		dibData = _buildDibV5()
		monitor = _createImageReadMonitor(clipboardMonitor._ClipboardImage("PNG", b"png", (1, 1, 24)))
		monitor._readDibImageFromOpenClipboard = Mock(
			return_value=(clipboardMonitor._ClipboardImage("DIBV5", dibData, (1, 1, 32)), None, True),
		)

		with (
			patch.object(clipboardMonitor, "_closeClipboard", return_value=True),
			patch.object(clipboardMonitor, "getImageProperties") as analyzer,
			patch.object(clipboardMonitor, "isDibImageSupported", return_value=True),
			patch.object(clipboardMonitor, "isPngImageDecodable", return_value=True) as pngCheck,
		):
			decodableSnapshot = monitor.readNow()
			monitor._readDibImageFromOpenClipboard.assert_not_called()
			pngCheck.return_value = False
			fallbackSnapshot = monitor.readNow()

		pngCheck.assert_called_with(b"png", (1, 1, 24))
		analyzer.assert_not_called()
		self.assertEqual(("PNG", b"png"), (decodableSnapshot.imageFormat, decodableSnapshot.imageData))
		self.assertEqual(("DIBV5", dibData), (fallbackSnapshot.imageFormat, fallbackSnapshot.imageData))
		self.assertEqual(32, fallbackSnapshot.imageBitDepth)
		monitor._readDibImageFromOpenClipboard.assert_called_once_with()
		self.assertEqual(3, monitor._openClipboardWithRetry.call_count)

	def testUndecodablePngIsDroppedWhenClipboardChangedBeforeFallback(self) -> None:
		"""Keep only image metadata when the DIB fallback sequence no longer matches."""
		monitor = _createImageReadMonitor(clipboardMonitor._ClipboardImage("PNG", b"png", (3, 2, 24)))
		monitor._readDibImageFromOpenClipboard = Mock()

		def closeClipboard() -> bool:
			"""Simulate another application replacing the clipboard after the first read."""
			monitor.getSequenceNumber.return_value = 43
			return True

		with (
			patch.object(clipboardMonitor, "_closeClipboard", side_effect=closeClipboard),
			patch.object(clipboardMonitor, "isPngImageDecodable", return_value=False),
		):
			snapshot = monitor.readNow()

		self.assertEqual(clipboardMonitor.ClipboardContentType.IMAGE, snapshot.contentType)
		self.assertIsNone(snapshot.imageFormat)
		self.assertIsNone(snapshot.imageData)
		self.assertEqual((3, 2, 24), (snapshot.imageWidth, snapshot.imageHeight, snapshot.imageBitDepth))
		monitor._readDibImageFromOpenClipboard.assert_not_called()
		monitor._openClipboardWithRetry.assert_called_once_with(None)

	def testUnconvertibleDibIsReplacedByBitmapOnReadPath(self) -> None:
		"""Convert CF_BITMAP at read time when Pillow cannot convert the retained DIB."""
		dibData = _buildDibV5()
		bitmapImage = clipboardMonitor._ClipboardImage("DIB", b"converted", (1, 1, 24), fromBitmap=True)
		monitor = _createImageReadMonitor(clipboardMonitor._ClipboardImage("DIBV5", dibData, (1, 1, 32)))
		monitor._readBitmapImageFromOpenClipboard = Mock(return_value=(bitmapImage, (1, 1, 24)))

		with (
			patch.object(clipboardMonitor, "_closeClipboard", return_value=True),
			patch.object(clipboardMonitor, "isDibImageSupported", return_value=False),
			patch.object(clipboardMonitor, "getImageProperties", return_value=None) as analyzer,
		):
			synchronousSnapshot = monitor.readNow()
			analyzedSnapshot = monitor.readNow(analyzeImage=True)

		for snapshot in (synchronousSnapshot, analyzedSnapshot):
			self.assertEqual(("DIB", b"converted"), (snapshot.imageFormat, snapshot.imageData))
			self.assertEqual((1, 1, 24), (snapshot.imageWidth, snapshot.imageHeight, snapshot.imageBitDepth))
			self.assertIsNone(snapshot.imageColor)
		# The converted bitmap stays unanalyzed, matching the former history-time conversion.
		analyzer.assert_called_once_with("DIBV5", dibData, (1, 1, 32))
		self.assertEqual(2, monitor._readBitmapImageFromOpenClipboard.call_count)

	def testUnconvertibleDibIsKeptWhenBitmapFallbackIsUnavailable(self) -> None:
		"""Keep the original DIB when no bitmap conversion can replace it."""
		dibData = _buildDibV5()
		monitor = _createImageReadMonitor(clipboardMonitor._ClipboardImage("DIBV5", dibData, (1, 1, 32)))
		monitor._readBitmapImageFromOpenClipboard = Mock(return_value=(None, None))

		with (
			patch.object(clipboardMonitor, "_closeClipboard", return_value=True),
			patch.object(clipboardMonitor, "isDibImageSupported", return_value=False),
		):
			snapshot = monitor.readNow()

		self.assertEqual(("DIBV5", dibData), (snapshot.imageFormat, snapshot.imageData))
		monitor._readBitmapImageFromOpenClipboard.assert_called_once_with()

	def testBitmapFallbackIsSkippedForConvertedBitmapsAndUnsafeSizes(self) -> None:
		"""Trust canonical bitmap conversions and leave oversized DIBs to normalization."""
		bitmapImage = clipboardMonitor._ClipboardImage("DIB", b"converted", (1, 1, 24), fromBitmap=True)
		monitor = _createImageReadMonitor(bitmapImage)
		monitor._readBitmapImageFromOpenClipboard = Mock()

		with (
			patch.object(clipboardMonitor, "_closeClipboard", return_value=True),
			patch.object(clipboardMonitor, "isDibImageSupported", return_value=False) as headerCheck,
		):
			bitmapSnapshot = monitor.readNow()
			headerCheck.assert_not_called()
			dibData = _buildDibV5()
			monitor._readImageFromOpenClipboard.return_value = (
				clipboardMonitor._ClipboardImage("DIBV5", dibData, (1, 1, 32)),
				None,
				False,
			)
			with patch.object(clipboardMonitor, "isImageSizeSafe", return_value=False):
				oversizedSnapshot = monitor.readNow()

		self.assertEqual(b"converted", bitmapSnapshot.imageData)
		self.assertEqual(dibData, oversizedSnapshot.imageData)
		monitor._readBitmapImageFromOpenClipboard.assert_not_called()
		self.assertEqual(2, monitor._openClipboardWithRetry.call_count)

	def testOpenClipboardChainFallsBackFromPngToDibV5ToDibToBitmap(self) -> None:
		"""Read PNG, DIBV5, DIB, then bitmap formats while skipping structurally invalid data."""
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor._formats = SimpleNamespace(png=100)
		availableFormats = {
			100,
			clipboardMonitor.CF_DIBV5,
			clipboardMonitor.CF_DIB,
			clipboardMonitor.CF_BITMAP,
		}
		monitor._isFormatAvailable = Mock(side_effect=lambda formatId: formatId in availableFormats)
		dibV5Data = _buildDibV5()
		dibData = struct.pack("<I", 40) + dibV5Data[4:40] + b"\0" * 4
		payloads = {100: b"png", clipboardMonitor.CF_DIBV5: dibV5Data, clipboardMonitor.CF_DIB: dibData}
		monitor._readGlobalData = Mock(side_effect=lambda formatId, _maximumBytes: payloads[formatId])
		bitmapImage = clipboardMonitor._ClipboardImage("DIB", b"converted", (1, 1, 24), fromBitmap=True)
		monitor._readBitmapImageFromOpenClipboard = Mock(return_value=(bitmapImage, (1, 1, 24)))

		with patch.object(clipboardMonitor, "getPngImageInfo", return_value=(1, 1, 24)):
			pngResult = monitor._readImageFromOpenClipboard()
		with patch.object(clipboardMonitor, "getPngImageInfo", return_value=None):
			dibV5Result = monitor._readImageFromOpenClipboard()
			payloads[clipboardMonitor.CF_DIBV5] = b"invalid DIBV5"
			dibResult = monitor._readImageFromOpenClipboard()
			payloads[clipboardMonitor.CF_DIB] = b"invalid DIB"
			bitmapResult = monitor._readImageFromOpenClipboard()
			monitor._readBitmapImageFromOpenClipboard.return_value = (None, (1, 1, 8))
			droppedResult = monitor._readImageFromOpenClipboard()

		self.assertEqual(
			(clipboardMonitor._ClipboardImage("PNG", b"png", (1, 1, 24)), None, False),
			pngResult,
		)
		self.assertEqual(
			(clipboardMonitor._ClipboardImage("DIBV5", dibV5Data, (1, 1, 32)), None, False),
			dibV5Result,
		)
		self.assertEqual(
			(clipboardMonitor._ClipboardImage("DIB", dibData, (1, 1, 32)), None, False),
			dibResult,
		)
		self.assertEqual((bitmapImage, (1, 1, 24), False), bitmapResult)
		self.assertEqual((None, (1, 1, 8), True), droppedResult)

	def testSourceDibDoesNotCopySynthesizedBitmap(self) -> None:
		"""Retain exact DIBV5 bytes without materializing a duplicate bitmap copy."""
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor._formats = SimpleNamespace(png=100)
		monitor._isFormatAvailable = Mock(
			side_effect=lambda formatId: formatId in (clipboardMonitor.CF_DIBV5, clipboardMonitor.CF_BITMAP),
		)
		dibData = _buildDibV5()
		monitor._readGlobalData = Mock(return_value=dibData)
		monitor._readBitmapImageFromOpenClipboard = Mock()

		result = monitor._readImageFromOpenClipboard()

		self.assertEqual(
			(clipboardMonitor._ClipboardImage("DIBV5", dibData, (1, 1, 32)), None, False),
			result,
		)
		monitor._readBitmapImageFromOpenClipboard.assert_not_called()

	def testBitmapConversionMarksCanonicalDib(self) -> None:
		"""Mark CF_BITMAP conversions so the fallback chain ends with them."""
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)

		def getObject(_handle: int, _size: int, bitmapReference: object) -> int:
			"""Describe a 2-by-1 32-bit bitmap."""
			bitmap = bitmapReference._obj
			bitmap.bmWidth = 2
			bitmap.bmHeight = 1
			bitmap.bmPlanes = 1
			bitmap.bmBitsPixel = 32
			return 1

		with (
			patch.object(clipboardMonitor, "_getClipboardData", return_value=5),
			patch.object(clipboardMonitor, "_getObject", side_effect=getObject),
			patch.object(clipboardMonitor, "_bitmapHandleToDib", return_value=b"converted") as converter,
		):
			image, imageInfo = monitor._readBitmapImageFromOpenClipboard()
			converter.return_value = None
			_missingImage, missingInfo = monitor._readBitmapImageFromOpenClipboard()

		self.assertEqual(
			clipboardMonitor._ClipboardImage("DIB", b"converted", (2, 1, 24), fromBitmap=True),
			image,
		)
		self.assertEqual((2, 1, 24), imageInfo)
		self.assertEqual((2, 1, 32), missingInfo)
		converter.assert_called_with(5, (2, 1, 32))

	def testImageFallbackReadRequiresMatchingSequence(self) -> None:
		"""Read a fallback image only while the expected clipboard item remains current."""
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor.getSequenceNumber = Mock(return_value=42)
		monitor._openClipboardWithRetry = Mock(return_value=True)
		image = clipboardMonitor._ClipboardImage("DIB", b"converted", (1, 1, 24), fromBitmap=True)
		reader = Mock(return_value=image)

		with patch.object(clipboardMonitor, "_closeClipboard", return_value=True) as closeClipboard:
			self.assertIsNone(monitor._readImageForSequence(0, reader))
			self.assertIsNone(monitor._readImageForSequence(41, reader))
			monitor._openClipboardWithRetry.assert_not_called()
			self.assertIs(image, monitor._readImageForSequence(42, reader))
			monitor.getSequenceNumber.side_effect = (42, 43)
			self.assertIsNone(monitor._readImageForSequence(42, reader))

		reader.assert_called_once_with()
		self.assertEqual(2, monitor._openClipboardWithRetry.call_count)
		monitor._openClipboardWithRetry.assert_called_with(None)
		self.assertEqual(2, closeClipboard.call_count)

	def testWritableFormatsUseSourceDib(self) -> None:
		"""Write the source DIBV5 bytes without materializing another representation."""
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor._formats = SimpleNamespace(
			png=100,
			preferredDropEffect=101,
			canIncludeHistory=102,
			canUpload=103,
		)
		dibData = _buildDibV5()
		snapshot = clipboardMonitor.ClipboardSnapshot(
			clipboardMonitor.ClipboardContentType.IMAGE,
			imageFormat="DIBV5",
			imageData=dibData,
			imageWidth=1,
			imageHeight=1,
			imageBitDepth=32,
		)

		self.assertEqual(
			[(clipboardMonitor.CF_DIBV5, dibData)],
			monitor._buildWritableFormats(snapshot),
		)

	def testWritablePngIncludesPillowDibFallback(self) -> None:
		"""Publish a Pillow-decoded DIB fallback alongside registered PNG data."""
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor._formats = SimpleNamespace(
			png=100,
			preferredDropEffect=101,
			canIncludeHistory=102,
			canUpload=103,
		)
		snapshot = clipboardMonitor.ClipboardSnapshot(
			clipboardMonitor.ClipboardContentType.IMAGE,
			imageFormat="PNG",
			imageData=b"png",
			imageWidth=1,
			imageHeight=1,
			imageBitDepth=24,
		)

		with (
			patch.object(clipboardMonitor, "getPngImageInfo", return_value=(1, 1, 24)),
			patch.object(clipboardMonitor, "pngToPackedDib", return_value=b"dib"),
		):
			formats = monitor._buildWritableFormats(snapshot)

		self.assertEqual([(100, b"png"), (clipboardMonitor.CF_DIB, b"dib")], formats)

	def testWritablePngAcceptsValidatedPreparedDibWithoutDecoding(self) -> None:
		"""Use a worker-prepared DIB after validating it on the clipboard thread."""
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor._formats = SimpleNamespace(
			png=100,
			preferredDropEffect=101,
			canIncludeHistory=102,
			canUpload=103,
		)
		snapshot = clipboardMonitor.ClipboardSnapshot(
			clipboardMonitor.ClipboardContentType.IMAGE,
			imageFormat="PNG",
			imageData=b"png",
			imageWidth=1,
			imageHeight=1,
			imageBitDepth=24,
		)
		dibData = _buildDibV5()

		with (
			patch.object(clipboardMonitor, "getPngImageInfo", return_value=(1, 1, 24)),
			patch.object(clipboardMonitor, "pngToPackedDib") as converter,
		):
			formats = monitor._buildWritableFormats(snapshot, preparedPngDib=dibData)
			with self.assertRaises(ValueError):
				monitor._buildWritableFormats(snapshot, preparedPngDib=b"invalid")

		converter.assert_not_called()
		self.assertEqual([(100, b"png"), (clipboardMonitor.CF_DIB, dibData)], formats)

	def testDispatchScheduleFailureReleasesReaderState(self) -> None:
		"""Allow a later clipboard update after wx rejects a worker result."""
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor._lock = clipboardMonitor.Lock()
		monitor._cancelToken = 0
		monitor._isRunning = True
		monitor._readInProgress = True
		monitor._readAgain = False
		monitor._onSnapshot = Mock()
		monitor.readNow = Mock(
			return_value=clipboardMonitor.ClipboardSnapshot(clipboardMonitor.ClipboardContentType.EMPTY),
		)

		with patch.object(
			_WX,
			"CallAfter",
			side_effect=RuntimeError("wx stopped"),
			create=True,
		):
			monitor._readAndDispatchSnapshot(0)

		self.assertFalse(monitor._readInProgress)
		self.assertFalse(monitor._readAgain)

	def testLightweightReadDoesNotFetchOptionalFormats(self) -> None:
		"""Use advertised formats for classification without triggering delayed rendering."""
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor._formats = SimpleNamespace(
			html=101,
			rtf=102,
			png=103,
			excludeMonitor=104,
			canIncludeHistory=105,
			canUpload=106,
		)
		monitor._openClipboardWithRetry = Mock(return_value=True)
		monitor._isFormatAvailable = Mock(
			side_effect=lambda formatId: formatId
			in {
				monitor._formats.html,
				monitor._formats.png,
			},
		)
		monitor._readPolicy = Mock(return_value=True)
		monitor._readTextIfAvailable = Mock(return_value="plain")
		monitor._readImageFromOpenClipboard = Mock()
		monitor.getSequenceNumber = Mock(return_value=11)
		with (
			patch.object(clipboardMonitor, "_closeClipboard", return_value=True),
			patch.object(clipboardMonitor, "_getClipboardData") as getData,
		):
			snapshot = monitor.readNow(includeRichFormats=False, includeImages=False)

		self.assertEqual(clipboardMonitor.ClipboardContentType.TEXT_AND_IMAGE, snapshot.contentType)
		self.assertEqual("plain", snapshot.text)
		getData.assert_not_called()
		monitor._readImageFromOpenClipboard.assert_not_called()

	def testExhaustedRichBudgetNeverRequestsDelayedRtf(self) -> None:
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor._isFormatAvailable = Mock(return_value=True)
		with patch.object(clipboardMonitor, "_getClipboardData") as getData:
			self.assertEqual((None, True), monitor._readOptionalFormat(102, 0))
		getData.assert_not_called()

	def testCancelledReaderKeepsNewUpdatesSerialized(self) -> None:
		for updateBeforeCleanup in (False, True):
			with self.subTest(updateBeforeCleanup=updateBeforeCleanup):
				callback = Mock()
				with patch.object(clipboardMonitor, "_registerFormats", return_value=Mock()):
					monitor = clipboardMonitor.ClipboardMonitor(callback)
				monitor._isRunning = True
				stale = clipboardMonitor.ClipboardSnapshot(
					clipboardMonitor.ClipboardContentType.TEXT,
					sequenceNumber=1,
					text="old",
				)
				current = clipboardMonitor.ClipboardSnapshot(
					clipboardMonitor.ClipboardContentType.TEXT,
					sequenceNumber=2,
					text="new",
				)
				monitor.readNow = Mock(return_value=stale)
				with (
					patch.object(clipboardMonitor, "Thread") as readerThread,
					patch.object(
						_WX,
						"CallAfter",
						side_effect=lambda function, *args: function(*args),
						create=True,
					),
				):
					monitor.handleClipboardUpdate()
					oldTask = readerThread.call_args.kwargs
					monitor.invalidatePendingSnapshots()
					if updateBeforeCleanup:
						monitor.handleClipboardUpdate()
					else:
						# Deliver the next notification immediately after cancellation releases the lock.
						monitor._lock = MagicMock()

						def updateAfterUnlock(*_args):
							monitor._lock.__exit__.side_effect = None
							monitor.handleClipboardUpdate()

						monitor._lock.__exit__.side_effect = updateAfterUnlock
					oldTask["target"](*oldTask["args"])
					callback.assert_not_called()
					self.assertEqual(2, readerThread.call_count)
					newTask = readerThread.call_args.kwargs
					monitor.handleClipboardUpdate()
					self.assertEqual(2, readerThread.call_count)
					monitor.readNow.return_value = current
					newTask["target"](*newTask["args"])
					callback.assert_called_once_with(current)
					self.assertFalse(monitor._readInProgress)

	def testDropFilesUseTheSameUtf16LimitForReadingAndWriting(self) -> None:
		# Both paths occupy five UTF-16 code units, with or without surrogate pairs.
		for files in ((r"C:\ab", r"D:\cd"), (r"C:\😀", r"D:\𠀀")):
			data = clipboardMonitor._buildDropFilesData(files)
			monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
			monitor._readGlobalData = Mock(return_value=data)
			header = clipboardMonitor._DROPFILES.from_buffer_copy(data)
			self.assertEqual(files, tuple(data[header.pFiles :].decode("utf-16-le").split("\0")[:-2]))

			for limit in (9, 10, 11):
				with (
					self.subTest(files=files, limit=limit),
					patch.object(clipboardMonitor, "_MAX_FILE_PATH_CHARACTERS", limit),
				):
					if limit < 10:
						with self.assertRaises(ValueError):
							clipboardMonitor._buildDropFilesData(files)
						with self.assertRaises(clipboardMonitor._ClipboardDataLimitError):
							monitor._readFilesFromOpenClipboard()
					else:
						self.assertEqual(data, clipboardMonitor._buildDropFilesData(files))
						self.assertEqual(list(files), monitor._readFilesFromOpenClipboard())

	def testReadFilesReadsClipboardDataOnce(self) -> None:
		"""Read CF_HDROP through one bounded global-data copy."""
		files = (r"C:\one.txt", r"D:\two.txt")
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor._readGlobalData = Mock(return_value=clipboardMonitor._buildDropFilesData(files))
		with patch.object(clipboardMonitor, "_getClipboardData") as getClipboardData:
			self.assertEqual(list(files), monitor._readFilesFromOpenClipboard())
		getClipboardData.assert_not_called()
		monitor._readGlobalData.assert_called_once_with(
			clipboardMonitor.CF_HDROP,
			clipboardMonitor._MAX_DROPFILES_BYTES,
		)

	def testReadFilesReturnsEmptyListWhenGlobalDataIsUnavailable(self) -> None:
		"""Treat unreadable or oversized CF_HDROP data as an empty file list."""
		for error in (OSError(), clipboardMonitor._ClipboardDataLimitError(1)):
			with self.subTest(error=type(error).__name__):
				monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
				monitor._readGlobalData = Mock(side_effect=error)
				self.assertEqual([], monitor._readFilesFromOpenClipboard())

	def testParseDropFilesReadsWidePathsInOrder(self) -> None:
		"""Decode Unicode path lists in order, replacing lone surrogates like the old UTF-16 round trip."""
		files = (r"C:\b.txt", r"D:\😀\a.txt", r"\\server\share\é")
		self.assertEqual(
			list(files),
			clipboardMonitor._parseDropFilesPaths(clipboardMonitor._buildDropFilesData(files)),
		)
		data = _buildRawDropFilesData(
			"C:\\x\ud800y\0\0".encode("utf-16-le", errors="surrogatepass"),
			isWide=True,
		)
		self.assertEqual(["C:\\x\ufffdy"], clipboardMonitor._parseDropFilesPaths(data))

	def testParseDropFilesDecodesAnsiPathsWithTheAnsiCodePage(self) -> None:
		"""Decode non-wide path lists with the ANSI codec and count their UTF-16 lengths."""
		data = _buildRawDropFilesData(b"C:\\caf\xe9\0D:\\\x81x\0\0", isWide=False)
		with patch.object(clipboardMonitor, "_ANSI_PATH_ENCODING", "cp1252"):
			self.assertEqual(["C:\\café", "D:\\\ufffdx"], clipboardMonitor._parseDropFilesPaths(data))
			with patch.object(clipboardMonitor, "_MAX_FILE_PATH_CHARACTERS", 11):
				with self.assertRaises(clipboardMonitor._ClipboardDataLimitError):
					clipboardMonitor._parseDropFilesPaths(data)
			with patch.object(clipboardMonitor, "_MAX_FILE_PATH_CHARACTERS", 12):
				self.assertEqual(2, len(clipboardMonitor._parseDropFilesPaths(data)))

	def testParseDropFilesReturnsNoPathsForAnEmptyList(self) -> None:
		"""Stop at an immediate list terminator, as DragQueryFileW reports zero files."""
		self.assertEqual(
			[],
			clipboardMonitor._parseDropFilesPaths(_buildRawDropFilesData(b"\0\0\0\0", isWide=True)),
		)
		with patch.object(clipboardMonitor, "_ANSI_PATH_ENCODING", "cp1252"):
			self.assertEqual(
				[],
				clipboardMonitor._parseDropFilesPaths(_buildRawDropFilesData(b"\0\0", isWide=False)),
			)
		data = _buildRawDropFilesData("a\0\0\0b\0\0".encode("utf-16-le"), isWide=True)
		self.assertEqual(["a"], clipboardMonitor._parseDropFilesPaths(data))

	def testParseDropFilesRejectsMalformedData(self) -> None:
		"""Reject truncated headers, bad offsets, and unterminated or misaligned path lists."""
		valid = clipboardMonitor._buildDropFilesData((r"C:\a",))
		headerSize = ctypes.sizeof(clipboardMonitor._DROPFILES)
		cases = {
			"truncatedHeader": valid[: headerSize - 1],
			"unterminatedWide": valid[:-2],
			"misalignedWideTerminator": _buildRawDropFilesData(b"a\0\0\0\0b", isWide=True),
			"unterminatedAnsi": _buildRawDropFilesData(b"C:\\a\0", isWide=False),
			"offsetBeforeHeader": _buildRawDropFilesData(valid[headerSize:], isWide=True, offset=-2),
			"oddWideOffset": _buildRawDropFilesData(
				b"\0" + "a\0\0".encode("utf-16-le"),
				isWide=True,
				offset=1,
			),
			"offsetPastEnd": _buildRawDropFilesData(b"\0\0\0\0", isWide=True, offset=8),
		}
		for name, data in cases.items():
			with self.subTest(name=name):
				self.assertEqual([], clipboardMonitor._parseDropFilesPaths(data))

	def testParseDropFilesEnforcesTheFileCountLimit(self) -> None:
		"""Raise the clipboard limit error before decoding an oversized file list."""
		data = clipboardMonitor._buildDropFilesData(("a", "b", "c"))
		with patch.object(clipboardMonitor, "_MAX_FILE_COUNT", 2):
			with self.assertRaises(clipboardMonitor._ClipboardDataLimitError):
				clipboardMonitor._parseDropFilesPaths(data)
		with patch.object(clipboardMonitor, "_MAX_FILE_COUNT", 3):
			self.assertEqual(["a", "b", "c"], clipboardMonitor._parseDropFilesPaths(data))

	def testInvalidDropFilesErrorDoesNotContainPaths(self) -> None:
		"""Keep rejected clipboard file paths out of exception chains."""
		privatePath = r"C:\Users\private\secret.txt" + "\0invalid"
		with self.assertRaises(ValueError) as context:
			clipboardMonitor._buildDropFilesData((privatePath,))
		self.assertNotIn("private", str(context.exception))

	def testSnapshotRetainsHtmlWhenClipboardHasNoPlainText(self) -> None:
		"""Retain HTML-only clipboard data for the browseable viewer."""
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor._formats = SimpleNamespace(
			html=101,
			rtf=102,
			excludeMonitor=103,
			canIncludeHistory=104,
			canUpload=105,
		)
		monitor._openClipboardWithRetry = Mock(return_value=True)
		monitor._isFormatAvailable = Mock(side_effect=lambda formatId: formatId == monitor._formats.html)
		monitor._readPolicy = Mock(return_value=True)
		monitor._readTextIfAvailable = Mock(return_value=None)
		monitor._readOptionalFormat = Mock(
			side_effect=lambda formatId, _maximumBytes: (
				(b"<p>html</p>", False) if formatId == monitor._formats.html else (None, False)
			),
		)
		monitor._readImageFromOpenClipboard = Mock(return_value=(None, None, False))
		monitor.getSequenceNumber = Mock(return_value=1)
		with (
			patch.object(clipboardMonitor, "_countClipboardFormats", return_value=1),
			patch.object(clipboardMonitor, "_closeClipboard", return_value=True),
		):
			snapshot = monitor._readSnapshot(analyzeImage=False)
		self.assertEqual(clipboardMonitor.ClipboardContentType.UNSUPPORTED, snapshot.contentType)
		self.assertEqual(b"<p>html</p>", snapshot.html)

	def testControllerTranslatesOversizedTextAndResynchronizesMonitor(self) -> None:
		"""Translate a rejected text write and resume clipboard monitoring."""
		controllerPath = addonSourcePath("controller")
		tree = ast.parse(controllerPath.read_text(encoding="utf-8"))
		controllerClass = next(
			node
			for node in tree.body
			if isinstance(node, ast.ClassDef) and node.name == "ClipboardController"
		)
		writeSnapshotNode = next(
			node
			for node in controllerClass.body
			if isinstance(node, ast.FunctionDef) and node.name == "_writeSnapshot"
		)
		namespace = {
			"ClipboardSnapshot": clipboardMonitor.ClipboardSnapshot,
			"ClipboardChangeSource": object,
			"ClipboardSequenceChangedError": clipboardMonitor.ClipboardSequenceChangedError,
			"ClipboardWriteError": clipboardMonitor.ClipboardWriteError,
			"_": lambda message: message,
		}
		exec(
			compile(ast.Module(body=[writeSnapshotNode], type_ignores=[]), controllerPath, "exec"),
			namespace,
		)
		writeSnapshot = namespace["_writeSnapshot"]
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor.invalidatePendingSnapshots = Mock()
		monitor.handleClipboardUpdate = Mock()
		controller = SimpleNamespace(
			monitor=monitor,
			_getClipboardOwnerHandle=Mock(return_value=1),
		)
		snapshot = clipboardMonitor.ClipboardSnapshot(
			clipboardMonitor.ClipboardContentType.TEXT,
			text="ab",
		)

		with (
			patch.object(clipboardMonitor, "MAX_TEXT_BYTES", 4),
			self.assertRaisesRegex(RuntimeError, "^Could not write to the system clipboard$") as context,
		):
			writeSnapshot(controller, snapshot, object())

		self.assertEqual((clipboardMonitor.CF_UNICODETEXT,), context.exception.__cause__.args)
		monitor.invalidatePendingSnapshots.assert_called_once_with()
		monitor.handleClipboardUpdate.assert_called_once_with()


class SharedWinDllTests(unittest.TestCase):
	"""Keep add-on ctypes prototypes off the process-wide ``ctypes.windll`` libraries."""

	def testAddonDoesNotUseSharedWindll(self) -> None:
		"""No add-on module references ``windll``, whose function prototypes NVDA and other add-ons share."""
		packageDirectory = addonSourcePath("__init__").parent
		offenders = [
			str(path.relative_to(packageDirectory))
			for path in sorted(packageDirectory.rglob("*.py"))
			if "_vendor" not in path.relative_to(packageDirectory).parts
			and any(
				(isinstance(node, ast.Name) and node.id == "windll")
				or (isinstance(node, ast.Attribute) and node.attr == "windll")
				or (isinstance(node, ast.alias) and node.name == "windll")
				for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
			)
		]
		self.assertEqual([], offenders)

	def testClipboardMonitorUsesPrivateLastErrorLibraries(self) -> None:
		"""Load user32, kernel32 and gdi32 as private instances that capture the last error."""
		tree = ast.parse(addonSourcePath("clipboard.monitor").read_text(encoding="utf-8"))
		libraries = {
			node.args[0].value
			for node in ast.walk(tree)
			if isinstance(node, ast.Call)
			and isinstance(node.func, ast.Name)
			and node.func.id == "WinDLL"
			and isinstance(node.args[0], ast.Constant)
			and any(
				keyword.arg == "use_last_error"
				and isinstance(keyword.value, ast.Constant)
				and keyword.value.value is True
				for keyword in node.keywords
			)
		}
		self.assertEqual({"user32", "kernel32", "gdi32"}, libraries)
