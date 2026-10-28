# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Synchronize permitted clipboard history and categories with OneDrive."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from hashlib import sha1
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread, Timer
from time import monotonic

import addonHandler
from gui.message import MessageDialog, ReturnCode
from logHandler import log
import NVDAState
import requests

from ..background import queueMainThread
from .auth import (
	ERROR_AUTH_UNAVAILABLE,
	TOKEN_CACHE_FILE_NAME,
	AccountToken,
	AuthenticationUnavailableError,
	AuthManager,
)
from .graph import (
	ERROR_SIGN_IN_EXPIRED,
	GraphClient,
	ManifestConflictError,
	OperationCancelledError,
	RemoteItemFile,
	RemoteManifest,
	remoteItemMatches,
)
from .protocol import (
	MAX_ITEM_FILE_BYTES,
	OneDriveError,
	_ItemSizeLimitError,
	_ManifestSizeLimitError,
	INVALID_SYNC_DATA_MESSAGE,
	_MAX_SYNC_CLOCK,
	_applyNormalization,
	_cleanSnapshot,
	_cloudSnapshot,
	_compactSnapshot,
	_decodeItem,
	_encodeItem,
	_encodeManifest,
	_generationRebaseStates,
	_livePayloadHashes,
	_maximumClock,
	_mergeSnapshots,
	_normalizationTargets,
	_rebaseLocalSnapshot,
	_tombstoneCount,
)
from ..storage.database import (
	ClipboardStorage,
	ImageStorageLimitError,
	OneDriveAccountMismatchError,
	OneDriveLocalChangeError,
	StorageError,
	StorageFormatError,
)
from ..storage.models import (
	ClipboardItem,
	ItemNotFoundError,
	OneDriveSyncSnapshot,
)


addonHandler.initTranslation()

_SYNC_DEBOUNCE_SECONDS = 30
_SYNC_POLL_SECONDS = 5 * 60
# ponytail: hourly full scans bound payload repair; use Graph delta tokens only if this scan cost matters.
_SYNC_FULL_SCAN_INTERVAL_SECONDS = 60 * 60
_PROGRESS_UPDATE_SECONDS = 2.0
_MANIFEST_RETRIES = 3
_MAX_TOMBSTONES_BEFORE_COMPACTION = 4096

# Translators: Generic error shown when Microsoft sign-in fails.
_ERROR_SIGN_IN_FAILED = _("Sign in failed")
# Translators: Error shown when OneDrive synchronization is requested without signing in.
_ERROR_SIGN_IN_REQUIRED = _("Sign in to OneDrive before synchronizing")

# Translators: OneDrive status while cached Microsoft sign-in is checked.
_STATUS_CHECKING = _("Checking...")
# Translators: OneDrive status when no cached Microsoft account is usable.
_STATUS_NOT_SIGNED_IN = _("Not signed in")
# Translators: OneDrive progress while cloud synchronization metadata is read.
_STATUS_READING_SYNC_DATA = _("Reading OneDrive synchronization data...")
# Translators: OneDrive status while cached Microsoft credentials are removed.
_STATUS_SIGNING_OUT = _("Signing out...")
# Translators: OneDrive status while synchronization is running or about to start.
_STATUS_SYNCHRONIZING = _("Synchronizing...")

_CompletionCallback = Callable[[bool, str], None]
_DeviceCodeCallback = Callable[[str, str], None]


@dataclass(frozen=True, slots=True)
class OneDriveSyncState:
	"""Describe the current OneDrive state for user interfaces."""

	isAvailable: bool
	isLoggedIn: bool
	isBusy: bool
	isSigningOut: bool
	accountName: str
	statusMessage: str


class _ItemStage:
	"""Store downloaded immutable payloads on disk and load only one at a time."""

	def __init__(self, path: Path) -> None:
		"""Create a payload stage inside an operation-owned temporary directory."""
		self.path = path

	def store(self, payloadHash: bytes, data: bytes) -> None:
		"""Store one bounded payload that already passed full validation, including PNG decoding."""
		if len(payloadHash) != 32 or len(data) > MAX_ITEM_FILE_BYTES:
			raise ValueError(payloadHash)
		self._path(payloadHash).write_bytes(data)

	def load(self, payloadHash: bytes) -> ClipboardItem | None:
		"""Load and validate one staged payload, or return ``None`` when absent.

		Staged payloads were fully validated before ``store``, so reloading checks the
		structure and content hash again but skips the repeated PNG decode.
		"""
		data = self.read(payloadHash)
		return _decodeItem(data, payloadHash, verifyImageDecodable=False) if data is not None else None

	def read(self, payloadHash: bytes) -> bytes | None:
		"""Read one staged payload, or return ``None`` when absent."""
		path = self._path(payloadHash)
		return path.read_bytes() if path.exists() else None

	def _path(self, payloadHash: bytes) -> Path:
		"""Return the private staging path for one validated payload hash."""
		if len(payloadHash) != 32:
			raise ValueError(payloadHash)
		return self.path / payloadHash.hex()


@dataclass(frozen=True, slots=True)
class _SyncResult:
	"""Describe one synchronization result and its observed manifest ETag."""

	uploaded: int
	downloaded: int
	changedLocally: bool
	manifestEtag: str | None


