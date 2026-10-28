# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Run cancellable calculations in background threads and queue work onto wx's GUI thread."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from threading import Event, Lock, Thread
from typing import ClassVar, Self

from logHandler import log
import wx


__all__ = ["BackgroundCalculation", "LatestWorkerResult", "queueMainThread"]


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


def queueMainThread(
	callback: Callable[..., object],
	*args: object,
	onFailure: Callable[[], bool],
	failureMessage: str,
) -> bool:
	"""Queue a callback on wx's GUI thread and return whether wx accepted it.

	wx raises :class:`RuntimeError` once it no longer accepts work, typically during shutdown.
	In that case ``onFailure`` releases any caller state and returns whether the owner is still
	active, and ``failureMessage`` is logged as a debug warning only for an active owner.
	"""
	try:
		wx.CallAfter(callback, *args)
	except RuntimeError:
		if onFailure():
			log.debugWarning(failureMessage, exc_info=True)
		return False
	return True


class LatestWorkerResult[ResultT]:
	"""Deliver only the latest of a series of worker futures to a callback on wx's GUI thread.

	Tracking a new future or cancelling makes any earlier future stale: a stale future is
	cancelled if it has not started, and its result never reaches ``onFinish``.
	"""

	def __init__(
		self,
		onFinish: Callable[[Future[ResultT]], None],
		*,
		isOwnerActive: Callable[[], bool],
		scheduleFailureMessage: str,
		onScheduleFailure: Callable[[], None] | None = None,
		queueStaleResults: bool = True,
	) -> None:
		"""Create an idle tracker.

		``onFinish`` receives the completed latest future on the GUI thread. When wx no longer
		accepts work, ``onScheduleFailure`` runs if the future was still the latest one, and
		``scheduleFailureMessage`` is logged as a debug warning while ``isOwnerActive`` is true.
		``queueStaleResults`` controls whether a future that became stale while running is still
		queued to the GUI thread, where it is then ignored.
		"""
		super().__init__()
		self._onFinish = onFinish
		self._isOwnerActive = isOwnerActive
		self._scheduleFailureMessage = scheduleFailureMessage
		self._onScheduleFailure = onScheduleFailure
		self._queueStaleResults = queueStaleResults
		self._pending: Future[ResultT] | None = None

	def track(self, future: Future[ResultT]) -> None:
		"""Make ``future`` the latest pending result and deliver it once it completes."""
		self._pending = future
		future.add_done_callback(self._queueFinish)

	def cancel(self) -> None:
		"""Cancel a queued future and make any running result stale."""
		future = self._pending
		self._pending = None
		if future is not None:
			future.cancel()

	def _queueFinish(self, future: Future[ResultT]) -> None:
		"""Queue one completed future for delivery on the GUI thread."""
		if not self._queueStaleResults and future is not self._pending:
			return

		def releaseAfterScheduleFailure() -> bool:
			"""Forget the future if it is still the latest one and return whether the owner is active."""
			if future is self._pending:
				self._pending = None
				if self._onScheduleFailure is not None:
					self._onScheduleFailure()
			return self._isOwnerActive()

		_ = queueMainThread(
			self._finish,
			future,
			onFailure=releaseAfterScheduleFailure,
			failureMessage=self._scheduleFailureMessage,
		)

	def _finish(self, future: Future[ResultT]) -> None:
		"""Pass the latest completed future to ``onFinish`` and ignore stale ones."""
		if future is not self._pending:
			return
		self._pending = None
		self._onFinish(future)
