"""Tests for standalone OneDrive synchronization orchestration."""

from __future__ import annotations

import ctypes
from dataclasses import replace
from hashlib import sha1, sha256
import importlib
import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import call, MagicMock, Mock, patch
from uuid import UUID


_MODULE_DIRECTORY = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard"
_PACKAGE_NAME = "nvdaClipboardOneDriveSyncTests"
_PACKAGE = ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_MODULE_DIRECTORY)]
sys.modules[_PACKAGE_NAME] = _PACKAGE
storageModels = importlib.import_module(f"{_PACKAGE_NAME}.storageModels")


def _translate(message: str) -> str:
	"""Return an untranslated message for standalone validation."""
	return message


def _initTranslation() -> None:
	"""Install the standalone translator in the importing module globals."""
	sys._getframe(1).f_globals["_"] = _translate


def _itemBytes(item: storageModels.ClipboardItem) -> bytes:
	"""Return a deterministic representation for fake storage hashes."""
	return repr(
		(
			item.contentType.value,
			item.text,
			item.html,
			item.rtf,
			item.imageData,
			item.imageWidth,
			item.imageHeight,
			item.imageBitDepth,
			item.files,
			item.canUpload,
		),
	).encode("utf-8")


def _getItemPayloadHash(item: storageModels.ClipboardItem) -> bytes:
	"""Return the fake storage payload hash for an item."""
	return sha256(b"payload\0" + _itemBytes(item)).digest()


def _getItemDedupKey(item: storageModels.ClipboardItem) -> bytes:
	"""Return the fake storage history identity for an item."""
	if item.contentType == storageModels.ClipboardItemType.IMAGE:
		primaryContent = ("image", item.imageData)
	elif item.contentType == storageModels.ClipboardItemType.FILES:
		primaryContent = ("files", item.files)
	else:
		primaryContent = ("text", item.text)
	return sha256(b"dedup\0" + repr(primaryContent).encode("utf-8")).digest()


def _isOneDriveSyncItem(item: storageModels.ClipboardItem) -> bool:
	"""Return whether a fake item may participate in synchronization."""
	return item.canUpload and not item.files


class _StorageError(Exception):
	"""Base fake storage error."""


class _StorageFormatError(_StorageError):
	"""Report malformed fake storage data."""


class _ImageStorageLimitError(_StorageError):
	"""Report a fake image storage limit."""


class _OneDriveAccountMismatchError(_StorageError):
	"""Report a fake synchronization account mismatch."""


class _OneDriveLocalChangeError(_StorageError):
	"""Report a fake concurrent local change."""


_ADDON_HANDLER = ModuleType("addonHandler")
_ADDON_HANDLER.initTranslation = _initTranslation
_GUI = ModuleType("gui")
_GUI.__path__ = []
_GUI_MESSAGE = ModuleType("gui.message")
_GUI_MESSAGE.MessageDialog = MagicMock()
_GUI_MESSAGE.ReturnCode = SimpleNamespace(OK=1)
_LOG_HANDLER = ModuleType("logHandler")
_LOG_HANDLER.log = MagicMock()
_REQUESTS = ModuleType("requests")
_REQUESTS.RequestException = OSError
_REQUESTS.Response = object
_REQUESTS.Session = MagicMock
_WX = ModuleType("wx")
_WX.CallAfter = MagicMock()
_STORAGE = ModuleType(f"{_PACKAGE_NAME}.storage")
_STORAGE.ClipboardStorage = object
_STORAGE.ImageStorageLimitError = _ImageStorageLimitError
_STORAGE.OneDriveAccountMismatchError = _OneDriveAccountMismatchError
_STORAGE.OneDriveLocalChangeError = _OneDriveLocalChangeError
_STORAGE.StorageError = _StorageError
_STORAGE.StorageFormatError = _StorageFormatError
_STORAGE.MAX_HISTORY_ITEMS = 1000
_STORAGE.MAX_IMAGE_HISTORY_ITEMS = 20
_STORAGE.MAX_TOTAL_IMAGE_BYTES = 256 * 1024 * 1024
_STORAGE.getItemDedupKey = _getItemDedupKey
_STORAGE.getItemPayloadHash = _getItemPayloadHash
_STORAGE.isOneDriveSyncItem = _isOneDriveSyncItem
_SPEC = importlib.util.spec_from_file_location(
	f"{_PACKAGE_NAME}.oneDriveSync",
	_MODULE_DIRECTORY / "oneDriveSync.py",
)
assert _SPEC is not None and _SPEC.loader is not None
oneDriveSync = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = oneDriveSync
with (
	patch.dict(
		sys.modules,
		{
			"addonHandler": _ADDON_HANDLER,
			"gui": _GUI,
			"gui.message": _GUI_MESSAGE,
			"logHandler": _LOG_HANDLER,
			"requests": _REQUESTS,
			"wx": _WX,
			f"{_PACKAGE_NAME}.storage": _STORAGE,
		},
	),
	patch.object(ctypes, "WinDLL", side_effect=lambda *_args, **_kwargs: MagicMock(), create=True),
):
	_SPEC.loader.exec_module(oneDriveSync)