class OneDriveSyncManager:
	"""Coordinate Microsoft sign-in and serialized background synchronization."""

	def __init__(
		self,
		storage: ClipboardStorage,
		*,
		onStateChanged: Callable[[], None] | None = None,
		onDataChanged: Callable[[], None] | None = None,
	) -> None:
		"""Create a manager around one clipboard database and Microsoft account."""
		self._storage = storage
		self._cachePath = storage.path.with_name(TOKEN_CACHE_FILE_NAME)
		self._onStateChanged = onStateChanged
		self._onDataChanged = onDataChanged
		self._lock = Lock()
		self._auth: AuthManager | None = None
		self._accountToken: AccountToken | None = None
		self._debounceTimer: Timer | None = None
		self._debounceGeneration = 0
		self._pollTimer: Timer | None = None
		self._lastManifestEtag: str | None = None
		self._lastFullSyncAt: float | None = None
		self._hasPendingSync = False
		self._signOutPending = False
		self._pendingSignOutDone: _CompletionCallback | None = None
		self._operationCancelEvent = Event()
		self._lastProgressUpdateAt = 0.0
		self._deviceCodeCancellationRequested = False
		self._isInitialized = False
		self._isTerminated = False
		self._state = OneDriveSyncState(
			isAvailable=True,
			isLoggedIn=False,
			isBusy=False,
			isSigningOut=False,
			accountName="",
			statusMessage=_STATUS_CHECKING,
		)

	def initialize(self) -> None:
		"""Check cached Microsoft login and synchronize silently after NVDA starts."""
		with self._lock:
			if self._isInitialized or self._isTerminated:
				return
			self._isInitialized = True
		busyMessage = _STATUS_CHECKING
		# Translators: Generic error when startup OneDrive initialization fails unexpectedly.
		failureMessage = _("OneDrive setup failed")
		self._startOperation(
			self._initializeWorker,
			name="nvdaClipboard.oneDriveStartup",
			busyMessage=busyMessage,
			failureMessage=failureMessage,
			onDone=None,
		)

	def terminate(self) -> None:
		"""Cancel timers and suppress results from unfinished daemon operations."""
		with self._lock:
			if self._isTerminated:
				return
			self._isTerminated = True
			self._isInitialized = False
			self._cancelTimersLocked()
			self._hasPendingSync = False
			self._signOutPending = False
			self._pendingSignOutDone = None
			self._operationCancelEvent.set()
			auth = self._auth
			self._onStateChanged = None
			self._onDataChanged = None
		if auth is not None:
			auth.cancelDeviceFlow()

	def getState(self) -> OneDriveSyncState:
		"""Return an immutable snapshot of current OneDrive state."""
		with self._lock:
			return self._state

	def loginInteractive(
		self,
		parentWindowHandle: int | None,
		onDone: _CompletionCallback | None = None,
	) -> None:
		"""Sign in through the system browser and synchronize after success."""

		def work() -> tuple[str, bool]:
			"""Complete browser login and initial synchronization in the worker."""
			token = self._getAuth().loginInteractive(parentWindowHandle)
			return self._finishLogin(token)

		# Translators: OneDrive status while the system browser handles Microsoft login.
		busyMessage = _("Signing in...")
		failureMessage = _ERROR_SIGN_IN_FAILED
		self._startOperation(
			work,
			name="nvdaClipboard.oneDriveLogin",
			busyMessage=busyMessage,
			failureMessage=failureMessage,
			onDone=onDone,
		)

	def startDeviceCode(
		self,
		onCode: _DeviceCodeCallback,
		onDone: _CompletionCallback | None = None,
	) -> None:
		"""Start fallback device-code login and report its code on the GUI thread."""

		def work() -> tuple[str, bool]:
			"""Create, display, and complete one Microsoft device-code flow."""
			auth = self._getAuth()
			flow, userCode, verificationUrl = auth.initiateDeviceFlow()
			with self._lock:
				wasCancelled = self._deviceCodeCancellationRequested
			if wasCancelled:
				auth.cancelDeviceFlow()
			else:
				self._postDeviceCode(onCode, userCode, verificationUrl)
			self._ensureActive()
			return self._finishLogin(auth.completeDeviceFlow(flow))

		with self._lock:
			self._deviceCodeCancellationRequested = False
		# Translators: OneDrive status while Microsoft device-code login is running.
		busyMessage = _("Waiting for sign-in...")
		failureMessage = _ERROR_SIGN_IN_FAILED
		self._startOperation(
			work,
			name="nvdaClipboard.oneDriveDeviceLogin",
			busyMessage=busyMessage,
			failureMessage=failureMessage,
			onDone=onDone,
		)

	def cancelDeviceCode(self) -> None:
		"""Cancel any active Microsoft device-code login without blocking the GUI."""
		with self._lock:
			self._deviceCodeCancellationRequested = True
			auth = self._auth
			if self._isTerminated:
				return
		if auth is not None:
			auth.cancelDeviceFlow()

	def syncNow(self, onDone: _CompletionCallback | None = None) -> None:
		"""Synchronize current history and categories immediately."""
		self._requestSync(onDone=onDone, isAutomatic=False)

	def signOut(self, onDone: _CompletionCallback | None = None) -> None:
		"""Cancel current synchronization, then remove cached Microsoft credentials."""
		with self._lock:
			if self._isTerminated or self._state.isSigningOut:
				return
			shouldQueue = self._state.isBusy
			if shouldQueue:
				self._signOutPending = True
				self._pendingSignOutDone = onDone
				self._hasPendingSync = False
				self._cancelTimersLocked()
				self._operationCancelEvent.set()
			if shouldQueue or (self._isInitialized and self._state.isAvailable):
				self._state = replace(
					self._state,
					isSigningOut=True,
					statusMessage=_STATUS_SIGNING_OUT,
				)
		if shouldQueue:
			self._postStateChanged()
			return
		self._startSignOut(onDone)

	def _startSignOut(self, onDone: _CompletionCallback | None) -> None:
		"""Start credential removal after prior work has stopped."""
		self._startOperation(
			self._signOutWorker,
			name="nvdaClipboard.oneDriveSignOut",
			busyMessage=_STATUS_SIGNING_OUT,
			# Translators: Generic error when OneDrive sign-out fails unexpectedly.
			failureMessage=_("Sign out failed"),
			onDone=onDone,
			isSigningOut=True,
		)

	def notifyLocalChange(self) -> None:
		"""Debounce a synchronization after eligible local data changes."""
		with self._lock:
			if self._isTerminated or not self._isInitialized or not self._state.isLoggedIn:
				return
			if self._debounceTimer is not None:
				self._debounceTimer.cancel()
			self._debounceGeneration += 1
			timer = Timer(
				_SYNC_DEBOUNCE_SECONDS,
				self._debounceTimerFired,
				args=(self._debounceGeneration,),
			)
			timer.daemon = True
			self._debounceTimer = timer
			try:
				timer.start()
			except RuntimeError:
				self._debounceTimer = None
				log.exception("Could not start the OneDrive local-change timer.")

	def _initializeWorker(self) -> tuple[str, bool]:
		"""Restore a bound cached account and run the startup synchronization."""
		self._ensureActive()
		if not self._cachePath.is_file() or not NVDAState.shouldWriteToDisk():
			self._setAccountToken(None)
			return _STATUS_NOT_SIGNED_IN, False
		auth = self._getAuth()
		self._ensureActive()
		accountId = self._storage.getOneDriveSnapshot().accountId
		token = auth.getCachedToken(accountId)
		self._ensureActive()
		if token is None:
			self._setAccountToken(None)
			return _STATUS_NOT_SIGNED_IN, False
		self._storage.bindOneDriveAccount(token.accountId)
		self._setAccountToken(token)
		self._requestSync(onDone=None, isAutomatic=True)
		return _STATUS_SYNCHRONIZING, False

	def _finishLogin(self, token: AccountToken) -> tuple[str, bool]:
		"""Confirm any account switch, bind it locally, and synchronize."""
		self._ensureActive()
		currentAccountId = self._storage.getOneDriveSnapshot().accountId
		isSwitch = currentAccountId is not None and currentAccountId != token.accountId
		if isSwitch:
			message = _(
				# Translators: Confirmation before linking this NVDA configuration to a different account.
				"This NVDA configuration is linked to another Microsoft account. "
				"Switching to {account} resets its synchronization baseline and merges "
				"the current local history and categories with that account's OneDrive. Continue?",
			).format(account=token.accountName)
			# Translators: Title of the OneDrive account-switch confirmation.
			caption = _("Switch OneDrive account")
			# Translators: Confirmation button that switches the OneDrive account.
			okLabel = _("&Switch account")
			if MessageDialog.confirm(message, caption, okLabel=okLabel) != ReturnCode.OK:
				try:
					self._getAuth().removeAccount(token.account)
				except Exception:
					log.debugWarning("Could not remove the unselected Microsoft account.", exc_info=True)
				# Translators: Status after canceling a OneDrive account switch.
				raise OperationCancelledError(_("OneDrive account switch was canceled"))
		self._ensureActive()
		self._storage.bindOneDriveAccount(token.accountId, reset=isSwitch)
		self._setAccountToken(token)
		self._requestSync(onDone=None, isAutomatic=True)
		return _STATUS_SYNCHRONIZING, False

	def _signOutWorker(self) -> tuple[str, bool]:
		"""Remove all MSAL accounts and clear runtime login state."""
		with self._lock:
			self._cancelTimersLocked()
			self._hasPendingSync = False
		try:
			self._getAuth().signOut()
		finally:
			with self._lock:
				self._cancelTimersLocked()
				self._hasPendingSync = False
		self._setAccountToken(None)
		# Translators: Status after removing the cached Microsoft account.
		return _("Signed out"), False

	def _requestSync(
		self,
		*,
		onDone: _CompletionCallback | None,
		isAutomatic: bool,
		isPoll: bool = False,
	) -> None:
		"""Start one synchronization or retain an automatic request while busy."""
		with self._lock:
			isLoggedIn = self._state.isLoggedIn
			isBusy = self._state.isBusy
			isTerminated = self._isTerminated
			if isAutomatic and isBusy and isLoggedIn and not isTerminated:
				# Poll timers schedule their successor before requesting synchronization.
				if not isPoll:
					self._hasPendingSync = True
				return
		if not isLoggedIn:
			self._postCompletion(onDone, False, _ERROR_SIGN_IN_REQUIRED)
			return
		busyMessage = _STATUS_SYNCHRONIZING
		# Translators: Generic error when OneDrive synchronization fails unexpectedly.
		failureMessage = _("Synchronization failed")
		self._startOperation(
			partial(self._syncWorker, isPoll=isPoll),
			name="nvdaClipboard.oneDriveSync",
			busyMessage=busyMessage,
			failureMessage=failureMessage,
			onDone=onDone,
			requiresLogin=True,
		)

	def _syncWorker(self, *, isPoll: bool = False) -> tuple[str, bool]:
		"""Run one synchronization and return its user-facing completion state."""
		result = self._synchronizeWithRefresh(isPoll=isPoll)
		if result.uploaded or result.downloaded or result.changedLocally:
			log.debug(
				f"OneDrive synchronization changed data: uploaded={result.uploaded}, "
				f"downloaded={result.downloaded}, localChanged={result.changedLocally}",
			)
		# Translators: Status after a complete OneDrive synchronization succeeds.
		return _("Synchronization complete"), result.changedLocally

	def _synchronizeWithRefresh(self, *, isPoll: bool = False) -> _SyncResult:
		"""Synchronize with one normal and at most one forced silent token refresh."""
		token = self._getAccountToken()
		if token is None:
			raise OneDriveError(ERROR_SIGN_IN_EXPIRED, statusCode=401)
		auth = self._getAuth()
		cachedToken = auth.getCachedToken(token.accountId)
		if cachedToken is None:
			self._setAccountToken(None)
			raise OneDriveError(ERROR_SIGN_IN_EXPIRED, statusCode=401)
		self._setAccountToken(cachedToken)
		try:
			return self._synchronize(cachedToken, isPoll=isPoll)
		except OneDriveError as error:
			if error.statusCode != 401:
				raise
		refreshedToken = auth.getCachedToken(cachedToken.accountId, forceRefresh=True)
		if refreshedToken is None:
			self._setAccountToken(None)
			raise OneDriveError(ERROR_SIGN_IN_EXPIRED, statusCode=401)
		self._setAccountToken(refreshedToken)
		try:
			return self._synchronize(refreshedToken, isPoll=isPoll)
		except OneDriveError as error:
			if error.statusCode == 401:
				self._setAccountToken(None)
			raise

	def _synchronize(self, token: AccountToken, *, isPoll: bool = False) -> _SyncResult:
		"""Merge, transfer, commit, and apply one account's synchronized state."""
		self._storage.excludeIneligibleOneDriveItems()
		with requests.Session() as session:
			graph = GraphClient(token.accessToken, self._operationCancelEvent, session)
			with self._lock:
				manifestEtag = self._lastManifestEtag
				lastFullSyncAt = self._lastFullSyncAt
			if (
				isPoll
				and manifestEtag is not None
				and lastFullSyncAt is not None
				and monotonic() - lastFullSyncAt < _SYNC_FULL_SCAN_INTERVAL_SECONDS
				and not self._storage.hasPendingOneDriveChanges()
				and graph.isManifestUnchanged(manifestEtag)
			):
				return _SyncResult(0, 0, False, manifestEtag)
			self._updateProgress(_STATUS_READING_SYNC_DATA, force=True)
			graph.ensureAppFolder()
			with TemporaryDirectory(prefix="nvdaClipboard-oneDrive-") as temporaryDirectory:
				result = self._synchronizeStaged(token, graph, _ItemStage(Path(temporaryDirectory)))
		with self._lock:
			if self._accountToken is not None and self._accountToken.accountId == token.accountId:
				self._lastManifestEtag = result.manifestEtag
				self._lastFullSyncAt = monotonic()
		return result

	def _synchronizeStaged(
		self,
		token: AccountToken,
		graph: GraphClient,
		stage: _ItemStage,
	) -> _SyncResult:
		"""Synchronize while retaining downloaded payloads only on temporary storage."""
		downloadedHashes: set[bytes] = set()
		uploadedHashes: set[bytes] = set()
		changedLocally = False
		for attempt in range(_MANIFEST_RETRIES):
			self._ensureActive()
			if attempt:
				self._updateProgress(_STATUS_READING_SYNC_DATA, force=True)
			readResult = self._readAndMergeSnapshots(token, graph)
			if readResult is None:
				continue
			local, remote, merged, itemFiles, orphanFiles = readResult
			downloadResult = self._downloadMissingPayloads(graph, stage, remote.snapshot, merged)
			if downloadResult is None:
				continue
			imageBytes, attemptDownloadedHashes = downloadResult
			downloadedHashes.update(attemptDownloadedHashes)
			manifestResult = self._normalizeAndEncodeManifest(local, merged, imageBytes)
			if manifestResult is None:
				continue
			merged, manifestData = manifestResult
			attemptUploadedHashes, oversizedPayloads, shouldRetry = self._uploadOrReusePayloads(
				graph,
				stage,
				remote.snapshot,
				merged,
				itemFiles,
			)
			uploadedHashes.update(attemptUploadedHashes)
			if shouldRetry:
				continue
			if oversizedPayloads:
				for payloadHash in oversizedPayloads:
					changedLocally = self._storage.excludeOneDrivePayload(payloadHash) or changedLocally
				continue
			# Translators: OneDrive synchronization phase while the cloud index is committed.
			self._updateProgress(_("Saving the OneDrive synchronization index..."), force=True)
			manifestEtag = remote.etag
			try:
				# The conditional manifest write is the cleanup barrier: older writers conflict,
				# while newer writers claim reused payloads by changing their file ETags.
				if remote.isMissing or remote.snapshot != _cloudSnapshot(merged) or orphanFiles:
					manifestEtag = graph.putManifest(manifestData, remote)
			except ManifestConflictError:
				continue
			self._ensureActive()
			# Translators: OneDrive synchronization phase while merged data is saved on this device.
			self._updateProgress(_("Saving synchronized data on this device..."), force=True)
			try:
				changedLocally = self._storage.applyOneDriveMerge(merged, local, stage.load) or changedLocally
			except ItemNotFoundError:
				# A concurrent local deletion removed a reused payload; the next attempt downloads it.
				continue
			self._cleanupOrphanFiles(graph, orphanFiles)
			return _SyncResult(
				uploaded=len(uploadedHashes),
				downloaded=len(downloadedHashes),
				changedLocally=changedLocally,
				manifestEtag=manifestEtag,
			)
		# Translators: Error after repeated local or cloud changes prevent a stable synchronization.
		raise OneDriveError(_("Clipboard or OneDrive data kept changing; try again"))

	def _readAndMergeSnapshots(
		self,
		token: AccountToken,
		graph: GraphClient,
	) -> (
		tuple[
			OneDriveSyncSnapshot,
			RemoteManifest,
			OneDriveSyncSnapshot,
			dict[bytes, RemoteItemFile],
			tuple[tuple[bytes, str], ...],
		]
		| None
	):
		"""Read remote state, list payload files, and merge snapshots for one attempt."""
		local = self._storage.getOneDriveSnapshot()
		if local.accountId != token.accountId:
			raise OneDriveAccountMismatchError(local.accountId, token.accountId)
		try:
			remote = graph.getManifest()
		except ManifestConflictError:
			return None
		try:
			self._storage.reserveOneDriveVersions(local, _maximumClock(remote.snapshot), 0)
		except OneDriveLocalChangeError:
			return None
		possiblePayloads = _livePayloadHashes(local) | _livePayloadHashes(remote.snapshot)
		itemFiles, orphanFiles = graph.listItemFiles(possiblePayloads)
		self._ensureActive()
		if remote.snapshot.generation > local.generation:
			rebaseStates = _generationRebaseStates(local, remote.snapshot)
			rebaseCount = sum(len(states) for states in rebaseStates)
			minimumClock = _maximumClock(remote.snapshot)
			if minimumClock + rebaseCount >= _MAX_SYNC_CLOCK:
				raise OneDriveError(INVALID_SYNC_DATA_MESSAGE)
			try:
				versions = self._storage.reserveOneDriveVersions(local, minimumClock, rebaseCount)
			except OneDriveLocalChangeError:
				return None
			rebasedLocal = _rebaseLocalSnapshot(
				local,
				remote.snapshot,
				rebaseStates,
				versions,
			)
			merged = _mergeSnapshots(rebasedLocal, remote.snapshot)
		elif local.generation > remote.snapshot.generation:
			merged = _cleanSnapshot(local)
		else:
			merged = _mergeSnapshots(local, remote.snapshot)
		return local, remote, merged, itemFiles, orphanFiles

	def _downloadMissingPayloads(
		self,
		graph: GraphClient,
		stage: _ItemStage,
		remoteSnapshot: OneDriveSyncSnapshot,
		merged: OneDriveSyncSnapshot,
	) -> tuple[dict[bytes, int], set[bytes]] | None:
		"""Stage missing remote payloads and return image byte counts for one attempt."""
		remotePayloads = _livePayloadHashes(remoteSnapshot)
		imageBytes: dict[bytes, int] = {}
		pendingDownloads: list[bytes] = []
		for payloadHash in sorted(_livePayloadHashes(merged)):
			self._ensureActive()
			imageByteCount = self._storage.getOneDriveItemImageByteCount(payloadHash)
			if imageByteCount is None:
				if payloadHash not in remotePayloads:
					return None
				item = stage.load(payloadHash)
				if item is None:
					pendingDownloads.append(payloadHash)
					continue
				imageByteCount = len(item.imageData or b"")
				del item
			imageBytes[payloadHash] = imageByteCount
		downloadedHashes: set[bytes] = set()
		if pendingDownloads:
			# Translators: OneDrive download progress. {current} and {total} are item counts.
			downloadMessage = _("Downloading OneDrive items ({current}/{total})...")
			self._updateProgress(downloadMessage.format(current=0, total=len(pendingDownloads)), force=True)
			for current, payloadHash in enumerate(pendingDownloads, start=1):
				self._ensureActive()
				data = graph.getItemData(payloadHash)
				item = _decodeItem(data, payloadHash)
				stage.store(payloadHash, data)
				downloadedHashes.add(payloadHash)
				imageBytes[payloadHash] = len(item.imageData or b"")
				del data, item
				self._updateProgress(
					downloadMessage.format(current=current, total=len(pendingDownloads)),
					force=current == len(pendingDownloads),
				)
		return imageBytes, downloadedHashes

	def _normalizeAndEncodeManifest(
		self,
		local: OneDriveSyncSnapshot,
		merged: OneDriveSyncSnapshot,
		imageBytes: Mapping[bytes, int],
	) -> tuple[OneDriveSyncSnapshot, bytes] | None:
		"""Normalize, compact when necessary, and encode one manifest."""
		historyKeys, categoryItemKeys = _normalizationTargets(
			merged,
			imageBytes,
			self._storage.getOneDriveLocalOnlyCategoryImageByteCounts(),
		)
		tombstoneCount = len(historyKeys) + len(categoryItemKeys)
		minimumClock = _maximumClock(merged)
		if minimumClock + tombstoneCount >= _MAX_SYNC_CLOCK:
			raise OneDriveError(INVALID_SYNC_DATA_MESSAGE)
		try:
			versions = self._storage.reserveOneDriveVersions(local, minimumClock, tombstoneCount)
		except OneDriveLocalChangeError:
			return None
		merged = _applyNormalization(merged, historyKeys, categoryItemKeys, versions)
		if _tombstoneCount(merged) >= _MAX_TOMBSTONES_BEFORE_COMPACTION:
			try:
				merged = self._compactSnapshotForSync(local, merged)
			except OneDriveLocalChangeError:
				return None
		try:
			manifestData = _encodeManifest(merged)
		except _ManifestSizeLimitError:
			if not _tombstoneCount(merged):
				raise
			try:
				merged = self._compactSnapshotForSync(local, merged)
			except OneDriveLocalChangeError:
				return None
			manifestData = _encodeManifest(merged)
		return merged, manifestData

	def _compactSnapshotForSync(
		self,
		local: OneDriveSyncSnapshot,
		merged: OneDriveSyncSnapshot,
	) -> OneDriveSyncSnapshot:
		"""Compact a manifest after reserving one successor generation."""
		if _maximumClock(merged) >= _MAX_SYNC_CLOCK:
			raise OneDriveError(INVALID_SYNC_DATA_MESSAGE)
		generation = self._storage.reserveOneDriveVersions(
			local,
			_maximumClock(merged),
			1,
		)[0]
		return _compactSnapshot(merged, generation)

	def _uploadOrReusePayloads(
		self,
		graph: GraphClient,
		stage: _ItemStage,
		remoteSnapshot: OneDriveSyncSnapshot,
		merged: OneDriveSyncSnapshot,
		itemFiles: dict[bytes, RemoteItemFile],
	) -> tuple[set[bytes], tuple[bytes, ...], bool]:
		"""Upload live payloads or reuse matching remote files for one attempt."""
		uploadedHashes: set[bytes] = set()
		oversizedPayloads: list[bytes] = []
		livePayloads = _livePayloadHashes(merged)
		remotePayloads = _livePayloadHashes(remoteSnapshot)
		uploadTotal = len(livePayloads)
		if uploadTotal:
			# Translators: Progress while existing files are verified and missing files are uploaded.
			uploadMessage = _("Ensuring OneDrive items are uploaded ({current}/{total})...")
			self._updateProgress(uploadMessage.format(current=0, total=uploadTotal), force=True)
		for current, payloadHash in enumerate(sorted(livePayloads), start=1):
			self._ensureActive()
			item = self._storage.getOneDriveItem(payloadHash)
			if item is None:
				itemData = stage.read(payloadHash)
				if itemData is None:
					return uploadedHashes, tuple(oversizedPayloads), True
			else:
				try:
					itemData = _encodeItem(item)
				except _ItemSizeLimitError:
					oversizedPayloads.append(payloadHash)
					item = None
					continue
			itemFile = itemFiles.get(payloadHash)
			if (
				itemFile is None
				or payloadHash not in remotePayloads
				or not remoteItemMatches(itemFile, itemData)
			):
				graph.putItemData(payloadHash, itemData)
				uploadedHashes.add(payloadHash)
				itemFiles[payloadHash] = RemoteItemFile(
					size=len(itemData),
					sha1Hash=sha1(itemData).hexdigest(),
				)
			self._updateProgress(
				uploadMessage.format(current=current, total=uploadTotal),
				force=current == uploadTotal,
			)
			del itemData
			item = None
		return uploadedHashes, tuple(oversizedPayloads), False

	def _cleanupOrphanFiles(
		self,
		graph: GraphClient,
		orphanFiles: tuple[tuple[bytes, str], ...],
	) -> None:
		"""Best-effort delete unreferenced OneDrive payload files."""
		if not orphanFiles:
			return
		# Translators: Progress while old unreferenced OneDrive payload files are removed.
		cleanupMessage = _("Cleaning up old OneDrive items ({current}/{total})...")
		cleanupTotal = len(orphanFiles)
		self._updateProgress(cleanupMessage.format(current=0, total=cleanupTotal), force=True)
		try:
			for current, (payloadHash, etag) in enumerate(orphanFiles, start=1):
				graph.deleteItemData(payloadHash, etag)
				self._updateProgress(
					cleanupMessage.format(current=current, total=cleanupTotal),
					force=current == cleanupTotal,
				)
		except OperationCancelledError:
			pass
		except OneDriveError as error:
			log.debugWarning(
				f"Could not clean up old OneDrive item files; HTTP status={error.statusCode}.",
			)

	def _startOperation(
		self,
		work: Callable[[], tuple[str, bool]],
		*,
		name: str,
		busyMessage: str,
		failureMessage: str,
		onDone: _CompletionCallback | None,
		isSigningOut: bool = False,
		requiresLogin: bool = False,
	) -> bool:
		"""Start one serialized daemon operation and update busy state."""
		with self._lock:
			if self._isTerminated:
				return False
			if not self._isInitialized:
				# Translators: Error when OneDrive is used before its service starts.
				rejectionMessage = _("OneDrive synchronization is not initialized")
			elif not self._state.isAvailable or self._state.isSigningOut and not isSigningOut:
				rejectionMessage = self._state.statusMessage
			elif requiresLogin and not self._state.isLoggedIn:
				rejectionMessage = _ERROR_SIGN_IN_REQUIRED
			elif self._state.isBusy:
				# Translators: Error when another OneDrive action is already running.
				rejectionMessage = _("Another OneDrive operation is already in progress")
			else:
				rejectionMessage = ""
				self._operationCancelEvent.clear()
				self._lastProgressUpdateAt = 0.0
				self._state = replace(
					self._state,
					isBusy=True,
					isSigningOut=isSigningOut,
					statusMessage=busyMessage,
				)
				worker = Thread(
					target=self._operationWorker,
					args=(work, failureMessage, onDone),
					name=name,
					daemon=True,
				)
		if rejectionMessage:
			self._postCompletion(onDone, False, rejectionMessage)
			return False
		self._postStateChanged()
		try:
			worker.start()
		except RuntimeError:
			log.exception("Could not start a OneDrive worker thread.")
			self._finishOperation(False, failureMessage, False, onDone)
			return False
		return True

	def _operationWorker(
		self,
		work: Callable[[], tuple[str, bool]],
		failureMessage: str,
		onDone: _CompletionCallback | None,
	) -> None:
		"""Run one operation and translate all failures before returning to wx."""
		try:
			message, dataChanged = work()
		except OperationCancelledError as error:
			self._finishOperation(False, str(error), False, onDone)
			return
		except Exception as error:
			message = self._formatFailure(error, failureMessage)
			if isinstance(error, AuthenticationUnavailableError):
				log.exception("Bundled Microsoft authentication support could not be loaded.", exc_info=error)
			elif isinstance(error, OneDriveError):
				log.debugWarning(
					f"OneDrive operation failed: type={type(error).__name__}, HTTP status={error.statusCode}.",
				)
			elif isinstance(error, StorageError):
				log.debugWarning(f"OneDrive operation failed: type={type(error).__name__}.")
			else:
				log.exception(
					"Unexpected OneDrive operation failure.",
					exc_info=error,
				)
			self._finishOperation(False, message, False, onDone)
			return
		self._finishOperation(True, message, dataChanged, onDone)

	def _finishOperation(
		self,
		success: bool,
		message: str,
		dataChanged: bool,
		onDone: _CompletionCallback | None,
	) -> None:
		"""Clear busy state, schedule follow-up work, and deliver GUI callbacks."""
		with self._lock:
			if self._isTerminated:
				return
			shouldSignOut = self._signOutPending
			if shouldSignOut:
				signOutDone = self._pendingSignOutDone
				self._signOutPending = False
				self._pendingSignOutDone = None
				self._hasPendingSync = False
				self._state = replace(self._state, isBusy=False)
				shouldRunPending = False
			else:
				signOutDone = None
				self._state = replace(
					self._state,
					isBusy=False,
					isSigningOut=False,
					statusMessage=message,
				)
				isLoggedIn = self._state.isLoggedIn
				shouldRunPending = self._hasPendingSync and isLoggedIn
				self._hasPendingSync = False
				if isLoggedIn and self._pollTimer is None:
					self._startPollTimerLocked()
		if shouldSignOut:
			if dataChanged:
				self._postOperationResult(None, success, message, True, False)
			self._startSignOut(signOutDone)
			return
		self._postOperationResult(onDone, success, message, dataChanged, shouldRunPending)

	def _formatFailure(self, error: Exception, fallback: str) -> str:
		"""Return a localized, token-free failure description."""
		if isinstance(error, OneDriveError):
			return str(error)
		if isinstance(error, OneDriveAccountMismatchError):
			# Translators: Error shown when this clipboard database is linked to another account.
			return _("This clipboard database is linked to another Microsoft account")
		if isinstance(error, ImageStorageLimitError):
			# Translators: Error when downloaded OneDrive images cannot fit in local storage.
			return _("OneDrive image data cannot fit in the 256 MB local storage limit")
		if isinstance(error, StorageFormatError):
			return INVALID_SYNC_DATA_MESSAGE
		if isinstance(error, StorageError):
			# Translators: Error when synchronized data cannot be saved locally.
			return _("Could not update local clipboard storage")
		return fallback

	def _getAuth(self) -> AuthManager:
		"""Create the single MSAL manager lazily on a worker thread."""
		with self._lock:
			auth = self._auth
		if auth is not None:
			return auth
		try:
			newAuth = AuthManager(self._cachePath)
		except AuthenticationUnavailableError:
			with self._lock:
				self._state = replace(
					self._state,
					isAvailable=False,
					statusMessage=ERROR_AUTH_UNAVAILABLE,
				)
			raise
		with self._lock:
			if self._isTerminated:
				raise OperationCancelledError("")
			if self._auth is None:
				self._auth = newAuth
			return self._auth

	def _getAccountToken(self) -> AccountToken | None:
		"""Return the current runtime account token under the state lock."""
		with self._lock:
			return self._accountToken

	def _setAccountToken(self, token: AccountToken | None) -> None:
		"""Atomically update runtime account identity and public login state."""
		with self._lock:
			if token is None or self._accountToken is None or self._accountToken.accountId != token.accountId:
				self._lastManifestEtag = None
				self._lastFullSyncAt = None
			self._accountToken = token
			self._state = replace(
				self._state,
				isLoggedIn=token is not None,
				accountName="" if token is None else token.accountName,
			)

	def _ensureActive(self) -> None:
		"""Abort work before it touches local state after manager termination."""
		with self._lock:
			if self._isTerminated or self._operationCancelEvent.is_set():
				raise OperationCancelledError("")

	def _updateProgress(self, message: str, *, force: bool = False) -> None:
		"""Publish one throttled synchronization phase or item count."""
		now = monotonic()
		with self._lock:
			if (
				self._isTerminated
				or not self._state.isBusy
				or self._state.isSigningOut
				or self._operationCancelEvent.is_set()
				or message == self._state.statusMessage
			):
				return
			if not force and now - self._lastProgressUpdateAt < _PROGRESS_UPDATE_SECONDS:
				return
			self._lastProgressUpdateAt = now
			self._state = replace(self._state, statusMessage=message)
		self._postStateChanged()

	def _debounceTimerFired(self, generation: int) -> None:
		"""Consume the local-change timer and request automatic synchronization."""
		with self._lock:
			if generation != self._debounceGeneration:
				return
			self._debounceTimer = None
		self._requestSync(onDone=None, isAutomatic=True)

	def _pollTimerFired(self) -> None:
		"""Schedule the next poll and request one automatic synchronization."""
		with self._lock:
			self._pollTimer = None
			if self._isTerminated or not self._state.isLoggedIn:
				return
			self._startPollTimerLocked()
		self._requestSync(onDone=None, isAutomatic=True, isPoll=True)

	def _startPollTimerLocked(self) -> None:
		"""Start the five-minute polling timer while the state lock is held."""
		timer = Timer(_SYNC_POLL_SECONDS, self._pollTimerFired)
		timer.daemon = True
		self._pollTimer = timer
		try:
			timer.start()
		except RuntimeError:
			self._pollTimer = None
			log.exception("Could not start the OneDrive polling timer.")

	def _cancelTimersLocked(self) -> None:
		"""Cancel synchronization timers while the state lock is held."""
		if self._debounceTimer is not None:
			self._debounceTimer.cancel()
			self._debounceTimer = None
		self._debounceGeneration += 1
		if self._pollTimer is not None:
			self._pollTimer.cancel()
			self._pollTimer = None

	def _isActive(self) -> bool:
		"""Return whether the manager has not been terminated."""
		with self._lock:
			return not self._isTerminated

	def _callOnGui(
		self,
		callback: Callable[..., None],
		*args: object,
		failureMessage: str,
		onFailure: Callable[[], bool] | None = None,
	) -> None:
		"""Schedule a callback on wx's GUI thread, logging failures only while the manager is active.

		``onFailure`` replaces the default active-manager check when wx rejects the callback.
		"""
		# Rejection is fully handled by onFailure, so whether wx accepted the callback is not needed.
		_ = queueMainThread(
			callback,
			*args,
			onFailure=self._isActive if onFailure is None else onFailure,
			failureMessage=failureMessage,
		)

	def _invokeCallback(self, callback: Callable[..., None], *args: object, failureMessage: str) -> None:
		"""Invoke one GUI callback and log instead of propagating its failure."""
		try:
			callback(*args)
		except Exception:
			log.exception(failureMessage)

	def _postStateChanged(self) -> None:
		"""Schedule a state callback on wx's GUI thread."""
		self._callOnGui(
			self._deliverStateChanged,
			failureMessage="Could not schedule a OneDrive state callback.",
		)

	def _postDeviceCode(
		self,
		callback: _DeviceCodeCallback,
		userCode: str,
		verificationUrl: str,
	) -> None:
		"""Schedule device-code presentation on wx's GUI thread."""
		self._callOnGui(
			self._deliverDeviceCode,
			callback,
			userCode,
			verificationUrl,
			failureMessage="Could not schedule the Microsoft device code.",
		)

	def _postCompletion(
		self,
		onDone: _CompletionCallback | None,
		success: bool,
		message: str,
	) -> None:
		"""Schedule a rejected operation callback on wx's GUI thread."""
		if onDone is None:
			return
		self._callOnGui(
			self._deliverCompletion,
			onDone,
			success,
			message,
			failureMessage="Could not schedule a OneDrive completion callback.",
		)

	def _postOperationResult(
		self,
		onDone: _CompletionCallback | None,
		success: bool,
		message: str,
		dataChanged: bool,
		shouldRunPending: bool,
	) -> None:
		"""Schedule ordered state, data, and completion callbacks on wx."""

		def retainPendingSync() -> bool:
			"""Keep queued synchronization when wx rejects delivery and report whether to log it."""
			with self._lock:
				isTerminated = self._isTerminated
				if shouldRunPending and not isTerminated and self._state.isLoggedIn:
					self._hasPendingSync = True
			return not isTerminated

		self._callOnGui(
			self._deliverOperationResult,
			onDone,
			success,
			message,
			dataChanged,
			shouldRunPending,
			failureMessage="Could not schedule OneDrive result callbacks.",
			onFailure=retainPendingSync,
		)

	def _deliverStateChanged(self) -> None:
		"""Invoke the current state callback on the GUI thread."""
		with self._lock:
			if self._isTerminated:
				return
			callback = self._onStateChanged
		if callback is not None:
			self._invokeCallback(callback, failureMessage="OneDrive state callback failed.")

	def _deliverDeviceCode(
		self,
		callback: _DeviceCodeCallback,
		userCode: str,
		verificationUrl: str,
	) -> None:
		"""Invoke a device-code callback only while the manager is active."""
		with self._lock:
			if self._isTerminated or self._deviceCodeCancellationRequested:
				return
		self._invokeCallback(
			callback,
			userCode,
			verificationUrl,
			failureMessage="OneDrive device-code callback failed.",
		)

	def _deliverCompletion(
		self,
		callback: _CompletionCallback,
		success: bool,
		message: str,
	) -> None:
		"""Invoke a completion callback only while the manager is active."""
		with self._lock:
			if self._isTerminated:
				return
		self._invokeCallback(
			callback, success, message, failureMessage="OneDrive completion callback failed."
		)

	def _deliverOperationResult(
		self,
		onDone: _CompletionCallback | None,
		success: bool,
		message: str,
		dataChanged: bool,
		shouldRunPending: bool,
	) -> None:
		"""Deliver final callbacks in a stable order on the GUI thread."""
		with self._lock:
			if self._isTerminated:
				return
			onStateChanged = self._onStateChanged
			onDataChanged = self._onDataChanged if dataChanged else None
		try:
			if onStateChanged is not None:
				self._invokeCallback(onStateChanged, failureMessage="OneDrive state callback failed.")
			if onDataChanged is not None:
				self._invokeCallback(onDataChanged, failureMessage="OneDrive data callback failed.")
			if onDone is not None:
				self._invokeCallback(
					onDone,
					success,
					message,
					failureMessage="OneDrive completion callback failed.",
				)
		finally:
			if shouldRunPending:
				self._requestSync(onDone=None, isAutomatic=True)
