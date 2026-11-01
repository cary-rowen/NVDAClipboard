# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for the standalone clipboard file size calculator."""

from __future__ import annotations

from ctypes import WinDLL, c_longlong, create_unicode_buffer
from ctypes.wintypes import LPWSTR, UINT
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore, Event
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from tests._manager_loader import fakeNvdaModules
from tests._module_loader import createAddonPackage, loadAddonModule


_PACKAGE_NAME = "nvdaClipboardFileSizeTests"
createAddonPackage(_PACKAGE_NAME)
_WIN_BINDINGS = ModuleType("winBindings")
_WIN_BINDINGS.kernel32 = SimpleNamespace()
fileSize = loadAddonModule(
	f"{_PACKAGE_NAME}.summary.fileSize",
	injectedModules={
		"logHandler": SimpleNamespace(log=Mock()),
		"winBindings": _WIN_BINDINGS,
		"wx": SimpleNamespace(CallAfter=lambda *_args: None),
	},
)
formatting = loadAddonModule(
	f"{_PACKAGE_NAME}.summary.formatting",
	injectedModules={
		**fakeNvdaModules(extraModuleNames=("colors",)),
		f"{_PACKAGE_NAME}.clipboard.monitor": SimpleNamespace(ClipboardContentType=object),
	},
)