class _FakeStorage:
	"""Keep synchronization metadata and payloads in memory for orchestration tests."""

	def __init__(self, operationBase: int) -> None:
		"""Create empty storage with a device-specific operation ID range."""
		self.accountId = "account"
		self.snapshot = oneDriveSync.OneDriveSyncSnapshot(self.accountId, (), (), ())
		self.items: dict[bytes, storageModels.ClipboardItem] = {}
		self.excludedPayloads: set[bytes] = set()
		self.applyCalls = 0
		self.failReservations = 0
		self.trace: list[str] | None = None
		self._clock = 0
		self._operationBase = operationBase

	def _nextVersion(self, minimumClock: int = 0) -> storageModels.SyncVersion:
		"""Return a new fake logical version above the requested clock."""
		self._clock = max(self._clock, minimumClock) + 1
		operationId = str(UUID(int=(self._operationBase << 64) | self._clock))
		return storageModels.SyncVersion(self._clock, operationId)

	def addText(self, text: str) -> bytes:
		"""Add one dirty local plain-text history item and return its payload hash."""
		item = storageModels.ClipboardItem(storageModels.ClipboardItemType.PLAIN_TEXT, text=text)
		payloadHash = _getItemPayloadHash(item)
		dedupKey = _getItemDedupKey(item)
		state = storageModels.OneDriveHistoryState(
			dedupKey=dedupKey,
			payloadHash=payloadHash,
			version=self._nextVersion(),
			deleted=False,
			dirty=True,
		)
		self.items[payloadHash] = item
		self.snapshot = replace(
			self.snapshot,
			history=tuple(
				sorted(
					(state, *(value for value in self.snapshot.history if value.dedupKey != dedupKey)),
					key=lambda value: value.dedupKey,
				),
			),
		)
		return payloadHash

	def deletePayload(self, payloadHash: bytes) -> None:
		"""Record a dirty tombstone for one live history payload."""
		found = False
		history = []
		for state in self.snapshot.history:
			if not state.deleted and state.payloadHash == payloadHash:
				state = replace(
					state,
					payloadHash=None,
					version=self._nextVersion(),
					deleted=True,
					dirty=True,
				)
				found = True
			history.append(state)
		if not found:
			raise AssertionError(payloadHash)
		self.snapshot = replace(self.snapshot, history=tuple(history))

	def getOneDriveSnapshot(self) -> storageModels.OneDriveSyncSnapshot:
		"""Return current fake synchronization metadata."""
		return self.snapshot

	def reserveOneDriveVersions(
		self,
		expectedSnapshot: storageModels.OneDriveSyncSnapshot,
		minimumClock: int,
		count: int,
	) -> tuple[storageModels.SyncVersion, ...]:
		"""Reserve versions or report an injected or real fake local race."""
		if self.failReservations:
			self.failReservations -= 1
			raise oneDriveSync.OneDriveLocalChangeError
		if self.snapshot != expectedSnapshot:
			raise oneDriveSync.OneDriveLocalChangeError
		self._clock = max(self._clock, minimumClock)
		return tuple(self._nextVersion() for _index in range(count))

	def getOneDriveItemImageByteCount(self, payloadHash: bytes) -> int | None:
		"""Return a local fake item's image byte count when uploadable."""
		item = None if payloadHash in self.excludedPayloads else self.items.get(payloadHash)
		return None if item is None else len(item.imageData or b"")

	def getOneDriveLocalOnlyCategoryImageByteCounts(self) -> dict[bytes, int]:
		"""Return no retained category images for plain-text tests."""
		return {}

	def getOneDriveItem(self, payloadHash: bytes) -> storageModels.ClipboardItem | None:
		"""Return one local uploadable fake item."""
		return None if payloadHash in self.excludedPayloads else self.items.get(payloadHash)

	def excludeOneDrivePayload(self, payloadHash: bytes) -> bool:
		"""Keep a payload locally while replacing live cloud references with tombstones."""
		if payloadHash not in self.items:
			return False
		wasExcluded = payloadHash in self.excludedPayloads
		self.excludedPayloads.add(payloadHash)
		history = tuple(
			replace(
				state,
				payloadHash=None,
				version=self._nextVersion(),
				deleted=True,
				dirty=True,
			)
			if not state.deleted and state.payloadHash == payloadHash
			else state
			for state in self.snapshot.history
		)
		changed = history != self.snapshot.history
		self.snapshot = replace(self.snapshot, history=history)
		return not wasExcluded or changed

	def applyOneDriveMerge(
		self,
		snapshot: storageModels.OneDriveSyncSnapshot,
		expectedSnapshot: storageModels.OneDriveSyncSnapshot,
		loadItem,
	) -> bool:
		"""Apply a committed snapshot and materialize downloaded payloads."""
		self.applyCalls += 1
		if self.snapshot != expectedSnapshot:
			raise oneDriveSync.OneDriveLocalChangeError
		if self.trace is not None:
			self.trace.append("apply")
		for state in snapshot.history:
			if state.deleted or state.payloadHash is None or state.payloadHash in self.items:
				continue
			item = loadItem(state.payloadHash)
			if item is None:
				raise AssertionError(state.payloadHash)
			self.items[state.payloadHash] = item
		applied = replace(snapshot, accountId=self.accountId)
		changed = applied != self.snapshot
		self.snapshot = applied
		return changed

	@property
	def liveTexts(self) -> set[str]:
		"""Return text from all live fake history states."""
		return {
			self.items[state.payloadHash].text
			for state in self.snapshot.history
			if not state.deleted and state.payloadHash is not None
		}

	def isDeleted(self, payloadHash: bytes) -> bool:
		"""Return whether a payload's history identity is tombstoned."""
		dedupKey = _getItemDedupKey(self.items[payloadHash])
		return any(state.deleted and state.dedupKey == dedupKey for state in self.snapshot.history)


