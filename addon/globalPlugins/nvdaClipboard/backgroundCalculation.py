# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Run one cancellable calculation in a background thread and publish its progress."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, Lock, Thread
from typing import ClassVar, Self

from logHandler import log


__all__ = ["BackgroundCalculation"]


class BackgroundCalculation[ProgressT]:
	"""Start a calculation at most once and expose immutable progress snapshots.

	Subclasses implement :meth:`_calculate`, which runs on the worker thread, and
	:meth:`_publishStartFailure`, which records a completed result when no worker can be started.
	"""

	_threadName: ClassVar[str]
	_startFailureMessage: ClassVar[str]
	_completionFailureMessage: ClassVar[str]

	def __init__(self, initialProgress: ProgressT) -> None:
		"""Create a calculation that remains idle until :meth:`start` is called."""
		super().__init__()
		self._cancelEvent: Event = Event()
		self._progressLock: Lock = Lock()
		self._progress: ProgressT = initialProgress
		self._hasStarted: bool = False

	def start(self, onComplete: Callable[[Self], None] | None = None) -> bool:
		"""Start the calculation once and return whether its worker was started."""
		with self._progressLock:
			if self._hasStarted:
				return False
			self._hasStarted = True
		thread = Thread(
			target=self._run,
			args=(onComplete,),
			name=self._threadName,
			daemon=True,
		)
		try:
			thread.start()
		except RuntimeError:
			log.exception(self._startFailureMessage)
			self._publishStartFailure()
			return False
		return True

	def getProgress(self) -> ProgressT:
		"""Return an immutable snapshot of the latest calculation progress."""
		with self._progressLock:
			return self._progress

	def cancel(self) -> None:
		"""Request cancellation without waiting for the worker thread."""
		self._cancelEvent.set()

	def _run(self, onComplete: Callable[[Self], None] | None) -> None:
		"""Run the calculation on the worker thread and report its completion."""
		self._calculate()
		if onComplete is not None:
			try:
				onComplete(self)
			except Exception:
				log.exception(self._completionFailureMessage)

	def _setProgress(self, progress: ProgressT) -> None:
		"""Atomically replace the progress visible to callers."""
		with self._progressLock:
			self._progress = progress

	def _calculate(self) -> None:
		"""Perform the calculation and publish a completed progress snapshot."""
		raise NotImplementedError

	def _publishStartFailure(self) -> None:
		"""Publish a completed progress snapshot after the worker thread failed to start."""
		raise NotImplementedError
