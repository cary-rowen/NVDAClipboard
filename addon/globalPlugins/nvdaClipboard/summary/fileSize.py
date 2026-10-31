# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Calculate clipboard file sizes without blocking NVDA's main thread."""

from __future__ import annotations

from ctypes import WinDLL, WinError, get_last_error
from ctypes.wintypes import BOOL, HANDLE
from dataclasses import dataclass
import os
import stat
from threading import BoundedSemaphore
from time import monotonic
from typing import NamedTuple, override

from logHandler import log
from winBindings import kernel32

from ..background import BackgroundCalculation


__all__ = ["FileSizeCalculation", "FileSizeProgress"]

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_THREAD_TERMINATE = 0x0001
_ERROR_NOT_FOUND = 1168
_STALL_TIMEOUT_SECONDS = 30
# shortcut: Uncancellable drivers can occupy both slots; guaranteed recovery needs process isolation.
_scanSlots = BoundedSemaphore(2)

_kernel32 = WinDLL("kernel32", use_last_error=True)
_cancelSynchronousIo = _kernel32.CancelSynchronousIo
_cancelSynchronousIo.argtypes = (HANDLE,)
_cancelSynchronousIo.restype = BOOL


class FileSizeProgress(NamedTuple):
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
		self._threadHandle: int | None = None
		self._lastProgressTime: float | None = None

	@override
	def cancel(self) -> None:
		with self._progressLock:
			self._cancelLocked()

	def poll(self) -> bool:
		"""Check for stalled I/O and retry cancellation; return whether another check is needed."""
		with self._progressLock:
			if self._cancelEvent.is_set():
				self._cancelLocked()
			elif (
				not self._progress.isComplete
				and self._lastProgressTime is not None
				and monotonic() - self._lastProgressTime >= _STALL_TIMEOUT_SECONDS
			):
				self._cancelLocked()
				log.debugWarning("Clipboard file size calculation stopped after 30 seconds without progress")
			return self._threadHandle is not None or not self._progress.isComplete

	def _cancelLocked(self) -> None:
		super().cancel()
		if not self._progress.isComplete:
			self._progress = self._progress._replace(isComplete=True, isIncomplete=True)
		# The same lock protects cancellation and closing the handle, including handle reuse.
		if self._threadHandle is not None and not _cancelSynchronousIo(self._threadHandle):
			error = get_last_error()
			if error != _ERROR_NOT_FOUND:
				log.debugWarning(f"Unable to cancel clipboard file size I/O: {WinError(error)}")

	@override
	def _publishStartFailure(self) -> None:
		"""Publish an incomplete empty result when the worker thread cannot be started."""
		self._publish(_ScanTotals(isIncomplete=True), isComplete=True)

	@override
	def _calculate(self) -> None:
		"""Keep blocked scans bounded without queuing more threads behind them."""
		totals = _ScanTotals()
		if not _scanSlots.acquire(blocking=False):
			log.debugWarning("Clipboard file size calculation unavailable: scanning slots are occupied")
			self._publish(_ScanTotals(isIncomplete=True), isComplete=True)
			return
		try:
			with self._progressLock:
				if self._cancelEvent.is_set():
					return
				self._threadHandle = kernel32.OpenThread(
					_THREAD_TERMINATE,
					False,
					kernel32.GetCurrentThreadId(),
				)
				if not self._threadHandle:
					self._threadHandle = None
					raise WinError()
				self._lastProgressTime = monotonic()
			self._scan(totals)
		except Exception:
			log.exception("Unexpected error while calculating clipboard file size")
			totals.isIncomplete = True
		finally:
			with self._progressLock:
				if self._threadHandle is not None:
					if not kernel32.CloseHandle(self._threadHandle):
						log.debugWarning(f"Unable to close clipboard scan thread handle: {WinError()}")
					self._threadHandle = None
			self._publish(totals, isComplete=True)
			_scanSlots.release()

	def _scan(self, totals: _ScanTotals) -> None:
		"""Add metadata for each regular file and directory to the totals."""
		for path in self._paths:
			if self._cancelEvent.is_set():
				return
			try:
				pathStat = os.stat(path, follow_symlinks=False)
			except OSError:
				totals.isIncomplete = True
			else:
				if _isReparsePoint(pathStat):
					totals.isIncomplete = True
				elif stat.S_ISDIR(pathStat.st_mode):
					totals.directoryCount += 1
					self._publish(totals)
					self._scanDirectory(path, totals)
				elif stat.S_ISREG(pathStat.st_mode):
					totals.fileCount += 1
					totals.byteCount += pathStat.st_size
				else:
					totals.isIncomplete = True
			self._publish(totals)

	def _scanDirectory(
		self,
		directory: str,
		totals: _ScanTotals,
	) -> None:
		"""Scan one directory with a streaming depth-first iterator stack."""
		if self._cancelEvent.is_set():
			return
		try:
			iterators = [os.scandir(directory)]
		except OSError:
			totals.isIncomplete = True
			return
		try:
			while iterators:
				if self._cancelEvent.is_set():
					return
				self._publish(totals)
				try:
					entry = next(iterators[-1])
				except StopIteration:
					iterators.pop().close()
					continue
				except OSError:
					totals.isIncomplete = True
					iterators.pop().close()
					continue
				try:
					entryStat = entry.stat(follow_symlinks=False)
				except OSError:
					totals.isIncomplete = True
				else:
					if _isReparsePoint(entryStat):
						totals.isIncomplete = True
					elif stat.S_ISDIR(entryStat.st_mode):
						totals.directoryCount += 1
						self._publish(totals)
						if self._cancelEvent.is_set():
							return
						try:
							iterators.append(os.scandir(entry.path))
						except OSError:
							totals.isIncomplete = True
					elif stat.S_ISREG(entryStat.st_mode):
						totals.fileCount += 1
						totals.byteCount += entryStat.st_size
					else:
						totals.isIncomplete = True
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
		with self._progressLock:
			if self._progress.isComplete:
				return
			self._lastProgressTime = monotonic()
			self._progress = FileSizeProgress(
				byteCount=totals.byteCount,
				fileCount=totals.fileCount,
				directoryCount=totals.directoryCount,
				isComplete=isComplete,
				isIncomplete=totals.isIncomplete,
			)


def _isReparsePoint(pathStat: os.stat_result) -> bool:
	"""Return whether a stat result represents a Windows reparse point."""
	return bool(getattr(pathStat, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)
