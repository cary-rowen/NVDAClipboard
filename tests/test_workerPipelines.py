# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for the worker-to-main-thread pipelines of the controller, paster, and history writer.

Each pipeline submits work to a background worker, queues the result onto wx's main thread,
and finishes it there. These tests pin success, cancellation, staleness, worker failures,
controller termination, and a wx application that no longer accepts queued calls.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests._manager_loader import _PlaceholderModule, fakeNvdaModules
from tests._module_loader import createAddonPackage, loadAddonModule
from tests.test_clipboardMonitor import clipboardMonitor


_PACKAGE_NAME = "nvdaClipboardWorkerPipelineTests"
createAddonPackage(_PACKAGE_NAME)
storageModels = loadAddonModule(f"{_PACKAGE_NAME}.storageModels")


def _placeholderAddonModules() -> dict[str, object]:
	"""Return placeholders for add-on modules the pipelines do not exercise."""
	modules: dict[str, object] = {}
	for name in (
		"cloudSync",
		"configuration",
		"cues",
		"fileSize",
		"images",
		"navigation",
		"oneDriveSync",
		"search",
		"storage",
		"textStats",
	):
		modules[f"{_PACKAGE_NAME}.{name}"] = _PlaceholderModule(f"{_PACKAGE_NAME}.{name}")
	storage = modules[f"{_PACKAGE_NAME}.storage"]
	for errorName in (
		"CategoryExistsError",
		"CategoryNameError",
		"CategoryNotEmptyError",
		"CategoryNotFoundError",
		"ImageStorageLimitError",
		"InvalidItemError",
		"ItemNotFoundError",
		"ReservedCategoryNameError",
		"StorageError",
		"StorageFormatError",
	):
		setattr(storage, errorName, getattr(storageModels, errorName))
	return modules


controllerModule = loadAddonModule(
	f"{_PACKAGE_NAME}.controller",
	injectedModules={
		**fakeNvdaModules(
			extraModuleNames=(
				"braille",
				"colors",
				"controlTypes",
				"core",
				"eventHandler",
				"keyboardHandler",
				"speech",
				"textInfos",
			),
		),
		f"{_PACKAGE_NAME}.clipboardMonitor": clipboardMonitor,
		**_placeholderAddonModules(),
	},
)
pasteModule = sys.modules[f"{_PACKAGE_NAME}.paste"]
historyWriterModule = sys.modules[f"{_PACKAGE_NAME}.historyWriter"]
# Every loaded add-on module shares these fake NVDA modules.
_WX = controllerModule.wx
_UI = controllerModule.ui
_LOG = controllerModule.log

_CLIPBOARD_CHANGED = "The clipboard changed before it could be updated. Try again."
_COULD_NOT_WRITE = "Could not write to the system clipboard"
_COULD_NOT_PASTE = "Could not paste the content"


class _MainThread:
	"""Collect calls queued with ``wx.CallAfter`` and run them on request."""

	def __init__(self) -> None:
		"""Create an open queue."""
		self.isClosed = False
		self._calls: list[tuple[Callable[..., object], tuple[object, ...]]] = []
		self._lock = Lock()

	def callAfter(self, callback: Callable[..., object], *args: object) -> None:
		"""Queue one call, or raise like wx once the application no longer accepts work."""
		if self.isClosed:
			raise RuntimeError("wx.App no longer exists")
		with self._lock:
			self._calls.append((callback, args))

	@property
	def pendingCount(self) -> int:
		"""Return how many queued calls have not run yet."""
		with self._lock:
			return len(self._calls)

	def drain(self) -> None:
		"""Run queued calls in order, including calls queued while draining."""
		while True:
			with self._lock:
				if not self._calls:
					return
				callback, args = self._calls.pop(0)
			callback(*args)


