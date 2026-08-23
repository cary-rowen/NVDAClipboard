"""Tests for protected clipboard transaction handling without loading NVDA."""

from __future__ import annotations

import ast
import ctypes
from pathlib import Path
import struct
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests._module_loader import loadAddonModule


_MODULE_DIRECTORY = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard"
_PACKAGE_NAME = "nvdaClipboardClipboardMonitorTests"
_PACKAGE = ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_MODULE_DIRECTORY)]
sys.modules[_PACKAGE_NAME] = _PACKAGE

with (
	patch.object(ctypes, "windll", Mock(), create=True),
	patch.object(ctypes, "WinError", lambda *_args: OSError(), create=True),
	patch.dict(
		sys.modules,
		{
			"logHandler": SimpleNamespace(log=Mock()),
			"winBindings": SimpleNamespace(gdi32=Mock(), kernel32=Mock(), user32=Mock()),
			"windowUtils": SimpleNamespace(CustomWindow=type("CustomWindow", (), {})),
			"wx": SimpleNamespace(),
		},
	),
):
	clipboardMonitor = loadAddonModule(
		f"{_PACKAGE_NAME}.clipboardMonitor",
		_MODULE_DIRECTORY / "clipboardMonitor.py",
	)


def _buildDibV5() -> bytes:
	"""Build one complete 1-by-1 32-bit DIBV5 payload."""
	data = bytearray(128)
	struct.pack_into("<IiiHHII", data, 0, 124, 1, 1, 1, 32, 0, 4)
	return bytes(data)


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
		monitor._readImageFromOpenClipboard = Mock(return_value=(None, None, None, True))

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

	def testImageAnalysisRunsOnlyForWorkerReadsAfterClipboardClose(self) -> None:
		"""Keep synchronous DIB reads fast and analyze worker images only after clipboard close."""
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
		dibData = _buildDibV5()
		monitor._readImageFromOpenClipboard = Mock(return_value=("DIBV5", dibData, (1, 1, 32), False))
		monitor._readDibImageForSequence = Mock(
			return_value=("DIBV5", dibData, (1, 1, 32), False),
		)
		monitor.getSequenceNumber = Mock(return_value=42)
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

		with (
			patch.object(clipboardMonitor, "_closeClipboard", side_effect=closeClipboard),
			patch.object(clipboardMonitor, "getImageProperties", side_effect=analyzeImage) as analyzer,
		):
			synchronousSnapshot = monitor.readNow(decodePng=True)
			analyzer.assert_not_called()
			monitor._readImageFromOpenClipboard.return_value = ("PNG", b"png", (1, 1, 32), False)
			clipboardIsClosed = False
			snapshot = monitor.readNow(analyzeImage=True)

		self.assertEqual("DIBV5", synchronousSnapshot.imageFormat)
		self.assertEqual("DIBV5", snapshot.imageFormat)
		self.assertEqual(dibData, snapshot.imageData)
		self.assertEqual((0, 0, 0), snapshot.imageColor)
		monitor._readDibImageForSequence.assert_called_once_with(42)

	def testSourceDibDoesNotCopySynthesizedBitmap(self) -> None:
		"""Retain exact DIBV5 bytes without materializing a duplicate bitmap copy."""
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor._formats = SimpleNamespace(png=100)
		monitor._isFormatAvailable = Mock(
			side_effect=lambda formatId: formatId in (clipboardMonitor.CF_DIBV5, clipboardMonitor.CF_BITMAP),
		)
		dibData = _buildDibV5()
		monitor._readGlobalData = Mock(return_value=dibData)
		monitor._readBitmapFromOpenClipboard = Mock(return_value=(b"converted", (1, 1, 24)))

		result = monitor._readImageFromOpenClipboard()

		self.assertEqual(("DIBV5", dibData, (1, 1, 32), False), result)
		monitor._readBitmapFromOpenClipboard.assert_not_called()

	def testBitmapFallbackRequiresMatchingSequence(self) -> None:
		"""Read a canonical bitmap only while the expected clipboard item remains current."""
		monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
		monitor.getSequenceNumber = Mock(return_value=42)
		monitor._openClipboardWithRetry = Mock(return_value=True)
		monitor._readBitmapFromOpenClipboard = Mock(return_value=(b"converted", (1, 1, 24)))

		with patch.object(clipboardMonitor, "_closeClipboard", return_value=True):
			self.assertIsNone(monitor.readBitmapDib(41))
			self.assertEqual((b"converted", (1, 1, 24)), monitor.readBitmapDib(42))

		monitor._openClipboardWithRetry.assert_called_once_with(None)
		monitor._readBitmapFromOpenClipboard.assert_called_once_with()

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
			clipboardMonitor.wx,
			"CallAfter",
			side_effect=RuntimeError("wx stopped"),
			create=True,
		):
			monitor._readAndDispatchSnapshot(0)

		self.assertFalse(monitor._readInProgress)
		self.assertFalse(monitor._readAgain)

	def testInvalidDropFilesErrorDoesNotContainPaths(self) -> None:
		"""Keep rejected clipboard file paths out of exception chains."""
		privatePath = r"C:\Users\private\secret.txt" + "\0invalid"
		with self.assertRaises(ValueError) as context:
			clipboardMonitor._buildDropFilesData((privatePath,))
		self.assertNotIn("private", str(context.exception))

	def testControllerTranslatesOversizedTextAndResynchronizesMonitor(self) -> None:
		"""Translate a rejected text write and resume clipboard monitoring."""
		controllerPath = _MODULE_DIRECTORY / "controller.py"
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
			"_ClipboardChangeSource": object,
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
