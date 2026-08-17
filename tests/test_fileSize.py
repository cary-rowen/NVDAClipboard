"""Tests for the standalone clipboard file size calculator."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


_MODULE_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "fileSize.py"
_WIN_BINDINGS = ModuleType("winBindings")
_WIN_BINDINGS.kernel32 = SimpleNamespace(GetDriveType=lambda _root: 3)
_SPEC = importlib.util.spec_from_file_location("nvdaClipboardFileSize", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
fileSize = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = fileSize
with patch.dict(
	sys.modules,
	{
		"logHandler": SimpleNamespace(log=SimpleNamespace(exception=lambda *_args, **_kwargs: None)),
		"winBindings": _WIN_BINDINGS,
	},
):
	_SPEC.loader.exec_module(fileSize)


class FileSizeCalculationTests(unittest.TestCase):
	"""Verify file size calculation without loading NVDA."""

	def _waitForCompletion(self, calculation: fileSize.FileSizeCalculation) -> fileSize.FileSizeProgress:
		"""Wait briefly for a background calculation to finish."""
		deadline = time.monotonic() + 2
		while time.monotonic() < deadline:
			progress = calculation.getProgress()
			if progress.isComplete:
				return progress
			time.sleep(0.01)
		self.fail("File size calculation did not complete")

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

	def testMissingAndRemotePathsAreIncomplete(self) -> None:
		"""Skip missing and remote paths without failing the calculation."""
		with TemporaryDirectory() as temporaryDirectory:
			root = Path(temporaryDirectory)
			localFile = root / "local.bin"
			localFile.write_bytes(b"1234")
			missingFile = root / "missing.bin"
			remoteFile = root / "remote.bin"
			remoteFile.write_bytes(b"ignored")

			with patch.object(fileSize, "_isRemotePath", side_effect=lambda path: path == str(remoteFile)):
				calculation = fileSize.FileSizeCalculation(
					(str(localFile), str(missingFile), str(remoteFile)),
				)
				calculation.start()
				progress = self._waitForCompletion(calculation)

		self.assertEqual(4, progress.byteCount)
		self.assertEqual(1, progress.fileCount)
		self.assertEqual(0, progress.directoryCount)
		self.assertTrue(progress.isIncomplete)

	@unittest.skipUnless(os.name == "nt", "Windows drive classification")
	def testExtendedLocalPathIsNotRemote(self) -> None:
		"""Distinguish an extended local path from UNC network paths."""
		drive, _tail = os.path.splitdrive(os.path.abspath(__file__))
		self.assertFalse(fileSize._isRemotePath(rf"\\?\{drive}\folder"))
		self.assertTrue(fileSize._isRemotePath(r"\\server\share\folder"))

	def testCancellationCompletesAsIncomplete(self) -> None:
		"""Cancel a calculation before it acquires the shared scanning slot."""
		with TemporaryDirectory() as temporaryDirectory:
			filePath = Path(temporaryDirectory) / "file.bin"
			filePath.write_bytes(b"1234")
			calculation = fileSize.FileSizeCalculation((str(filePath),))
			with fileSize._scanSlot:
				self.assertTrue(calculation.start())
				calculation.cancel()
				progress = self._waitForCompletion(calculation)

		self.assertEqual(0, progress.byteCount)
		self.assertEqual(0, progress.fileCount)
		self.assertEqual(0, progress.directoryCount)
		self.assertTrue(progress.isIncomplete)