class _ManualWorker:
	"""Serial worker whose jobs run only when a test asks for them."""

	def __init__(self) -> None:
		"""Create an empty worker."""
		self.jobs: list[tuple[Future[object], Callable[..., object], tuple[object, ...]]] = []
		self.isShutDown = False

	def submit(self, fn: Callable[..., object], /, *args: object) -> Future[object]:
		"""Queue one job and return its future."""
		future: Future[object] = Future()
		self.jobs.append((future, fn, args))
		return future

	def begin(self) -> tuple[Future[object], Callable[..., object], tuple[object, ...]] | None:
		"""Start the oldest job, returning ``None`` when it was cancelled while queued."""
		job = self.jobs.pop(0)
		return job if job[0].set_running_or_notify_cancel() else None

	@staticmethod
	def complete(job: tuple[Future[object], Callable[..., object], tuple[object, ...]] | None) -> None:
		"""Run a started job and publish its result, which runs done callbacks."""
		if job is None:
			return
		future, fn, args = job
		try:
			result = fn(*args)
		except Exception as error:
			future.set_exception(error)
		else:
			future.set_result(result)

	def runAll(self) -> None:
		"""Run every queued job in submission order."""
		while self.jobs:
			self.complete(self.begin())

	def shutdown(self) -> None:
		"""Record that the owner shut the worker down."""
		self.isShutDown = True


class _PipelineTestCase(unittest.TestCase):
	"""Install a controllable wx main-thread queue and reset shared fakes."""

	def setUp(self) -> None:
		"""Route ``wx.CallAfter`` to a fresh queue and reset logging and speech fakes."""
		self.mainThread = _MainThread()
		patcher = patch.object(_WX, "CallAfter", self.mainThread.callAfter, create=True)
		patcher.start()
		self.addCleanup(patcher.stop)
		patcher = patch.object(_UI, "message", Mock())
		patcher.start()
		self.addCleanup(patcher.stop)
		_LOG.reset_mock()

	def debugWarnings(self) -> list[str]:
		"""Return the messages logged as debug warnings."""
		return [call.args[0] for call in _LOG.debugWarning.call_args_list]

	def spokenMessages(self) -> list[str]:
		"""Return the messages passed to ``ui.message``."""
		return [call.args[0] for call in _UI.message.call_args_list]


def _createController(worker: object) -> object:
	"""Create a started controller whose services are fakes and whose serial worker is ``worker``."""
	with patch.multiple(
		controllerModule,
		ClipboardStorage=Mock(),
		ClipboardNavigator=Mock(),
		OneDriveSyncManager=Mock(),
		ClipboardMonitor=Mock(),
		HistoryWriter=Mock(return_value=worker),
	):
		controller = controllerModule.ClipboardController()
	controller._isStarted = True
	return controller


def _pngSnapshot(imageData: bytes = b"png", text: str = "") -> object:
	"""Return a stored PNG snapshot, optionally mixed with text."""
	contentType = clipboardMonitor.ClipboardContentType
	return clipboardMonitor.ClipboardSnapshot(
		contentType.TEXT_AND_IMAGE if text else contentType.IMAGE,
		text=text,
		imageFormat="PNG",
		imageData=imageData,
		imageWidth=1,
		imageHeight=1,
		imageBitDepth=32,
	)