class _FakeGraph:
	"""Store a canonical manifest and content-addressed payloads in memory."""

	def __init__(self) -> None:
		"""Create an empty fake OneDrive app folder."""
		self.snapshot = oneDriveSync._emptySnapshot()
		self.payloads: dict[bytes, bytes] = {}
		self.etag: str | None = None
		self.getManifestCalls = 0
		self.putManifestCalls = 0
		self.putItemCalls = 0
		self.manifestConflicts = 0
		self.cancelAfterList = False
		self.wasListed = False
		self.cancelCleanup = False
		self.events: list[str] = []

	def getManifest(self) -> oneDriveSync._RemoteManifest:
		"""Return current fake cloud metadata."""
		self.getManifestCalls += 1
		return oneDriveSync._RemoteManifest(self.snapshot, self.etag, self.etag is None)

	def listItemFiles(
		self,
		relevantPayloads: set[bytes],
	) -> tuple[dict[bytes, oneDriveSync._RemoteItemFile], tuple[tuple[bytes, str], ...]]:
		"""Return relevant payload metadata and all fake orphan files."""
		self.wasListed = True
		self.events.append("list")
		itemFiles = {
			payloadHash: oneDriveSync._RemoteItemFile(
				size=len(data),
				sha1Hash=sha1(data).hexdigest(),
			)
			for payloadHash, data in self.payloads.items()
			if payloadHash in relevantPayloads
		}
		orphanFiles = tuple(
			(payloadHash, f'"{payloadHash.hex()}"')
			for payloadHash in sorted(self.payloads)
			if payloadHash not in relevantPayloads
		)
		return itemFiles, orphanFiles

	def getItemData(self, payloadHash: bytes) -> bytes:
		"""Return one fake cloud payload."""
		return self.payloads[payloadHash]

	def putItemData(self, payloadHash: bytes, data: bytes) -> None:
		"""Store one fake cloud payload."""
		self.putItemCalls += 1
		self.events.append("putItem")
		self.payloads[payloadHash] = data

	def putManifest(self, data: bytes, _remote: oneDriveSync._RemoteManifest) -> str:
		"""Conditionally commit fake cloud metadata."""
		self.putManifestCalls += 1
		self.events.append("putManifest")
		if _remote.etag != self.etag or _remote.isMissing != (self.etag is None):
			raise oneDriveSync._ManifestConflictError("stale manifest", statusCode=412)
		if self.manifestConflicts:
			self.manifestConflicts -= 1
			raise oneDriveSync._ManifestConflictError("conflict", statusCode=412)
		snapshot = oneDriveSync._decodeManifest(data)
		missingPayloads = oneDriveSync._livePayloadHashes(snapshot) - self.payloads.keys()
		if missingPayloads:
			raise AssertionError(missingPayloads)
		self.snapshot = snapshot
		self.etag = f'"manifest-{self.putManifestCalls}"'
		return self.etag

	def deleteItemData(self, payloadHash: bytes, _etag: str) -> None:
		"""Delete one fake orphan or inject cleanup cancellation."""
		self.events.append("delete")
		if self.cancelCleanup:
			raise oneDriveSync._OperationCancelledError("cancelled")
		self.payloads.pop(payloadHash, None)


