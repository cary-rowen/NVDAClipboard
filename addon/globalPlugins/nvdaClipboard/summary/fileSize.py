# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Calculate clipboard file sizes without blocking NVDA's main thread."""

from __future__ import annotations

from dataclasses import dataclass
import os
import stat
from threading import Lock
from typing import override

from logHandler import log
from winBindings import kernel32

from ..background import BackgroundCalculation


__all__ = ["FileSizeCalculation", "FileSizeProgress"]

_DRIVE_REMOTE = 4
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_PUBLISH_INTERVAL = 256
_SCAN_SLOT_POLL_SECONDS = 0.05
_scanSlot = Lock()


@dataclass(frozen=True, slots=True)
class FileSizeProgress:
	"""Describe the latest published state of a file size calculation."""

	byteCount: int = 0
	fileCount: int = 0
	directoryCount: int = 0
	isComplete: bool = False
	isIncomplete: bool = False


@dataclass(slots=True)
class _ScanTotals:
	"""Accumulate file-system metadata before publishing an immutable snapshot."""

	byteCount: int = 0
	entryCount: int = 0
	fileCount: int = 0
	directoryCount: int = 0
	isIncomplete: bool = False


class FileSizeCalculation(BackgroundCalculation[FileSizeProgress]):
	"""Calculate the logical size of files and directory contents in one background thread."""

	_threadName = "nvdaClipboard.fileSize"
	_startFailureMessage = "Unable to start clipboard file size calculation"
	_completionFailureMessage = "Unable to report clipboard file size completion"

	def __init__(self, paths: tuple[str, ...]) -> None:
		"""Create a calculation that remains idle until :meth:`start` is called."""
		super().__init__(FileSizeProgress())
		self._paths: tuple[str, ...] = tuple(paths)

	@override
	def _publishStartFailure(self) -> None:
		"""Publish an incomplete empty result when the worker thread cannot be started."""
		self._publish(_ScanTotals(isIncomplete=True), isComplete=True)

	@override
	def _calculate(self) -> None:
		"""Run the calculation under the process-wide scanning slot."""
		totals = _ScanTotals()
		hasScanSlot = False
		try:
			while not self._cancelEvent.is_set():
				if _scanSlot.acquire(timeout=_SCAN_SLOT_POLL_SECONDS):
					hasScanSlot = True
					break
			if hasScanSlot:
				self._scan(totals)
		except Exception:
			log.exception("Unexpected error while calculating clipboard file size")
			totals.isIncomplete = True
		finally:
			if hasScanSlot:
				_scanSlot.release()
			totals.isIncomplete = totals.isIncomplete or self._cancelEvent.is_set()
			self._publish(totals, isComplete=True)

	def _scan(self, totals: _ScanTotals) -> None:
		"""Add metadata for each local regular file and directory to the totals."""
		for path in self._paths:
			if self._cancelEvent.is_set():
				totals.isIncomplete = True
				return
			totals.entryCount += 1
			if _isRemotePath(path):
				totals.isIncomplete = True
			else:
				try:
					pathStat = os.stat(path, follow_symlinks=False)
				except OSError:
					totals.isIncomplete = True
				else:
					if _isReparsePoint(pathStat):
						totals.isIncomplete = True
					elif stat.S_ISDIR(pathStat.st_mode):
						totals.directoryCount += 1
						self._scanDirectory(path, totals)
					elif stat.S_ISREG(pathStat.st_mode):
						totals.fileCount += 1
						totals.byteCount += pathStat.st_size
					else:
						totals.isIncomplete = True
			if totals.entryCount % _PUBLISH_INTERVAL == 0:
				self._publish(totals)

	def _scanDirectory(
		self,
		directory: str,
		totals: _ScanTotals,
	) -> None:
		"""Scan one directory with a streaming depth-first iterator stack."""
		try:
			iterators = [os.scandir(directory)]
		except OSError:
			totals.isIncomplete = True
			return
		try:
			while iterators:
				if self._cancelEvent.is_set():
					totals.isIncomplete = True
					return
				try:
					entry = next(iterators[-1])
				except StopIteration:
					iterators.pop().close()
					continue
				except OSError:
					totals.isIncomplete = True
					iterators.pop().close()
					continue
				totals.entryCount += 1
				try:
					entryStat = entry.stat(follow_symlinks=False)
				except OSError:
					totals.isIncomplete = True
				else:
					if _isReparsePoint(entryStat):
						totals.isIncomplete = True
					elif stat.S_ISDIR(entryStat.st_mode):
						totals.directoryCount += 1
						try:
							iterators.append(os.scandir(entry.path))
						except OSError:
							totals.isIncomplete = True
					elif stat.S_ISREG(entryStat.st_mode):
						totals.fileCount += 1
						totals.byteCount += entryStat.st_size
					else:
						totals.isIncomplete = True
				if totals.entryCount % _PUBLISH_INTERVAL == 0:
					self._publish(totals)
		finally:
			for iterator in reversed(iterators):
				iterator.close()

	def _publish(
		self,
		totals: _ScanTotals,
		*,
		isComplete: bool = False,
	) -> None:
		"""Atomically replace the progress visible to callers."""
		self._setProgress(
			FileSizeProgress(
				byteCount=totals.byteCount,
				fileCount=totals.fileCount,
				directoryCount=totals.directoryCount,
				isComplete=isComplete,
				isIncomplete=totals.isIncomplete,
			),
		)


def _isRemotePath(path: str) -> bool:
	"""Return whether Windows reports that a path is on a remote drive."""
	if os.name != "nt":
		return False
	absolutePath = os.path.abspath(path)
	drive, _tail = os.path.splitdrive(absolutePath)
	if not drive:
		return False
	normalizedDrive = drive.casefold()
	if normalizedDrive.startswith("\\\\?\\unc\\") or (
		normalizedDrive.startswith("\\\\") and not normalizedDrive.startswith(("\\\\?\\", "\\\\.\\"))
	):
		return True
	root = f"{drive}\\"
	return kernel32.GetDriveType(root) == _DRIVE_REMOTE


def _isReparsePoint(pathStat: os.stat_result) -> bool:
	"""Return whether a stat result represents a Windows reparse point."""
	return bool(getattr(pathStat, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)