class PngClipboardWritePipelineTests(_PipelineTestCase):
	"""Pin putting a stored PNG entry on the clipboard through the serial worker."""

	def setUp(self) -> None:
		"""Create a started controller whose stored entry is a PNG image."""
		super().setUp()
		self.worker = _ManualWorker()
		self.controller = _createController(self.worker)
		self.controller.monitor.getSequenceNumber.return_value = 5
		self.snapshots: dict[int, object] = {}
		self.controller._getStoredItemById = Mock(side_effect=lambda _category, itemId: itemId)
		self.controller._snapshotFromItem = Mock(side_effect=lambda itemId: self.snapshots[itemId])
		self.controller._writeSnapshot = Mock(return_value=6)
		self.pngToPackedDib = Mock(return_value=b"dib")
		for target, name, value in (
			(pasteModule, "pngToPackedDib", self.pngToPackedDib),
			(
				controllerModule,
				"formatPutOnClipboardConfirmation",
				lambda itemId: f"Copied {itemId}",
			),
		):
			patcher = patch.object(target, name, value)
			patcher.start()
			self.addCleanup(patcher.stop)

	def put(self, itemId: int = 1) -> object:
		"""Request one stored PNG entry on the clipboard and return its snapshot."""
		snapshot = _pngSnapshot(f"png {itemId}".encode())
		self.snapshots[itemId] = snapshot
		self.controller.putItemOnClipboard("Work", itemId)
		return snapshot

	def assertNothingCommitted(self) -> None:
		"""Assert that nothing was written or announced."""
		self.controller._writeSnapshot.assert_not_called()
		self.assertEqual([], self.spokenMessages())

	def testSuccessCommitsPreparedDibAndConfirms(self) -> None:
		"""Write the prepared DIB on the main thread and announce the confirmation."""
		snapshot = self.put()
		self.controller._writeSnapshot.assert_not_called()
		self.worker.runAll()
		self.controller._writeSnapshot.assert_not_called()
		self.mainThread.drain()
		self.controller._writeSnapshot.assert_called_once_with(
			snapshot,
			pasteModule.ClipboardChangeSource.RECORD,
			expectedSequenceNumber=5,
			preparedPngDib=b"dib",
		)
		self.assertEqual(["Copied 1"], self.spokenMessages())

	def testNewerRequestCancelsQueuedPreparation(self) -> None:
		"""Never prepare or commit a request superseded while still queued."""
		self.put(1)
		newer = self.put(2)
		self.worker.runAll()
		self.mainThread.drain()
		self.pngToPackedDib.assert_called_once()
		self.controller._writeSnapshot.assert_called_once()
		self.assertIs(newer, self.controller._writeSnapshot.call_args.args[0])
		self.assertEqual(["Copied 2"], self.spokenMessages())

	def testNewerRequestMakesFinishedPreparationStale(self) -> None:
		"""Discard a prepared result whose request was superseded before the main thread ran it."""
		self.put(1)
		self.worker.runAll()
		newer = self.put(2)
		self.mainThread.drain()
		self.assertNothingCommitted()
		self.worker.runAll()
		self.mainThread.drain()
		self.controller._writeSnapshot.assert_called_once()
		self.assertIs(newer, self.controller._writeSnapshot.call_args.args[0])

	def testNewerRequestMakesRunningPreparationStale(self) -> None:
		"""Discard a result that was being prepared when a newer request arrived."""
		self.put(1)
		running = self.worker.begin()
		self.put(2)
		self.worker.complete(running)
		self.mainThread.drain()
		self.assertNothingCommitted()

	def testClipboardChangedBeforeCommitReportsRetry(self) -> None:
		"""Refuse to replace clipboard data that changed while the PNG was prepared."""
		self.put()
		self.worker.runAll()
		self.controller.monitor.getSequenceNumber.return_value = 6
		self.mainThread.drain()
		self.controller._writeSnapshot.assert_not_called()
		self.assertEqual([_CLIPBOARD_CHANGED], self.spokenMessages())

	def testClipboardChangedDuringCommitReportsRetry(self) -> None:
		"""Report a clipboard change detected by the write itself."""
		self.controller._writeSnapshot.side_effect = clipboardMonitor.ClipboardSequenceChangedError()
		self.put()
		self.worker.runAll()
		self.mainThread.drain()
		self.assertEqual([_CLIPBOARD_CHANGED], self.spokenMessages())

	def testUndecodablePngReportsWriteFailure(self) -> None:
		"""Report a PNG that cannot be decoded as a write failure with a debug warning."""
		self.pngToPackedDib.return_value = None
		self.put()
		self.worker.runAll()
		self.mainThread.drain()
		self.controller._writeSnapshot.assert_not_called()
		self.assertEqual([_COULD_NOT_WRITE], self.spokenMessages())
		self.assertEqual(
			["Stored PNG data could not be prepared for writing to the clipboard."],
			self.debugWarnings(),
		)
		_LOG.exception.assert_not_called()

	def testUnexpectedWorkerErrorIsLoggedAndReported(self) -> None:
		"""Log an unexpected preparation error and report a write failure."""
		self.pngToPackedDib.side_effect = OSError("decoder crashed")
		self.put()
		self.worker.runAll()
		self.mainThread.drain()
		self.controller._writeSnapshot.assert_not_called()
		self.assertEqual([_COULD_NOT_WRITE], self.spokenMessages())
		_LOG.exception.assert_called_once()

	def testTerminateCancelsQueuedPreparation(self) -> None:
		"""Cancel a queued preparation when the controller terminates."""
		self.put()
		self.controller.terminate()
		self.assertTrue(self.worker.isShutDown)
		self.worker.runAll()
		self.mainThread.drain()
		self.pngToPackedDib.assert_not_called()
		self.assertNothingCommitted()

	def testTerminateDiscardsFinishedPreparation(self) -> None:
		"""Discard a prepared result that reaches the main thread after termination."""
		self.put()
		self.worker.runAll()
		self.controller.terminate()
		self.mainThread.drain()
		self.assertNothingCommitted()

	def testWxGoneDropsResultAndLogsWhileStarted(self) -> None:
		"""Log an unschedulable result while started and keep later requests working."""
		self.mainThread.isClosed = True
		self.put(1)
		self.worker.runAll()
		self.assertEqual(["Could not schedule a prepared PNG clipboard write."], self.debugWarnings())
		self.assertNothingCommitted()
		self.mainThread.isClosed = False
		self.put(2)
		self.worker.runAll()
		self.mainThread.drain()
		self.assertEqual(["Copied 2"], self.spokenMessages())

	def testWxGoneAfterTerminateIsSilent(self) -> None:
		"""Do not log an unschedulable result after termination."""
		self.put()
		running = self.worker.begin()
		self.controller.terminate()
		self.mainThread.isClosed = True
		self.worker.complete(running)
		self.assertEqual([], self.debugWarnings())
		self.assertNothingCommitted()

	def testWxGoneForStaleResultKeepsNewerRequest(self) -> None:
		"""Schedule even a stale result, but let its failure leave the newer request pending."""
		self.put(1)
		running = self.worker.begin()
		newer = self.put(2)
		self.mainThread.isClosed = True
		self.worker.complete(running)
		self.assertEqual(["Could not schedule a prepared PNG clipboard write."], self.debugWarnings())
		self.mainThread.isClosed = False
		self.worker.runAll()
		self.mainThread.drain()
		self.controller._writeSnapshot.assert_called_once()
		self.assertIs(newer, self.controller._writeSnapshot.call_args.args[0])