class FileSizeCalculationTests(unittest.TestCase):
	"""Verify file size calculation without loading NVDA."""

	def setUp(self) -> None:
		self.kernel32 = SimpleNamespace(
			OpenThread=Mock(return_value=100),
			GetCurrentThreadId=Mock(return_value=1),
			CloseHandle=Mock(return_value=True),
		)
		self.enterContext(patch.object(fileSize, "kernel32", self.kernel32))
		self.enterContext(patch.object(fileSize, "_scanSlots", BoundedSemaphore(2)))
		self.cancelIo = self.enterContext(patch.object(fileSize, "_cancelSynchronousIo", return_value=True))

	def _run(self, calculation: fileSize.FileSizeCalculation) -> fileSize.FileSizeProgress:
		finished = Event()
		self.assertTrue(calculation.start(onComplete=lambda _calculation: finished.set()))
		self.assertTrue(finished.wait(2), "Scan did not exit")
		return calculation.getProgress()

	def testFilesAndFoldersAreCounted(self) -> None:
		"""Count selected files, folders, and their nested contents."""
		with TemporaryDirectory() as temporaryDirectory:
			root = Path(temporaryDirectory)
			selectedDirectory = root / "selected"
			selectedDirectory.mkdir()
			(selectedDirectory / "first.bin").write_bytes(b"123")
			nested = selectedDirectory / "nested"
			nested.mkdir()
			(nested / "second.bin").write_bytes(b"45678")
			(selectedDirectory / "empty").mkdir()
			looseFile = root / "empty.bin"
			looseFile.write_bytes(b"")

			calculation = fileSize.FileSizeCalculation((str(looseFile), str(selectedDirectory)))
			completionEvent = Event()
			self.assertTrue(calculation.start(onComplete=lambda _calculation: completionEvent.set()))
			self.assertFalse(calculation.start())
			self.assertTrue(completionEvent.wait(2), "File size completion was not reported")
			progress = calculation.getProgress()

		self.assertEqual(8, progress.byteCount)
		self.assertEqual(3, progress.fileCount)
		self.assertEqual(3, progress.directoryCount)
		self.assertTrue(progress.isComplete)
		self.assertFalse(progress.isIncomplete)

	def testSingleFolderCountsOnlyItsContents(self) -> None:
		"""Exclude the selected folder itself, including when it is empty."""
		with TemporaryDirectory() as temporaryDirectory:
			root = Path(temporaryDirectory)
			empty = self._run(fileSize.FileSizeCalculation((str(root),)))
			self.assertEqual(0, empty.directoryCount)
			self.assertEqual(
				f"Files; contents total {formatting.formatDataSize(0)}",
				formatting.formatFileSizeSummary("Files", empty),
			)
			nested = root / "nested"
			nested.mkdir()
			(nested / "file.bin").write_bytes(b"1234")
			progress = self._run(fileSize.FileSizeCalculation((str(root),)))
		self.assertEqual((4, 1, 1), (progress.byteCount, progress.fileCount, progress.directoryCount))
		self.assertTrue(progress.isComplete)
		self.assertFalse(progress.isIncomplete)

	def testMissingAndUnreadablePathsKeepPartialResults(self) -> None:
		"""Keep readable file sizes when another item is missing or inaccessible."""
		with TemporaryDirectory() as temporaryDirectory:
			root = Path(temporaryDirectory)
			localFile = root / "local.bin"
			localFile.write_bytes(b"1234")
			missingFile = root / "missing.bin"
			with patch.object(fileSize.os, "scandir", side_effect=PermissionError()):
				calculation = fileSize.FileSizeCalculation(
					(str(localFile), str(missingFile), str(root)),
				)
				progress = self._run(calculation)

		self.assertEqual(4, progress.byteCount)
		self.assertEqual(1, progress.fileCount)
		self.assertEqual(1, progress.directoryCount)
		self.assertTrue(progress.isIncomplete)

	def testNetworkPathsUseTheSameDirectoryScan(self) -> None:
		"""Both UNC and mapped paths reach enumeration and include their contents."""
		with TemporaryDirectory() as temporaryDirectory:
			root = Path(temporaryDirectory)
			(root / "file.bin").write_bytes(b"1234")
			rootStat = root.stat()
			scandir = os.scandir
			for path in (r"\\server\share\folder", r"Z:\folder"):
				with (
					self.subTest(path=path),
					patch.object(fileSize.os, "stat", return_value=rootStat) as pathStat,
					patch.object(fileSize.os, "scandir", side_effect=lambda _path: scandir(root)) as scan,
				):
					progress = self._run(fileSize.FileSizeCalculation((path,)))
					pathStat.assert_called_once_with(path, follow_symlinks=False)
					scan.assert_called_once_with(path)
					self.assertEqual(
						(4, 1, 0), (progress.byteCount, progress.fileCount, progress.directoryCount)
					)
					self.assertFalse(progress.isIncomplete)

	def testStalledScansKeepTheirSlotsUntilTheyExit(self) -> None:
		"""Freeze partial results, allow one replacement, and reject further work until cleanup."""
		entered = [Event(), Event()]
		release = Event()
		finished = [Event(), Event()]
		clock = Mock(return_value=100.0)
		pathStat = os.stat

		def blockedStat(path: str, *, follow_symlinks: bool) -> os.stat_result:
			if path in ("blocked-0", "blocked-1"):
				entered[int(path[-1])].set()
				if not release.wait(5):
					raise TimeoutError("Test did not release blocked I/O")
				path = __file__
			return pathStat(path, follow_symlinks=follow_symlinks)

		with TemporaryDirectory() as temporaryDirectory:
			readable = Path(temporaryDirectory) / "file.bin"
			readable.write_bytes(b"1234")
			first = fileSize.FileSizeCalculation((str(readable), "blocked-0"))
			second = fileSize.FileSizeCalculation(("blocked-1",))
			with (
				patch.object(fileSize, "monotonic", clock),
				patch.object(fileSize.os, "stat", side_effect=blockedStat),
			):
				try:
					first.start(onComplete=lambda _calculation: finished[0].set())
					self.assertTrue(entered[0].wait(2))
					clock.return_value = 131.0
					first.poll()
					frozen = first.getProgress()
					self.assertEqual((4, 1), (frozen.byteCount, frozen.fileCount))
					self.assertTrue(frozen.isComplete and frozen.isIncomplete)

					second.start(onComplete=lambda _calculation: finished[1].set())
					self.assertTrue(entered[1].wait(2), "Replacement scan could not start")
					second.cancel()
					self.assertEqual(2, self.cancelIo.call_count)
					self.kernel32.CloseHandle.assert_not_called()
					rejected = self._run(fileSize.FileSizeCalculation((str(readable),)))
					self.assertTrue(rejected.isComplete and rejected.isIncomplete)
					self.assertEqual(0, rejected.fileCount)
					self.assertFalse(first.start(), "A finished result must not silently retry")
				finally:
					release.set()
					for completion in finished:
						self.assertTrue(completion.wait(2), "Blocked scan did not exit after release")

			self.assertEqual(frozen, first.getProgress(), "Late I/O overwrote the finished result")
			first.cancel()
			second.cancel()
			self.assertEqual(2, self.cancelIo.call_count, "Cancellation used a closed thread handle")
			progress = self._run(fileSize.FileSizeCalculation((str(readable),)))
			self.assertEqual(4, progress.byteCount)
			self.assertFalse(progress.isIncomplete)

	def testCancellationIsRetriedWhenIoStartsAfterTheFirstRequest(self) -> None:
		"""Cancellation between the loop check and the native read must not leave that read blocked."""
		beforeRead, allowRead, reading, cancelled, finished = (Event() for _ in range(5))
		directory = MagicMock()

		def readNext():
			beforeRead.set()
			if not allowRead.wait(5):
				raise TimeoutError("Test did not allow the read to start")
			reading.set()
			if not cancelled.wait(5):
				raise TimeoutError("Test did not release the read")
			raise OSError(995, "I/O cancelled")

		def cancelRead(_handle: int) -> bool:
			if not reading.is_set():
				return False
			cancelled.set()
			return True

		directory.__next__.side_effect = readNext
		self.cancelIo.side_effect = cancelRead
		with (
			TemporaryDirectory() as temporaryDirectory,
			patch.object(fileSize.os, "scandir", return_value=directory),
			patch.object(fileSize, "get_last_error", return_value=1168),
		):
			calculation = fileSize.FileSizeCalculation((temporaryDirectory,))
			try:
				self.assertTrue(calculation.start(lambda _calculation: finished.set()))
				self.assertTrue(beforeRead.wait(2))
				calculation.cancel()
				self.assertEqual(1, self.cancelIo.call_count)
				allowRead.set()
				self.assertTrue(reading.wait(2))
				self.assertTrue(calculation.poll())
				self.assertEqual(2, self.cancelIo.call_count, "The pending read was never cancelled")
			finally:
				allowRead.set()
				cancelled.set()
				self.assertTrue(finished.wait(2), "Cancelled scan did not exit")
			directory.close.assert_called_once_with()
			self.assertFalse(calculation.poll())
			self.assertEqual(2, self.cancelIo.call_count, "Polling used a closed thread handle")

	def testDirectoryTimeoutKeepsFilesReadBeforeTheStall(self) -> None:
		"""Keep counts read in the first 250 ms when the next directory read stalls."""
		clock = Mock(return_value=100.0)
		reading, release, finished = Event(), Event(), Event()
		scandir = os.scandir

		def stalledEntries(path: str):
			with scandir(path) as entries:
				yield next(entries)
				reading.set()
				if not release.wait(5):
					raise TimeoutError("Test did not release directory enumeration")

		with TemporaryDirectory() as temporaryDirectory:
			(Path(temporaryDirectory) / "readable.bin").write_bytes(b"1234")
			calculation = fileSize.FileSizeCalculation((temporaryDirectory,))
			with (
				patch.object(fileSize, "monotonic", clock),
				patch.object(fileSize.os, "scandir", side_effect=stalledEntries),
			):
				try:
					self.assertTrue(calculation.start(lambda _calculation: finished.set()))
					self.assertTrue(reading.wait(2))
					clock.return_value = 131.0
					calculation.poll()
					progress = calculation.getProgress()
					self.assertEqual((4, 1), (progress.byteCount, progress.fileCount))
					self.assertTrue(progress.isComplete and progress.isIncomplete)
				finally:
					release.set()
					self.assertTrue(finished.wait(2), "Scan did not exit after release")
				self.assertEqual(
					progress, calculation.getProgress(), "Late completion replaced partial results"
				)

	def testSlowDirectoryProgressDoesNotTimeOut(self) -> None:
		"""A scan lasting over 30 seconds stays alive while fewer than 256 entries arrive."""
		clock = Mock(return_value=100.0)
		scandir = os.scandir
		with TemporaryDirectory() as temporaryDirectory:
			root = Path(temporaryDirectory)
			for index in range(4):
				(root / str(index)).write_bytes(b"x")
			calculation = fileSize.FileSizeCalculation((str(root),))

			def slowScandir(path: str):
				with scandir(path) as entries:
					for entry in entries:
						clock.return_value += 20
						calculation.poll()
						yield entry

			with (
				patch.object(fileSize, "monotonic", clock),
				patch.object(fileSize.os, "scandir", side_effect=slowScandir),
			):
				progress = self._run(calculation)
			self.assertEqual(4, progress.fileCount)
			self.assertFalse(progress.isIncomplete)
			self.cancelIo.assert_not_called()

	def testHandleFailureReleasesCapacityWithoutReadingFiles(self) -> None:
		"""Fail safely when a cancellable worker cannot be established, and permit later scans."""
		with (
			patch.object(self.kernel32, "OpenThread", return_value=None),
			patch.object(fileSize.os, "stat") as pathStat,
		):
			for _ in range(3):
				progress = self._run(fileSize.FileSizeCalculation((__file__,)))
				self.assertTrue(progress.isComplete and progress.isIncomplete)
			pathStat.assert_not_called()
		self.kernel32.CloseHandle.assert_not_called()
		self.assertFalse(self._run(fileSize.FileSizeCalculation((__file__,))).isIncomplete)

	def testCancelBeforeStartDoesNotAccessFiles(self) -> None:
		calculation = fileSize.FileSizeCalculation((__file__,))
		calculation.cancel()
		with patch.object(fileSize.os, "stat") as pathStat:
			progress = self._run(calculation)
			pathStat.assert_not_called()
		self.kernel32.OpenThread.assert_not_called()
		self.assertTrue(progress.isComplete and progress.isIncomplete)

	def testSummaryDistinguishesEmptyUnreadableAndPartialContents(self) -> None:
		"""Only confirmed empty contents are zero bytes; partial counts are never called a total."""
		for progress, expected in (
			(
				fileSize.FileSizeProgress(),
				"Files; calculating contents",
			),
			(
				fileSize.FileSizeProgress(isComplete=True),
				f"Files; contents total {formatting.formatDataSize(0)}",
			),
			(
				fileSize.FileSizeProgress(isComplete=True, isIncomplete=True),
				"Files; could not calculate contents; copy the files again later to retry",
			),
			(
				fileSize.FileSizeProgress(byteCount=4, fileCount=1, isComplete=True, isIncomplete=True),
				f"Files; calculation finished, incomplete results: {formatting.formatDataSize(4)}, 1 file",
			),
		):
			with self.subTest(progress=progress):
				self.assertEqual(expected, formatting.formatFileSizeSummary("Files", progress))

	def testDataSizesMatchWindows(self) -> None:
		"""Match Windows' full localized output, including byte sizes and totals above 4 GB."""
		windowsFormat = WinDLL("shlwapi").StrFormatByteSizeW
		windowsFormat.argtypes = (c_longlong, LPWSTR, UINT)
		windowsFormat.restype = LPWSTR
		for byteCount in (0, 1023, 1024, 10_500, 5_351_040_416, 1_726_779_832):
			with self.subTest(byteCount=byteCount):
				buffer = create_unicode_buffer(64)
				expected = windowsFormat(byteCount, buffer, len(buffer))
				self.assertEqual(expected, formatting.formatDataSize(byteCount))