class OneDriveSyncTests(unittest.TestCase):
	"""Verify synchronization retries, ordering, and convergence."""

	def testImportDefersMsal(self) -> None:
		"""Keep bundled MSAL unloaded until authentication is needed."""
		self.assertNotIn(f"{_PACKAGE_NAME}._vendor.msal", sys.modules)

	def _makeManager(self, storage: _FakeStorage) -> oneDriveSync.OneDriveSyncManager:
		"""Return a minimally initialized manager for staged synchronization."""
		manager = object.__new__(oneDriveSync.OneDriveSyncManager)
		manager._storage = storage
		manager._ensureActive = Mock()
		manager._updateProgress = Mock()
		return manager

	def _sync(
		self,
		storage: _FakeStorage,
		graph: _FakeGraph,
		manager: oneDriveSync.OneDriveSyncManager | None = None,
	) -> oneDriveSync._SyncResult:
		"""Run one complete staged synchronization with a temporary payload stage."""
		manager = manager or self._makeManager(storage)
		storage.trace = graph.events
		token = oneDriveSync._AccountToken("token", storage.accountId, "Account", {})
		with TemporaryDirectory() as temporaryDirectory:
			return manager._synchronizeStaged(
				token,
				graph,
				oneDriveSync._ItemStage(Path(temporaryDirectory)),
			)

	def testInterruptedResponseStreamClosesResponse(self) -> None:
		"""Convert a mid-stream Graph disconnect and close its response."""
		client = oneDriveSync._GraphClient("token", oneDriveSync.Event(), Mock())
		response = Mock()
		response.headers = {}

		def interruptedChunks(_chunkSize: int):
			"""Yield one chunk before simulating a connection failure."""
			yield b"first"
			raise oneDriveSync.requests.RequestException("disconnected")

		response.iter_content.side_effect = interruptedChunks
		with self.assertRaises(oneDriveSync.OneDriveError) as context:
			client._readBoundedResponse(response, 1024)
		self.assertEqual(str(context.exception), "Could not connect to OneDrive")
		response.close.assert_called_once_with()

	def testRemoteItemHashSelection(self) -> None:
		"""Prefer SHA-1 while retaining QuickXorHash as a fallback."""
		data = b"payload"
		with patch.object(oneDriveSync, "_quickXorHash") as quickXorHash:
			self.assertTrue(
				oneDriveSync._remoteItemMatches(
					oneDriveSync._RemoteItemFile(len(data), sha1(data).hexdigest(), b"x" * 20),
					data,
				),
			)
		quickXorHash.assert_not_called()
		self.assertTrue(
			oneDriveSync._remoteItemMatches(
				oneDriveSync._RemoteItemFile(len(data), None, oneDriveSync._quickXorHash(data)),
				data,
			),
		)

	def testHugeNumericResponseHeadersRemainBoundedAndClose(self) -> None:
		"""Ignore unparseable numeric headers while enforcing bodies and closing responses."""
		hugeNumber = "9" * 5000
		client = oneDriveSync._GraphClient("token", oneDriveSync.Event(), Mock())
		contentResponse = Mock()
		contentResponse.headers = {"Content-Length": hugeNumber}
		contentResponse.iter_content.return_value = (b"12345",)
		with self.assertRaises(oneDriveSync.OneDriveError) as context:
			client._readBoundedResponse(contentResponse, 4)
		self.assertEqual(str(context.exception), oneDriveSync._ERROR_FILE_TOO_LARGE)
		contentResponse.iter_content.assert_called_once_with(64 * 1024)
		contentResponse.close.assert_called_once_with()

		retryResponse = Mock()
		retryResponse.status_code = 429
		retryResponse.headers = {"Retry-After": hugeNumber}
		retryResponse.json.return_value = {}
		session = Mock()
		session.request.return_value = retryResponse
		client = oneDriveSync._GraphClient("token", oneDriveSync.Event(), session)
		with self.assertRaises(oneDriveSync.OneDriveError) as context:
			client._request("GET", "https://graph.microsoft.com/v1.0/test", expected=(200,))
		self.assertIsNone(context.exception.retryAfter)
		session.request.assert_called_once()
		retryResponse.close.assert_called_once_with()

	def testPeriodicPollSkipsThenForcesFullSynchronization(self) -> None:
		"""Skip unchanged polls while retaining an hourly payload integrity pass."""
		etag = '"manifest-1"'
		token = oneDriveSync._AccountToken("token", "account", "Account", {})
		storage = Mock()
		storage.hasPendingOneDriveChanges.return_value = False
		manager = object.__new__(oneDriveSync.OneDriveSyncManager)
		manager._storage = storage
		manager._operationCancelEvent = oneDriveSync.Event()
		manager._lock = oneDriveSync.Lock()
		manager._accountToken = token
		manager._lastManifestEtag = etag
		manager._lastFullSyncAt = oneDriveSync.monotonic()
		manager._updateProgress = Mock()
		response = Mock(status_code=304)
		session = Mock()
		session.request.return_value = response
		sessionContext = MagicMock()
		sessionContext.__enter__.return_value = session
		with patch.object(oneDriveSync.requests, "Session", return_value=sessionContext):
			result = manager._synchronize(token, isPoll=True)
		self.assertEqual(result, oneDriveSync._SyncResult(0, 0, False, etag))
		storage.hasPendingOneDriveChanges.assert_called_once_with()
		session.request.assert_called_once()
		self.assertEqual(session.request.call_args.kwargs["headers"]["If-None-Match"], etag)
		response.close.assert_called_once_with()

		fullResult = oneDriveSync._SyncResult(0, 0, False, etag)
		manager._lastFullSyncAt = oneDriveSync.monotonic() - oneDriveSync._SYNC_FULL_SCAN_INTERVAL_SECONDS
		manager._synchronizeStaged = Mock(return_value=fullResult)
		response.status_code = 200
		session.request.reset_mock()
		with patch.object(oneDriveSync.requests, "Session", return_value=sessionContext):
			result = manager._synchronize(token, isPoll=True)
		self.assertEqual(result, fullResult)
		manager._synchronizeStaged.assert_called_once()
		storage.hasPendingOneDriveChanges.assert_called_once_with()
		self.assertNotIn("If-None-Match", session.request.call_args.kwargs["headers"])

	def testBusyPollDoesNotQueueRedundantFullSynchronization(self) -> None:
		"""Drop a busy poll while retaining a queued local-change synchronization."""
		manager = object.__new__(oneDriveSync.OneDriveSyncManager)
		manager._lock = oneDriveSync.Lock()
		manager._state = oneDriveSync.OneDriveSyncState(True, True, True, False, "Account", "Busy")
		manager._isTerminated = False
		manager._hasPendingSync = False
		manager._requestSync(onDone=None, isAutomatic=True, isPoll=True)
		self.assertFalse(manager._hasPendingSync)
		manager._requestSync(onDone=None, isAutomatic=True, isPoll=False)
		self.assertTrue(manager._hasPendingSync)

	def testCallbackFailuresDoNotDropPendingSynchronization(self) -> None:
		"""Attempt every result callback and run pending synchronization after failures."""
		manager = object.__new__(oneDriveSync.OneDriveSyncManager)
		manager._lock = oneDriveSync.Lock()
		manager._isTerminated = False
		callbacks = Mock()
		callbacks.state.side_effect = RuntimeError("destroyed state window")
		callbacks.data.side_effect = RuntimeError("destroyed data window")
		callbacks.done.side_effect = RuntimeError("destroyed completion window")
		manager._onStateChanged = callbacks.state
		manager._onDataChanged = callbacks.data
		manager._requestSync = callbacks.sync

		manager._deliverOperationResult(callbacks.done, True, "complete", True, True)

		self.assertEqual(
			callbacks.mock_calls,
			[
				call.state(),
				call.data(),
				call.done(True, "complete"),
				call.sync(onDone=None, isAutomatic=True),
			],
		)

	def testDispatchFailureRetainsPendingSynchronization(self) -> None:
		"""Retain queued work when wx rejects final result delivery."""
		manager = object.__new__(oneDriveSync.OneDriveSyncManager)
		manager._lock = oneDriveSync.Lock()
		manager._isTerminated = False
		manager._hasPendingSync = False
		manager._state = oneDriveSync.OneDriveSyncState(True, True, False, False, "Account", "Ready")

		with patch.object(oneDriveSync.wx, "CallAfter", side_effect=RuntimeError("wx stopped")):
			manager._postOperationResult(None, True, "complete", False, True)

		self.assertTrue(manager._hasPendingSync)

	def testIndependentCallbackFailuresAreIsolated(self) -> None:
		"""Prevent standalone state, device-code, and completion callbacks from escaping."""
		manager = object.__new__(oneDriveSync.OneDriveSyncManager)
		manager._lock = oneDriveSync.Lock()
		manager._isTerminated = False
		manager._deviceCodeCancellationRequested = False
		callbacks = Mock()
		callbacks.state.side_effect = RuntimeError("state window destroyed")
		callbacks.device.side_effect = RuntimeError("device window destroyed")
		callbacks.done.side_effect = RuntimeError("completion window destroyed")
		manager._onStateChanged = callbacks.state

		manager._deliverStateChanged()
		manager._deliverDeviceCode(callbacks.device, "code", "url")
		manager._deliverCompletion(callbacks.done, True, "done")

		callbacks.state.assert_called_once_with()
		callbacks.device.assert_called_once_with("code", "url")
		callbacks.done.assert_called_once_with(True, "done")

	def testCacheDirectoryFailureBecomesOneDriveError(self) -> None:
		"""Normalize a token-cache parent path collision to a user-safe error."""
		with TemporaryDirectory() as temporaryDirectory:
			blockedParent = Path(temporaryDirectory) / "blocked"
			blockedParent.write_bytes(b"")
			auth = object.__new__(oneDriveSync._AuthManager)
			auth.cachePath = blockedParent / "cache.dat"
			auth.cache = SimpleNamespace(has_state_changed=True)
			with patch.object(oneDriveSync, "_shouldWriteToDisk", return_value=True):
				with self.assertRaises(oneDriveSync.OneDriveError) as context:
					auth._saveCache()
			self.assertEqual(str(context.exception), "Could not save Microsoft sign-in data")

	def testTwoDeviceLifecycleConverges(self) -> None:
		"""Upload, download, merge concurrent additions, and synchronize deletion."""
		graph = _FakeGraph()
		first = _FakeStorage(1)
		second = _FakeStorage(2)
		firstPayload = first.addText("first")
		self.assertEqual(self._sync(first, graph).uploaded, 1)
		with patch.object(oneDriveSync, "_decodeItem", wraps=oneDriveSync._decodeItem) as decodeItem:
			self.assertEqual(self._sync(second, graph).downloaded, 1)
		self.assertEqual(decodeItem.call_count, 2)
		self.assertEqual(second.liveTexts, {"first"})

		first.addText("from first")
		second.addText("from second")
		self._sync(first, graph)
		self._sync(second, graph)
		self._sync(first, graph)
		self.assertEqual(first.liveTexts, {"first", "from first", "from second"})
		self.assertEqual(second.liveTexts, first.liveTexts)
		self.assertEqual(first.snapshot, replace(graph.snapshot, accountId=first.accountId))
		self.assertEqual(second.snapshot, replace(graph.snapshot, accountId=second.accountId))

		first.deletePayload(firstPayload)
		self._sync(first, graph)
		self._sync(second, graph)
		self._sync(first, graph)
		self.assertTrue(first.isDeleted(firstPayload))
		self.assertTrue(second.isDeleted(firstPayload))
		self.assertNotIn(firstPayload, graph.payloads)

	def testEmptyCloudCreatesManifestForPolling(self) -> None:
		"""Create one empty manifest so later unchanged polls have an ETag."""
		graph = _FakeGraph()
		result = self._sync(_FakeStorage(7), graph)
		self.assertEqual(graph.putManifestCalls, 1)
		self.assertEqual(result.manifestEtag, graph.etag)

	def testLocalAndManifestConflictsRetryBeforeApply(self) -> None:
		"""Retry local and cloud conflicts and apply only the committed attempt."""
		graph = _FakeGraph()
		graph.manifestConflicts = 1
		storage = _FakeStorage(3)
		storage.failReservations = 1
		storage.addText("retry")
		result = self._sync(storage, graph)
		self.assertEqual(result.uploaded, 1)
		self.assertEqual(graph.getManifestCalls, 3)
		self.assertEqual(graph.putManifestCalls, 2)
		self.assertEqual(storage.applyCalls, 1)
		self.assertEqual(storage.snapshot, replace(graph.snapshot, accountId=storage.accountId))

	def testCancellationBeforeCommitLeavesLocalStateUntouched(self) -> None:
		"""Propagate cancellation after listing without committing or applying data."""
		graph = _FakeGraph()
		graph.cancelAfterList = True
		storage = _FakeStorage(4)
		storage.addText("cancel")
		manager = self._makeManager(storage)
		originalSnapshot = storage.snapshot

		def ensureActive() -> None:
			"""Raise after the fake Graph listing reaches the cancellation point."""
			if graph.cancelAfterList and graph.wasListed:
				raise oneDriveSync._OperationCancelledError("cancelled")

		manager._ensureActive = ensureActive
		with self.assertRaises(oneDriveSync._OperationCancelledError):
			self._sync(storage, graph, manager)
		self.assertEqual(graph.putManifestCalls, 0)
		self.assertEqual(storage.applyCalls, 0)
		self.assertEqual(storage.snapshot, originalSnapshot)

	def testOversizedPayloadRemainsLocal(self) -> None:
		"""Exclude an over-encoded payload from cloud state without deleting local data."""
		graph = _FakeGraph()
		storage = _FakeStorage(5)
		payloadHash = storage.addText("oversized")
		originalEncodeItem = oneDriveSync._encodeItem

		def encodeItem(item: storageModels.ClipboardItem) -> bytes:
			"""Reject the selected fake payload and encode every other item normally."""
			if _getItemPayloadHash(item) == payloadHash:
				raise oneDriveSync._ItemSizeLimitError("too large")
			return originalEncodeItem(item)

		with patch.object(oneDriveSync, "_encodeItem", side_effect=encodeItem):
			result = self._sync(storage, graph)
		self.assertTrue(result.changedLocally)
		self.assertIn(payloadHash, storage.items)
		self.assertTrue(storage.isDeleted(payloadHash))
		self.assertNotIn(payloadHash, graph.payloads)
		self.assertEqual(graph.getManifestCalls, 2)

	def testCleanupCancellationDoesNotUndoCommit(self) -> None:
		"""Treat cancellation during best-effort orphan cleanup as a successful sync."""
		graph = _FakeGraph()
		orphanHash = b"o" * 32
		graph.payloads[orphanHash] = b"orphan"
		graph.cancelCleanup = True
		storage = _FakeStorage(6)
		result = self._sync(storage, graph)
		self.assertEqual(result, oneDriveSync._SyncResult(0, 0, False, graph.etag))
		self.assertEqual(graph.putManifestCalls, 1)
		self.assertEqual(storage.applyCalls, 1)
		self.assertIn(orphanHash, graph.payloads)
		self.assertLess(graph.events.index("putManifest"), graph.events.index("apply"))
		self.assertLess(graph.events.index("apply"), graph.events.index("delete"))


if __name__ == "__main__":
	unittest.main()