class TemporaryPngPastePipelineTests(_PipelineTestCase):
	"""Pin preparing a temporary PNG paste on the serial worker."""

	def setUp(self) -> None:
		"""Create a paster for a started controller that records paste starts."""
		super().setUp()
		self.worker = _ManualWorker()
		self.isStarted = True
		self.paster = pasteModule.TemporaryPaster(
			monitor=Mock(),
			storage=Mock(),
			worker=self.worker,
			isStarted=lambda: self.isStarted,
			getLastAppliedSequenceNumber=Mock(return_value=0),
			clearPendingWrites=Mock(),
			captureOriginalClipboard=Mock(),
			writeSnapshot=Mock(),
			clearClipboard=Mock(),
			applyLeftoverClipboardContent=Mock(),
			distrustRegisteredClipboardState=Mock(),
			getClipboardOwnerHandle=Mock(return_value=0),
			isSameFocus=Mock(return_value=True),
			snapshotFromItem=Mock(),
		)
		self.beginTemporaryPaste = Mock()
		self.paster._beginTemporaryPaste = self.beginTemporaryPaste
		self.pngToPackedDib = Mock(return_value=b"dib")
		patcher = patch.object(pasteModule, "pngToPackedDib", self.pngToPackedDib)
		patcher.start()
		self.addCleanup(patcher.stop)

	def paste(self, imageData: bytes = b"png", text: str = "") -> None:
		"""Request one temporary PNG paste."""
		self.paster.pasteSnapshotTemporarily(
			_pngSnapshot(imageData, text),
			"feedback",
			frozenset({0x56}),
			expectedFocus=object(),
		)

	@property
	def isInProgress(self) -> bool:
		"""Whether the paster still refuses a new temporary paste."""
		return self.paster._temporaryPasteInProgress

	def startedRequest(self) -> object:
		"""Return the only request passed to the paste start."""
		self.beginTemporaryPaste.assert_called_once()
		request, retryCount = self.beginTemporaryPaste.call_args.args
		self.assertEqual(0, retryCount)
		return request

	def testSuccessStartsPreparedPaste(self) -> None:
		"""Start the paste on the main thread with the prepared DIB."""
		self.paste()
		self.assertTrue(self.isInProgress)
		self.worker.runAll()
		self.beginTemporaryPaste.assert_not_called()
		self.mainThread.drain()
		request = self.startedRequest()
		self.assertEqual(b"dib", request.preparedPngDib)
		self.assertEqual(b"png", request.snapshot.imageData)
		self.assertTrue(self.isInProgress)

	def testSecondPasteIsRefusedWhilePreparing(self) -> None:
		"""Refuse another temporary paste while one is being prepared."""
		self.paste()
		with self.assertRaisesRegex(RuntimeError, "Pasting is in progress"):
			self.paste()
		self.assertEqual(1, len(self.worker.jobs))

	def testCancelSkipsQueuedPreparation(self) -> None:
		"""Never prepare or start a paste cancelled while queued."""
		self.paste()
		self.paster.cancel()
		self.assertFalse(self.isInProgress)
		self.worker.runAll()
		self.assertEqual(0, self.mainThread.pendingCount)
		self.mainThread.drain()
		self.pngToPackedDib.assert_not_called()
		self.beginTemporaryPaste.assert_not_called()

	def testCancelMakesRunningPreparationStale(self) -> None:
		"""Do not even schedule a result whose paste was cancelled while preparing."""
		self.paste()
		running = self.worker.begin()
		self.paster.cancel()
		self.worker.complete(running)
		self.assertEqual(0, self.mainThread.pendingCount)
		self.beginTemporaryPaste.assert_not_called()

	def testCancelMakesFinishedPreparationStale(self) -> None:
		"""Discard a prepared result whose paste was cancelled before the main thread ran it."""
		self.paste()
		self.worker.runAll()
		self.paster.cancel()
		self.mainThread.drain()
		self.beginTemporaryPaste.assert_not_called()
		self.assertFalse(self.isInProgress)

	def testNewerPasteSupersedesCancelledPreparation(self) -> None:
		"""Start only the newer paste after a cancelled one finishes preparing."""
		self.paste(b"old")
		running = self.worker.begin()
		self.paster.cancel()
		self.paste(b"new")
		self.worker.complete(running)
		self.worker.runAll()
		self.mainThread.drain()
		self.assertEqual(b"new", self.startedRequest().snapshot.imageData)

	def testUndecodableImageReportsPasteFailure(self) -> None:
		"""Report an undecodable image without text as a paste failure with a debug warning."""
		self.pngToPackedDib.return_value = None
		self.paste()
		self.worker.runAll()
		self.mainThread.drain()
		self.beginTemporaryPaste.assert_not_called()
		self.assertEqual([_COULD_NOT_PASTE], self.spokenMessages())
		self.assertEqual(
			["Stored PNG data could not be prepared for temporary paste."],
			self.debugWarnings(),
		)
		self.assertFalse(self.isInProgress)

	def testUndecodableMixedImageFallsBackToText(self) -> None:
		"""Paste the text of a mixed entry whose image cannot be decoded."""
		self.pngToPackedDib.return_value = None
		self.paste(text="caption")
		self.worker.runAll()
		self.mainThread.drain()
		request = self.startedRequest()
		self.assertTrue(request.usedPlainTextFallback)
		self.assertEqual("caption", request.snapshot.text)
		self.assertEqual([], self.spokenMessages())

	def testUnexpectedWorkerErrorIsLoggedAndReported(self) -> None:
		"""Log an unexpected preparation error and report a paste failure."""
		self.pngToPackedDib.side_effect = RuntimeError("decoder crashed")
		self.paste()
		self.worker.runAll()
		self.mainThread.drain()
		self.beginTemporaryPaste.assert_not_called()
		self.assertEqual([_COULD_NOT_PASTE], self.spokenMessages())
		_LOG.exception.assert_called_once()
		self.assertFalse(self.isInProgress)

	def testControllerStoppedBeforeFinishDiscardsResult(self) -> None:
		"""Silently release the paste when the controller stopped before the main thread ran."""
		self.paste()
		self.worker.runAll()
		self.isStarted = False
		self.mainThread.drain()
		self.beginTemporaryPaste.assert_not_called()
		self.assertEqual([], self.spokenMessages())
		self.assertFalse(self.isInProgress)

	def testWxGoneReleasesPasteAndLogsWhileStarted(self) -> None:
		"""Release the paste and log when its result cannot be scheduled while started."""
		self.mainThread.isClosed = True
		self.paste()
		self.worker.runAll()
		self.assertFalse(self.isInProgress)
		self.assertEqual(["Could not schedule a prepared temporary PNG paste."], self.debugWarnings())
		self.beginTemporaryPaste.assert_not_called()

	def testWxGoneAfterStopReleasesPasteSilently(self) -> None:
		"""Release the paste without logging when the controller already stopped."""
		self.paste()
		self.isStarted = False
		self.mainThread.isClosed = True
		self.worker.runAll()
		self.assertFalse(self.isInProgress)
		self.assertEqual([], self.debugWarnings())

	def testWxGoneForStaleResultKeepsNewerPaste(self) -> None:
		"""Ignore a stale result entirely, leaving the newer paste in progress."""
		self.paste(b"old")
		running = self.worker.begin()
		self.paster.cancel()
		self.paste(b"new")
		self.mainThread.isClosed = True
		self.worker.complete(running)
		self.assertEqual([], self.debugWarnings())
		self.assertTrue(self.isInProgress)
		self.mainThread.isClosed = False
		self.worker.runAll()
		self.mainThread.drain()
		self.assertEqual(b"new", self.startedRequest().snapshot.imageData)


class HistoryWritePipelineTests(_PipelineTestCase):
	"""Pin ordered history writes, which are never cancelled or superseded."""

	def setUp(self) -> None:
		"""Create a history writer with fake storage for a started controller."""
		super().setUp()
		self.isStarted = True
		self.storage = Mock()
		self.onItemStored = Mock()
		self.writer = historyWriterModule.HistoryWriter(
			self.storage,
			isStarted=lambda: self.isStarted,
			onItemStored=self.onItemStored,
		)
		self.addCleanup(self.writer.shutdown)

	def waitForWorker(self) -> None:
		"""Wait until every queued job and its completion callback ran on the worker."""
		self.writer.submit(lambda: None).result(timeout=10)

	def queueText(self, text: str, expectedText: str | None = None) -> None:
		"""Queue one plain-text history item."""
		self.writer.queueItem(
			storageModels.ClipboardItem(storageModels.ClipboardItemType.PLAIN_TEXT, text=text),
			expectedText=expectedText,
		)

	def storedResults(self) -> list[tuple[object, str | None]]:
		"""Return the arguments of every stored-item notification."""
		return [call.args for call in self.onItemStored.call_args_list]

	def testSuccessReportsStoredItem(self) -> None:
		"""Report a stored item with its expected text on the main thread."""
		self.storage.addHistory.return_value = 7
		self.queueText("a", expectedText="a")
		self.waitForWorker()
		self.onItemStored.assert_not_called()
		self.mainThread.drain()
		self.assertEqual(
			[(historyWriterModule.HistoryWriteResult(7), "a")],
			self.storedResults(),
		)

	def testEveryWriteIsReportedInOrder(self) -> None:
		"""Report every queued write in order; a newer write never supersedes an older one."""
		self.storage.addHistory.side_effect = [1, 2, 3]
		for text in ("a", "b", "c"):
			self.queueText(text, expectedText=text)
		self.waitForWorker()
		self.mainThread.drain()
		self.assertEqual(
			[
				(historyWriterModule.HistoryWriteResult(itemId), text)
				for itemId, text in ((1, "a"), (2, "b"), (3, "c"))
			],
			self.storedResults(),
		)

	def testWorkerErrorIsReportedOncePerFailureRun(self) -> None:
		"""Report consecutive save failures once and again after a successful save."""
		error = storageModels.StorageError("disk full")
		self.storage.addHistory.side_effect = [error, error, 3, error]
		for text in ("a", "b", "c", "d"):
			self.queueText(text)
		self.waitForWorker()
		self.mainThread.drain()
		failure = "Clipboard updated, but history could not be saved"
		self.assertEqual([failure, failure], self.spokenMessages())
		self.assertEqual(2, _LOG.exception.call_count)
		self.assertEqual([(historyWriterModule.HistoryWriteResult(3), None)], self.storedResults())

	def testRejectedImageIsAnnounced(self) -> None:
		"""Announce an image that history storage refused."""
		self.storage.addHistory.return_value = None
		self.writer.queueItem(
			storageModels.ClipboardItem(storageModels.ClipboardItemType.IMAGE, imageData=b"png"),
		)
		self.waitForWorker()
		self.mainThread.drain()
		self.assertEqual(
			["The image was not added because image history storage is full"],
			self.spokenMessages(),
		)
		self.onItemStored.assert_not_called()

	def testControllerStoppedBeforeFinishDiscardsResult(self) -> None:
		"""Report nothing once the controller stopped before the main thread ran."""
		self.storage.addHistory.side_effect = [1, storageModels.StorageError("disk full")]
		self.queueText("a")
		self.queueText("b")
		self.waitForWorker()
		self.isStarted = False
		self.mainThread.drain()
		self.onItemStored.assert_not_called()
		self.assertEqual([], self.spokenMessages())

	def testWxGoneLogsWhileStarted(self) -> None:
		"""Log an unschedulable history result while started."""
		self.mainThread.isClosed = True
		self.storage.addHistory.return_value = 1
		self.queueText("a")
		self.waitForWorker()
		self.storage.addHistory.assert_called_once()
		self.assertEqual(["Could not schedule a clipboard history write result."], self.debugWarnings())
		self.onItemStored.assert_not_called()

	def testWxGoneAfterStopIsSilent(self) -> None:
		"""Do not log an unschedulable history result after the controller stopped."""
		self.mainThread.isClosed = True
		self.isStarted = False
		self.queueText("a")
		self.waitForWorker()
		self.assertEqual([], self.debugWarnings())

	def testImageSlotsAreReleasedWhenWxIsGone(self) -> None:
		"""Keep storing images after their results could not be scheduled."""
		self.mainThread.isClosed = True
		self.storage.addHistory.return_value = 1
		patcher = patch.object(
			historyWriterModule,
			"normalizeImageSnapshot",
			lambda snapshot: (snapshot, False),
		)
		patcher.start()
		self.addCleanup(patcher.stop)
		for index in range(3):
			self.writer.queueImageSnapshot(_pngSnapshot(f"png {index}".encode()))
			self.waitForWorker()
		storedImages = [call.args[0].imageData for call in self.storage.addHistory.call_args_list]
		self.assertEqual([b"png 0", b"png 1", b"png 2"], storedImages)
		self.assertEqual([], self.spokenMessages())


class _RecordingThread(Thread):
	"""Thread that records every instance so tests can join them."""

	instances: list[_RecordingThread] = []

	def __init__(self, *args: object, **kwargs: object) -> None:
		"""Create and record one thread."""
		super().__init__(*args, **kwargs)
		_RecordingThread.instances.append(self)


class MissingFileCleanupPipelineTests(_PipelineTestCase):
	"""Pin the single-flight missing-file cleanup, which is never cancelled or superseded."""

	def setUp(self) -> None:
		"""Create a started controller whose selected file group has one missing file."""
		super().setUp()
		temporaryDirectory = TemporaryDirectory()
		self.addCleanup(temporaryDirectory.cleanup)
		self.missingPath = str(Path(temporaryDirectory.name) / "missing.txt")
		self.controller = _createController(_ManualWorker())
		self.controller._getStoredItemContentById = Mock(
			return_value=SimpleNamespace(files=(self.missingPath, __file__)),
		)
		self.storage = self.controller.storage
		self.storage.removeMissingFileReferences.return_value = (1, 0, {7: 8})
		self.onComplete = Mock()
		_RecordingThread.instances = []
		patcher = patch.object(controllerModule, "Thread", _RecordingThread)
		patcher.start()
		self.addCleanup(patcher.stop)

	def start(self) -> None:
		"""Request cleanup of one selected entry in a user category."""
		self.controller.startMissingFileCleanup("Work", (7,), self.onComplete)

	@staticmethod
	def joinWorkers() -> None:
		"""Wait for every cleanup worker thread."""
		for thread in _RecordingThread.instances:
			thread.join(timeout=10)

	def blockWorker(self) -> Event:
		"""Make the next cleanup worker wait until the returned event is set."""
		release = Event()
		content = self.controller._getStoredItemContentById.return_value

		def getContent(_category: object, _itemId: int) -> object:
			release.wait(timeout=10)
			return content

		self.controller._getStoredItemContentById.side_effect = getContent
		return release

	def testSuccessReportsResultAndNotifiesSync(self) -> None:
		"""Remove missing references on the worker and report the result on the main thread."""
		self.start()
		self.joinWorkers()
		self.storage.removeMissingFileReferences.assert_called_once_with(
			"Work",
			(7,),
			frozenset({self.missingPath}),
		)
		self.onComplete.assert_not_called()
		self.mainThread.drain()
		self.onComplete.assert_called_once_with(controllerModule.MissingFileCleanupResult(1, 0, {7: 8}))
		self.controller.oneDriveSync.notifyLocalChange.assert_called_once_with()

	def testOnlyOneCleanupRunsAtATime(self) -> None:
		"""Ignore new requests while a cleanup is active, and accept them afterwards."""
		release = self.blockWorker()
		self.start()
		self.start()
		release.set()
		self.joinWorkers()
		self.start()
		self.assertEqual(1, len(_RecordingThread.instances))
		self.mainThread.drain()
		self.start()
		self.joinWorkers()
		self.assertEqual(2, len(_RecordingThread.instances))

	def testNothingMissingSkipsStorage(self) -> None:
		"""Report an empty result without touching storage when every file exists."""
		self.controller._getStoredItemContentById.return_value = SimpleNamespace(files=(__file__,))
		self.start()
		self.joinWorkers()
		self.mainThread.drain()
		self.storage.removeMissingFileReferences.assert_not_called()
		self.onComplete.assert_called_once_with(controllerModule.MissingFileCleanupResult(0, 0, {}))
		self.controller.oneDriveSync.notifyLocalChange.assert_not_called()

	def testWorkerErrorIsReportedAsResult(self) -> None:
		"""Pass a storage failure to the completion callback as a user-facing error."""
		self.storage.removeMissingFileReferences.side_effect = storageModels.InvalidItemError("bad")
		self.start()
		self.joinWorkers()
		self.mainThread.drain()
		self.onComplete.assert_called_once()
		self.assertIsInstance(self.onComplete.call_args.args[0], ValueError)
		self.controller.oneDriveSync.notifyLocalChange.assert_not_called()
		self.start()
		self.assertEqual(2, len(_RecordingThread.instances))

	def testSyncNotificationErrorIsReportedAsResult(self) -> None:
		"""Pass a failed sync notification to the completion callback."""
		error = RuntimeError("sync failed")
		self.controller.oneDriveSync.notifyLocalChange.side_effect = error
		self.start()
		self.joinWorkers()
		self.mainThread.drain()
		self.onComplete.assert_called_once_with(error)

	def testTerminateBeforeCompletionDiscardsResult(self) -> None:
		"""Skip storage and the callback after termination, and release the single-flight guard."""
		release = self.blockWorker()
		self.start()
		self.controller.terminate()
		release.set()
		self.joinWorkers()
		self.mainThread.drain()
		self.storage.removeMissingFileReferences.assert_not_called()
		self.onComplete.assert_not_called()
		self.controller._isStarted = True
		self.start()
		self.assertEqual(2, len(_RecordingThread.instances))

	def testStartIsIgnoredWhileStopped(self) -> None:
		"""Do not start a cleanup while the controller is stopped."""
		self.controller._isStarted = False
		self.start()
		self.assertEqual([], _RecordingThread.instances)

	def testWxGoneReleasesGuardAndLogsWhileStarted(self) -> None:
		"""Release the guard and log when the result cannot be scheduled while started."""
		self.mainThread.isClosed = True
		self.start()
		self.joinWorkers()
		self.assertEqual(["Could not schedule a missing-file cleanup result."], self.debugWarnings())
		self.onComplete.assert_not_called()
		self.mainThread.isClosed = False
		self.start()
		self.assertEqual(2, len(_RecordingThread.instances))

	def testWxGoneAfterStopReleasesGuardSilently(self) -> None:
		"""Release the guard without logging when the controller already stopped."""
		release = self.blockWorker()
		self.start()
		self.controller._isStarted = False
		self.mainThread.isClosed = True
		release.set()
		self.joinWorkers()
		self.assertEqual([], self.debugWarnings())
		self.controller._isStarted = True
		self.mainThread.isClosed = False
		self.start()
		self.assertEqual(2, len(_RecordingThread.instances))


if __name__ == "__main__":
	unittest.main()
