# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for standalone clipboard storage."""

# ruff: noqa: F821 -- The copied self-check resolves names from the dynamically loaded module.

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from tests._module_loader import createAddonPackage, loadAddonModule


_PACKAGE_NAME = "nvdaClipboardStorageTests"
createAddonPackage(_PACKAGE_NAME)
_LOG_HANDLER = ModuleType("logHandler")
_LOG_HANDLER.log = Mock()
storageModule = loadAddonModule(
	f"{_PACKAGE_NAME}.storage",
	injectedModules={"logHandler": _LOG_HANDLER},
)
syncStateModule = sys.modules[f"{_PACKAGE_NAME}.storageSyncState"]
for _module in (syncStateModule, storageModule):
	for _name, _value in vars(_module).items():
		if not _name.startswith("__"):
			globals()[_name] = _value


def _expectRaises(
	exceptionType: type[BaseException],
	action: Callable[[], None],
	message: str,
) -> None:
	"""Assert that one callable raises one expected exception."""
	try:
		action()
	except exceptionType:
		return
	raise AssertionError(message)


# Keep this linear cross-feature scenario intact so storage interactions are exercised in one database lifecycle.
def _runSelfCheck() -> None:  # noqa: C901
	"""Exercise migration, synchronization state, batch references, and reopening."""
	existingOrder = ((1, True), (2, False), (3, True))
	assert _mergeSynchronizedOrder(existingOrder, (1, 3)) == (1, 2, 3)
	assert _mergeSynchronizedOrder(existingOrder, (3, 1)) == (3, 2, 1)
	assert _mergeSynchronizedOrder(existingOrder, (4, 3, 1)) == (4, 3, 2, 1)
	assert _mergeSynchronizedOrder(existingOrder, (3, 4, 1)) == (3, 2, 4, 1)
	validationVersion = SyncVersion(1, "00000000-0000-0000-0000-000000000001")
	validateOneDriveSnapshot(
		OneDriveSyncSnapshot(
			accountId=None,
			history=(),
			categories=(
				OneDriveCategoryState(
					"clipboard history",
					"Clipboard history",
					validationVersion,
					validationVersion,
					False,
				),
			),
			categoryItems=(),
		),
	)
	oneDriveItems = [(index, False, True) for index in range(MAX_HISTORY_ITEMS + 1)]
	localOnlyItems = [
		(MAX_HISTORY_ITEMS + 1 + index, False, False) for index in range(MAX_LOCAL_ONLY_HISTORY_ITEMS + 1)
	]
	assert _historyOverflowIds((*oneDriveItems, *localOnlyItems)) == {
		MAX_HISTORY_ITEMS,
		MAX_HISTORY_ITEMS + MAX_LOCAL_ONLY_HISTORY_ITEMS + 1,
	}
	oneDriveImages = [(index, True, True) for index in range(MAX_IMAGE_HISTORY_ITEMS + 1)]
	localOnlyImages = [
		(MAX_IMAGE_HISTORY_ITEMS + 1 + index, True, False)
		for index in range(MAX_LOCAL_ONLY_IMAGE_HISTORY_ITEMS + 1)
	]
	assert _historyOverflowIds((*oneDriveImages, *localOnlyImages)) == {
		MAX_IMAGE_HISTORY_ITEMS,
		MAX_IMAGE_HISTORY_ITEMS + MAX_LOCAL_ONLY_IMAGE_HISTORY_ITEMS + 1,
	}
	transientError = sqlite3.DatabaseError("database is locked")
	assert not _isCorruptDatabaseError(transientError)
	corruptError = sqlite3.DatabaseError("database disk image is malformed")
	setattr(corruptError, "sqlite_errorcode", sqlite3.SQLITE_CORRUPT)
	assert _isCorruptDatabaseError(corruptError)
	with TemporaryDirectory() as temporaryDirectory:
		root = Path(temporaryDirectory)
		dataPath = root / DATA_FILENAME
		version2HistoryPath = root / VERSION_2_HISTORY_FILENAME
		futureDataPath = root / "future.db"
		futureConnection = sqlite3.connect(futureDataPath)
		futureConnection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
		futureConnection.close()
		_expectRaises(
			UnsupportedSchemaVersionError,
			lambda: _openDatabase(futureDataPath),
			"A future schema version was accepted",
		)
		futureConnection = sqlite3.connect(futureDataPath)
		try:
			journalModeRow = futureConnection.execute("PRAGMA journal_mode").fetchone()
			assert journalModeRow is not None and journalModeRow[0] == "delete"
		finally:
			futureConnection.close()
		malformedVersion1Path = root / "malformed-schema1.db"
		malformedVersion1Connection = sqlite3.connect(malformedVersion1Path)
		malformedVersion1Connection.execute("PRAGMA user_version = 1")
		malformedVersion1Connection.close()
		recoveredVersion1Storage = ClipboardStorage(
			malformedVersion1Path,
			root / "missing-malformed-v1.json",
		)
		assert not recoveredVersion1Storage.history
		recoveredVersion1Storage.close()
		assert Path(f"{malformedVersion1Path}.corrupt").exists()
		version1DataPath = root / "schema1.db"
		version1Storage = ClipboardStorage(version1DataPath, root / "missing-v2.json")
		version1Item = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="version 1 item")
		version1ItemId = version1Storage.addHistory(version1Item)
		assert version1ItemId is not None
		version1Storage.createCategory("Version 1 category")
		version1Storage.copyHistoryItemsToCategoryById(
			(version1ItemId,),
			"Version 1 category",
		)
		version1Storage.close()
		version1Connection = sqlite3.connect(version1DataPath)
		version1Connection.execute("DROP TABLE oneDriveHistory")
		version1Connection.execute("DROP TABLE oneDriveCategories")
		version1Connection.execute("DROP TABLE oneDriveCategoryItems")
		version1Connection.execute("DROP TABLE oneDriveExcludedPayloads")
		version1Connection.execute("DROP TABLE oneDriveSync")
		version1Connection.execute("PRAGMA user_version = 1")
		version1Connection.close()
		version1Storage = ClipboardStorage(version1DataPath, root / "missing-v2.json")
		version1Snapshot = version1Storage.getOneDriveSnapshot()
		assert not version1Storage.hasPendingOneDriveChanges()
		version1DedupKey = _dedupKey(version1Item)
		assert not version1Snapshot.history
		assert not version1Snapshot.categories
		assert not version1Snapshot.categoryItems
		preSignInDeletedItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="deleted before sign-in")
		preSignInDeletedId = version1Storage.addHistory(preSignInDeletedItem)
		assert preSignInDeletedId is not None
		version1Storage.deleteItemsById(None, (preSignInDeletedId,))
		version1Storage.renameCategory("Version 1 category", "Renamed before sign-in")
		preSignInDeletedKey = _dedupKey(preSignInDeletedItem)
		assert version1Storage.getOneDriveSnapshot() == version1Snapshot
		with version1Storage._readConnection() as connection:
			assert connection.execute("SELECT logicalClock FROM oneDriveSync").fetchone()[0] == 0
		version1Storage.bindOneDriveAccount("account.tenant")
		boundSnapshot = version1Storage.getOneDriveSnapshot()
		assert version1Storage.hasPendingOneDriveChanges()
		assert boundSnapshot.accountId == "account.tenant"
		assert all(state.dedupKey != preSignInDeletedKey for state in boundSnapshot.history)
		assert any(
			state.dedupKey == version1DedupKey and not state.deleted for state in boundSnapshot.history
		)
		assert any(
			state.nameFolded == "renamed before sign-in" and not state.deleted
			for state in boundSnapshot.categories
		)
		assert any(
			state.nameFolded == "renamed before sign-in" and not state.deleted
			for state in boundSnapshot.categoryItems
		)
		version1Storage.deleteItemsById(None, (version1ItemId,))
		sameAccountSnapshot = version1Storage.getOneDriveSnapshot()
		assert next(
			state for state in sameAccountSnapshot.history if state.dedupKey == version1DedupKey
		).deleted
		version1Storage.bindOneDriveAccount("account.tenant")
		assert version1Storage.getOneDriveSnapshot() == sameAccountSnapshot
		evictedItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="internally evicted")
		evictedItemId = version1Storage.addHistory(evictedItem)
		assert evictedItemId is not None
		with version1Storage._transaction() as connection:
			_deleteHistoryReferences(connection, (evictedItemId,), explicit=False)
			_garbageCollect(connection)
		assert all(
			state.dedupKey != _dedupKey(evictedItem)
			for state in version1Storage.getOneDriveSnapshot().history
		)
		privateItem = ClipboardItem(
			ClipboardItemType.PLAIN_TEXT,
			text="private",
			canUpload=False,
		)
		assert version1Storage.addHistory(privateItem) is not None
		assert all(
			state.dedupKey != _dedupKey(privateItem)
			for state in version1Storage.getOneDriveSnapshot().history
		)
		oversizedItem = ClipboardItem(
			ClipboardItemType.PLAIN_TEXT,
			text="x" * (MAX_ONE_DRIVE_TEXT_BYTES + 1),
		)
		oversizedItemId = version1Storage.addHistory(oversizedItem)
		assert oversizedItemId is not None
		version1Storage.createCategory("Local only")
		version1Storage.copyHistoryItemsToCategoryById((oversizedItemId,), "Local only")
		oversizedDedupKey = _dedupKey(oversizedItem)
		oversizedPayloadHash = getItemPayloadHash(oversizedItem)
		with version1Storage._transaction() as connection:
			recordHistoryState(connection, oversizedDedupKey, oversizedPayloadHash)
			recordCategoryItemState(
				connection,
				"local only",
				oversizedPayloadHash,
				deleted=False,
			)
		assert version1Storage.excludeOneDrivePayload(oversizedPayloadHash)
		oversizedSnapshot = version1Storage.getOneDriveSnapshot()
		assert next(
			state for state in oversizedSnapshot.history if state.dedupKey == oversizedDedupKey
		).deleted
		assert next(
			state for state in oversizedSnapshot.categoryItems if state.payloadHash == oversizedPayloadHash
		).deleted
		assert oversizedItemId in {item.itemId for item in version1Storage.history}
		assert oversizedItemId in {item.itemId for item in version1Storage.getItems("Local only")}
		remoteItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="remote change")
		remotePayloadHash = getItemPayloadHash(remoteItem)
		remoteState = OneDriveHistoryState(
			_dedupKey(remoteItem),
			remotePayloadHash,
			SyncVersion(10_000, "00000000-0000-0000-0000-000000010000"),
			False,
		)
		assert version1Storage.applyOneDriveMerge(
			replace(oversizedSnapshot, history=(*oversizedSnapshot.history, remoteState)),
			oversizedSnapshot,
			lambda payloadHash: remoteItem if payloadHash == remotePayloadHash else None,
		)
		assert oversizedItemId in {item.itemId for item in version1Storage.history}
		assert oversizedItemId in {item.itemId for item in version1Storage.getItems("Local only")}
		assert version1Storage.getOneDriveItem(oversizedPayloadHash) is None
		with version1Storage._transaction() as connection:
			recordHistoryState(connection, oversizedDedupKey, oversizedPayloadHash)
			recordCategoryItemState(
				connection,
				"local only",
				oversizedPayloadHash,
				deleted=False,
			)
		assert version1Storage.excludeIneligibleOneDriveItems()
		assert not version1Storage.excludeIneligibleOneDriveItems()
		version1Storage.close()
		exclusionLimitStorage = ClipboardStorage(
			root / "exclusion-limit.db",
			root / "missing-exclusion-limit.json",
		)
		with patch.object(storageModule, "MAX_LOCAL_ONLY_HISTORY_ITEMS", 2):
			oldestLocalId = exclusionLimitStorage.addHistory(
				ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="oldest local", canUpload=False),
			)
			newerLocalId = exclusionLimitStorage.addHistory(
				ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="newer local", canUpload=False),
			)
			excludedLimitItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="excluded local")
			excludedLimitId = exclusionLimitStorage.addHistory(excludedLimitItem)
			assert None not in (oldestLocalId, newerLocalId, excludedLimitId)
			assert exclusionLimitStorage.excludeOneDrivePayload(getItemPayloadHash(excludedLimitItem))
			assert {item.itemId for item in exclusionLimitStorage.history} == {
				newerLocalId,
				excludedLimitId,
			}
		exclusionLimitStorage.close()
		reservationStorage = ClipboardStorage(root / "reservation.db", root / "missing-reservation.json")
		reservationStorage.bindOneDriveAccount("account.tenant")
		expectedSnapshot = reservationStorage.getOneDriveSnapshot()
		reservedVersions = reservationStorage.reserveOneDriveVersions(expectedSnapshot, 100, 2)
		assert tuple(version.clock for version in reservedVersions) == (101, 102)
		reservationStorage.reserveOneDriveVersions(expectedSnapshot, 200, 0)
		concurrentItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="concurrent local change")
		assert reservationStorage.addHistory(concurrentItem) is not None
		_expectRaises(
			OneDriveLocalChangeError,
			lambda: reservationStorage.reserveOneDriveVersions(expectedSnapshot, 300, 1),
			"A stale local synchronization snapshot was accepted",
		)
		currentSnapshot = reservationStorage.getOneDriveSnapshot()
		futureVersions = reservationStorage.reserveOneDriveVersions(currentSnapshot, 1_000, 2)
		futureItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="future local change")
		assert reservationStorage.addHistory(futureItem) is not None
		futureState = next(
			state
			for state in reservationStorage.getOneDriveSnapshot().history
			if state.dedupKey == _dedupKey(futureItem)
		)
		assert futureState.version.clock > futureVersions[-1].clock
		_expectRaises(
			ValueError,
			lambda: reservationStorage.reserveOneDriveVersions(
				reservationStorage.getOneDriveSnapshot(),
				_MAX_SYNC_CLOCK - 1,
				0,
			),
			"A clock without a local successor was observed",
		)
		_expectRaises(
			StorageFormatError,
			lambda: _validateSyncVersion(SyncVersion(_MAX_SYNC_CLOCK, ZERO_OPERATION_ID)),
			"A terminal synchronization clock was accepted",
		)
		generationStorage = ClipboardStorage(root / "generation.db", root / "missing-generation.json")
		generationStorage.bindOneDriveAccount("account.tenant")
		baselineItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="generation baseline")
		assert generationStorage.addHistory(baselineItem) is not None
		generationStorage.createCategory("First")
		generationStorage.createCategory("Zed")
		generationStorage.createCategory("Last")
		generationStorage.renameCategory("Zed", "Middle")
		expectedGenerationSnapshot = generationStorage.getOneDriveSnapshot()
		middleOrderVersion = next(
			state.orderVersion
			for state in expectedGenerationSnapshot.categories
			if state.nameFolded == "middle"
		)
		committedMiddleOrderVersion = SyncVersion(middleOrderVersion.clock, ZERO_OPERATION_ID)
		concurrentGenerationItem = ClipboardItem(
			ClipboardItemType.PLAIN_TEXT,
			text="generation concurrent",
		)
		assert generationStorage.addHistory(concurrentGenerationItem) is not None
		generationStorage.renameCategory("Middle", "Renamed")
		committedGeneration = SyncVersion(
			100,
			"00000000-0000-0000-0000-000000000100",
		)
		committedGenerationSnapshot = replace(
			expectedGenerationSnapshot,
			history=tuple(replace(state, dirty=False) for state in expectedGenerationSnapshot.history),
			categories=tuple(
				replace(
					state,
					orderVersion=(
						committedMiddleOrderVersion if state.nameFolded == "middle" else state.orderVersion
					),
					dirty=False,
				)
				for state in expectedGenerationSnapshot.categories
				if not state.deleted
			),
			generation=committedGeneration,
		)
		assert generationStorage.applyOneDriveMerge(
			committedGenerationSnapshot,
			expectedGenerationSnapshot,
			lambda _payloadHash: None,
		)
		afterGenerationSnapshot = generationStorage.getOneDriveSnapshot()
		assert afterGenerationSnapshot.generation == committedGeneration
		baselineState = next(
			state for state in afterGenerationSnapshot.history if state.dedupKey == _dedupKey(baselineItem)
		)
		concurrentGenerationState = next(
			state
			for state in afterGenerationSnapshot.history
			if state.dedupKey == _dedupKey(concurrentGenerationItem)
		)
		assert not baselineState.dirty
		assert concurrentGenerationState.dirty
		assert concurrentGenerationState.version.clock > committedGeneration.clock
		assert generationStorage.categoryNames == ("First", "Renamed", "Last")
		renamedState = next(
			state for state in afterGenerationSnapshot.categories if state.nameFolded == "renamed"
		)
		assert renamedState.orderVersion == committedMiddleOrderVersion
		generationStorage.close()
		reservationStorage.close()
		categoryRaceStorage = ClipboardStorage(
			root / "category-race.db",
			root / "missing-category-race.json",
		)
		categoryRaceStorage.bindOneDriveAccount("account.tenant")
		categoryRaceStorage.createCategory("Target")
		expectedCategoryRace = categoryRaceStorage.getOneDriveSnapshot()
		remoteCategoryDeletion = SyncVersion(
			100,
			"00000000-0000-0000-0000-000000000100",
		)
		deletedCategoryRace = replace(
			expectedCategoryRace,
			categories=tuple(
				replace(state, version=remoteCategoryDeletion, deleted=True, dirty=False)
				for state in expectedCategoryRace.categories
			),
		)
		categoryRaceStorage.reserveOneDriveVersions(expectedCategoryRace, remoteCategoryDeletion.clock, 0)
		categoryRaceItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="category race")
		categoryRaceStorage.addCategoryItem("Target", categoryRaceItem)
		categoryRaceStorage.applyOneDriveMerge(
			deletedCategoryRace,
			expectedCategoryRace,
			lambda _payloadHash: None,
		)
		categoryRaceSnapshot = categoryRaceStorage.getOneDriveSnapshot()
		categoryRaceState = next(
			state for state in categoryRaceSnapshot.categories if state.nameFolded == "target"
		)
		assert not categoryRaceState.deleted and categoryRaceState.version > remoteCategoryDeletion
		categoryRaceItems = categoryRaceStorage.getItems("Target")
		assert len(categoryRaceItems) == 1
		assert categoryRaceStorage.getItemById("Target", categoryRaceItems[0].itemId) == categoryRaceItem
		categoryRaceStorage.close()
		categoryOrderStorage = ClipboardStorage(
			root / "category-order.db",
			root / "missing-category-order.json",
		)
		categoryOrderStorage.bindOneDriveAccount("account.tenant")
		expectedCategoryOrder = categoryOrderStorage.getOneDriveSnapshot()
		tiedOrderVersion = SyncVersion(1, "00000000-0000-0000-0000-000000000001")
		assert categoryOrderStorage.applyOneDriveMerge(
			replace(
				expectedCategoryOrder,
				categories=(
					OneDriveCategoryState(
						"zulu",
						"Zulu",
						tiedOrderVersion,
						SyncVersion(3, str(UUID(int=3))),
						False,
					),
					OneDriveCategoryState(
						"alpha",
						"Alpha",
						tiedOrderVersion,
						SyncVersion(2, str(UUID(int=2))),
						False,
					),
				),
			),
			expectedCategoryOrder,
			lambda _payloadHash: None,
		)
		assert categoryOrderStorage.categoryNames == ("Alpha", "Zulu")
		categoryOrderStorage.close()
		categoryStorage = ClipboardStorage(root / "categories.db", root / "missing-categories.json")
		categoryStorage.bindOneDriveAccount("account.tenant")
		categoryStorage.createCategory("Cloud only")
		categoryStorage.createCategory("Mixed")
		cloudCategoryItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="cloud category item")
		privateCategoryItem = ClipboardItem(
			ClipboardItemType.PLAIN_TEXT,
			text="private category item",
			canUpload=False,
		)
		categoryStorage.addCategoryItem("Cloud only", cloudCategoryItem)
		categoryStorage.addCategoryItem("Mixed", cloudCategoryItem)
		categoryStorage.addCategoryItem("Mixed", privateCategoryItem)
		expectedCategorySnapshot = categoryStorage.getOneDriveSnapshot()
		assert categoryStorage.applyOneDriveMerge(
			OneDriveSyncSnapshot(
				accountId="account.tenant",
				history=(),
				categories=(),
				categoryItems=(),
				generation=SyncVersion(
					20_000,
					"00000000-0000-0000-0000-000000020000",
				),
			),
			expectedCategorySnapshot,
			lambda _payloadHash: None,
		)
		assert categoryStorage.categoryNames == ("Mixed",)
		mixedItems = categoryStorage.getItems("Mixed")
		assert len(mixedItems) == 1
		assert categoryStorage.getItemById("Mixed", mixedItems[0].itemId) == privateCategoryItem
		categoryStorage.close()
		imageStorage = ClipboardStorage(root / "images.db", root / "missing-images.json")
		imageStorage.createCategory("Images")
		privateImage = ClipboardItem(
			ClipboardItemType.IMAGE,
			imageData=b"private",
			imageWidth=1,
			imageHeight=1,
			imageBitDepth=32,
			canUpload=False,
		)
		excludedImage = replace(privateImage, imageData=b"excluded", canUpload=True)
		imageStorage.addCategoryItem("Images", privateImage)
		imageStorage.addCategoryItem("Images", excludedImage)
		excludedImageHash = getItemPayloadHash(excludedImage)
		assert imageStorage.excludeOneDrivePayload(excludedImageHash)
		assert imageStorage.getOneDriveLocalOnlyCategoryImageByteCounts() == {
			getItemPayloadHash(privateImage): len(b"private"),
			excludedImageHash: len(b"excluded"),
		}
		imageStorage.close()
		pruneStorage = ClipboardStorage(root / "prune.db", root / "missing-prune.json")
		prunedImageId = pruneStorage.addHistory(privateImage)
		assert prunedImageId is not None
		with pruneStorage._transaction() as connection:
			_pruneLocalOnlyImageHistory(connection, set(), MAX_TOTAL_IMAGE_BYTES - 1)
		assert not pruneStorage.history
		pruneStorage.close()
		version2HistoryPath.write_text(json.dumps(["latest", "older"]), encoding="utf-8")
		assert migrateVersion2History(
			dataPath,
			version2HistoryPath,
		)
		version2HistoryPath.write_text(
			json.dumps(["older", "gap latest", "latest"]),
			encoding="utf-8",
		)
		assert migrateVersion2History(
			dataPath,
			version2HistoryPath,
		)
		assert not migrateVersion2History(
			dataPath,
			version2HistoryPath,
		)
		storage = ClipboardStorage(dataPath, version2HistoryPath)
		assert not version2HistoryPath.exists()
		assert Path(f"{version2HistoryPath}.bak").exists()
		assert tuple(item.textPreview for item in storage.history) == (
			"older",
			"gap latest",
			"latest",
		)
		positionedItem, positionedIndex, itemCount = storage.getSummaryAt(None, 99, -1)
		assert positionedItem is not None
		assert (positionedItem.textPreview, positionedIndex, itemCount) == ("gap latest", 1, 3)
		storage.createCategory("Saved")
		assert storage.getSummaryAt("Saved", 99, 1) == (None, 0, 0)
		plainLatestId = next(item.itemId for item in storage.history if item.textPreview == "latest")
		storage.copyHistoryItemsToCategoryById((plainLatestId,), "Saved")
		assert tuple(item.textPreview for item in storage.getItems("Saved")) == ("latest",)
		formattedId = storage.addHistory(
			ClipboardItem(
				ClipboardItemType.FORMATTED_TEXT,
				text="latest",
				html=b"<b>latest</b>",
			),
		)
		assert formattedId is not None
		assert storage.history[0].contentType == ClipboardItemType.FORMATTED_TEXT
		assert storage.getItemContentById(None, formattedId) == ClipboardItemContent(
			itemId=formattedId,
			contentType=ClipboardItemType.FORMATTED_TEXT,
			text="latest",
			imageByteCount=0,
			imageWidth=None,
			imageHeight=None,
			imageBitDepth=None,
			files=(),
			canUpload=True,
		)
		categoryItemId = storage.getItems("Saved")[0].itemId
		assert storage.getItemById("Saved", categoryItemId).contentType == ClipboardItemType.PLAIN_TEXT
		assert storage.addHistory(storage.getItemById(None, storage.history[0].itemId)) == formattedId
		storage.createCategory("Moved")
		plainHistoryId = next(item.itemId for item in storage.history if item.textPreview == "older")
		storage.copyHistoryItemsToCategoryById((formattedId, formattedId, plainHistoryId), "Saved")
		savedItemIds = tuple(item.itemId for item in storage.getItems("Saved"))
		assert savedItemIds == (formattedId, plainHistoryId, plainLatestId)
		positionedItem, positionedIndex, itemCount = storage.getSummaryAt("Saved", 99, -1)
		assert positionedItem is not None
		assert (positionedItem.itemId, positionedIndex, itemCount) == (plainHistoryId, 1, 3)
		_expectRaises(
			ItemNotFoundError,
			lambda: storage.moveCategoryItemsById("Saved", (formattedId, -1), "Moved"),
			"A partial category move was committed",
		)
		assert tuple(item.itemId for item in storage.getItems("Saved")) == savedItemIds
		assert not storage.getItems("Moved")
		storage.moveCategoryItemsById("Saved", savedItemIds, "Moved")
		assert not storage.getItems("Saved")
		assert tuple(item.itemId for item in storage.getItems("Moved")) == savedItemIds
		storage.deleteItemsById("Moved", savedItemIds)
		assert not storage.getItems("Moved")
		historyItemIds = tuple(item.itemId for item in storage.history)
		_expectRaises(
			ItemNotFoundError,
			lambda: storage.deleteItemsById(None, (formattedId, -1)),
			"A partial history deletion was committed",
		)
		assert tuple(item.itemId for item in storage.history) == historyItemIds
		olderItemIds = tuple(
			item.itemId for item in storage.history if item.textPreview in ("older", "gap latest")
		)
		storage.deleteItemsById(None, olderItemIds)
		assert all(item.itemId not in olderItemIds for item in storage.history)
		longText = "x" * (SUMMARY_TEXT_LIMIT + 1) + "search tail"
		longTextId = storage.addHistory(ClipboardItem(ClipboardItemType.PLAIN_TEXT, text=longText))
		assert longTextId is not None and "search tail" not in storage.history[0].textPreview
		assert storage.getItemContentById(None, longTextId).text == longText
		files = (r"C:\one\first.txt", r"C:\two\second.txt", r"C:\three\target.py")
		filesId = storage.addHistory(ClipboardItem(ClipboardItemType.FILES, files=files))
		assert filesId is not None and storage.history[0].filesPreview == ("first.txt", "second.txt")
		assert storage.getItemContentById(None, filesId).files == files
		imageItem = ClipboardItem(
			ClipboardItemType.IMAGE,
			imageData=b"abc",
			imageWidth=1,
			imageHeight=1,
			imageBitDepth=24,
		)
		imageId = storage.addHistory(imageItem)
		assert imageId is not None
		historyContents = tuple(storage.iterItemContents())
		assert tuple(item.itemId for item in historyContents) == tuple(
			item.itemId for item in storage.history
		)
		contentById = {item.itemId: item for item in historyContents}
		assert contentById[longTextId].text == longText
		assert contentById[filesId].files == files
		assert contentById[imageId].imageByteCount == len(imageItem.imageData or b"")
		storage.copyHistoryItemsToCategoryById((filesId, longTextId), "Saved")
		categoryContents = tuple(storage.iterItemContents("Saved"))
		assert tuple(item.itemId for item in categoryContents) == (filesId, longTextId)
		with storage._transaction() as connection:
			connection.execute("UPDATE items SET imageData = ? WHERE itemId = ?", (b"abd", imageId))
		_expectRaises(
			StorageFormatError,
			lambda: storage.getItemById(None, imageId),
			"A payload with a stale hash was accepted",
		)
		replacementImageId = storage.addHistory(imageItem)
		assert replacementImageId is not None and replacementImageId != imageId
		assert storage.getItemById(None, replacementImageId) == imageItem
		with storage._transaction() as connection:
			connection.execute(
				"UPDATE items SET imageByteCount = imageByteCount + 1 WHERE itemId = ?",
				(replacementImageId,),
			)
		_expectRaises(
			StorageFormatError,
			lambda: storage.getItemById(None, replacementImageId),
			"An incorrect image byte count was accepted",
		)
		with storage._transaction() as connection:
			connection.execute(
				"UPDATE items SET imageByteCount = imageByteCount - 1 WHERE itemId = ?",
				(replacementImageId,),
			)
		storage.close()
		reopened = ClipboardStorage(dataPath, version2HistoryPath)
		assert reopened.getItemById(None, formattedId).html == b"<b>latest</b>"
		reopened.close()


class StorageSelfCheckTests(unittest.TestCase):
	"""Run the storage module's isolated behavior checks."""

	def testAppendingTextReplacesTheCurrentHistoryEntry(self) -> None:
		"""Keep append operations as one history entry while preserving older entries."""
		with TemporaryDirectory() as directory:
			root = Path(directory)
			storage = ClipboardStorage(root / "history.db", root / "missing-history.json")
			oldItemId = storage.addHistory(ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="old"))
			assert oldItemId is not None
			olderItemId = storage.addHistory(ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="older"))
			assert olderItemId is not None
			newItemId = storage.addHistory(
				ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="old\nnew"),
				replaceText="older",
			)
			assert newItemId is not None
			assert tuple(item.textPreview for item in storage.history) == ("old new", "old")
			storage.addHistory(
				ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="old\nnewer"),
				replaceText="missing",
			)
			assert tuple(item.textPreview for item in storage.history) == ("old newer", "old new", "old")
			storage.close()

	def testAppendingTextAfterDeletingTheNewestHistoryEntry(self) -> None:
		for previousText in ("old", "older"):
			with self.subTest(previousText=previousText), TemporaryDirectory() as directory:
				root = Path(directory)
				storage = ClipboardStorage(root / "history.db", root / "missing-history.json")
				try:
					olderId = storage.addHistory(ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="older"))
					oldItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="old")
					oldId = storage.addHistory(oldItem)
					deletedId = storage.addHistory(
						ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="deleted"),
					)
					assert olderId is not None and oldId is not None and deletedId is not None
					storage.deleteItemsById(None, (deletedId,))
					assert storage.getLatestHistoryItem() == oldItem
					newItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text=f"{previousText}\nnew")
					newId = storage.addHistory(newItem, replaceText=previousText)
					assert newId is not None
					expectedIds = (newId, olderId) if previousText == "old" else (newId, oldId, olderId)
					assert tuple(item.itemId for item in storage.history) == expectedIds
					assert storage.getItemById(None, newId) == newItem
				finally:
					storage.close()

	def testRepeatedAppendsAfterClipboardNewlineConversion(self) -> None:
		from tests.test_clipboardMonitor import clipboardMonitor

		for newline in ("\n", "\r", "\r\n"):
			with self.subTest(newline=newline), TemporaryDirectory() as directory:
				root = Path(directory)
				storage = ClipboardStorage(root / "history.db", root / "missing-history.json")
				try:
					storage.bindOneDriveAccount("account.tenant")
					assert (
						storage.addHistory(ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="old"))
						is not None
					)
					firstItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text=f"old{newline}new")
					assert storage.addHistory(firstItem, replaceText="old") is not None
					monitor = object.__new__(clipboardMonitor.ClipboardMonitor)
					snapshot = clipboardMonitor.ClipboardSnapshot(
						clipboardMonitor.ClipboardContentType.TEXT,
						text=firstItem.text,
					)
					formats = dict(monitor._buildWritableFormats(snapshot))
					clipboardText = formats[clipboardMonitor.CF_UNICODETEXT][:-2].decode("utf-16-le")
					assert clipboardText == "old\r\nnew"
					secondItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text=f"{clipboardText}\nnewer")
					secondId = storage.addHistory(secondItem, replaceText=clipboardText)
					assert secondId is not None
					assert tuple(item.itemId for item in storage.history) == (secondId,)
					assert storage.getItemById(None, secondId) == secondItem
					states = {state.dedupKey: state for state in storage.getOneDriveSnapshot().history}
					deletedState = states[_dedupKey(firstItem)]
					assert deletedState.deleted and deletedState.dirty
					assert deletedState.payloadHash is None
					newState = states[_dedupKey(secondItem)]
					assert not newState.deleted and newState.dirty
					assert newState.payloadHash == getItemPayloadHash(secondItem)
				finally:
					storage.close()

	def testAppendReplacementPreservesWhitespaceDifferences(self) -> None:
		for previousText in ("old\r\nnew ", "old\r\n\r\nnew"):
			with self.subTest(previousText=previousText), TemporaryDirectory() as directory:
				root = Path(directory)
				storage = ClipboardStorage(root / "history.db", root / "missing-history.json")
				try:
					oldItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="old\nnew")
					oldId = storage.addHistory(oldItem)
					assert oldId is not None
					newId = storage.addHistory(
						ClipboardItem(ClipboardItemType.PLAIN_TEXT, text=f"{previousText}\nnewer"),
						replaceText=previousText,
					)
					assert newId is not None
					assert tuple(item.itemId for item in storage.history) == (newId, oldId)
					assert storage.getItemById(None, oldId) == oldItem
				finally:
					storage.close()

	def testAppendingTextRecordsOneDriveHistoryReplacement(self) -> None:
		with TemporaryDirectory() as directory:
			root = Path(directory)
			storage = ClipboardStorage(root / "history.db", root / "missing-history.json")
			try:
				storage.bindOneDriveAccount("account.tenant")
				oldItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="old")
				assert storage.addHistory(oldItem) is not None
				oldState = next(
					state
					for state in storage.getOneDriveSnapshot().history
					if state.dedupKey == _dedupKey(oldItem)
				)
				assert not oldState.deleted
				newItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="old\nnew")
				newItemId = storage.addHistory(newItem, replaceText=oldItem.text)
				assert newItemId is not None
				states = {state.dedupKey: state for state in storage.getOneDriveSnapshot().history}
				deletedState = states[_dedupKey(oldItem)]
				assert deletedState.deleted and deletedState.dirty
				assert deletedState.payloadHash is None
				assert deletedState.version > oldState.version
				newState = states[_dedupKey(newItem)]
				assert not newState.deleted and newState.dirty
				assert newState.payloadHash == getItemPayloadHash(newItem)
				assert tuple(item.itemId for item in storage.history) == (newItemId,)
				assert storage.getItemById(None, newItemId) == newItem
			finally:
				storage.close()

	def testExistingSchemaAddsCategoryItemIdIndex(self) -> None:
		"""Restore the item lookup index without changing existing stored state."""
		with TemporaryDirectory() as directory:
			root = Path(directory)
			dataPath = root / "index-migration.db"
			storage = ClipboardStorage(dataPath, root / "missing-history.json")
			storage.createCategory("Saved")
			itemId = storage.addHistory(ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="saved"))
			assert itemId is not None
			storage.copyHistoryItemsToCategoryById((itemId,), "Saved")
			storage.bindOneDriveAccount("account.tenant")
			before = (
				storage.history,
				storage.categoryNames,
				storage.getItems("Saved"),
				storage.getOneDriveSnapshot(),
			)
			storage.close()

			connection = sqlite3.connect(dataPath)
			connection.execute("DROP INDEX categoryItems_itemId_idx")
			connection.close()

			for _ in range(2):
				storage = ClipboardStorage(dataPath, root / "missing-history.json")
				try:
					self.assertEqual(
						before,
						(
							storage.history,
							storage.categoryNames,
							storage.getItems("Saved"),
							storage.getOneDriveSnapshot(),
						),
					)
					with storage._readConnection() as connection:
						indexRow = connection.execute(
							"SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
							("categoryItems_itemId_idx",),
						).fetchone()
					self.assertIsNotNone(indexRow)
				finally:
					storage.close()

	def testImageLimitFailureRollsBackHistoryPruning(self) -> None:
		"""Restore all stored state when pruning cannot make room alongside a collected image."""
		with TemporaryDirectory() as directory:
			root = Path(directory)
			storage = ClipboardStorage(root / "images.db", root / "missing-history.json")
			try:
				storage.createCategory("Saved")
				image = ClipboardItem(
					ClipboardItemType.IMAGE,
					imageData=b"saved",
					imageWidth=1,
					imageHeight=1,
					imageBitDepth=32,
				)
				savedId = storage.addHistory(image)
				assert savedId is not None
				storage.copyHistoryItemsToCategoryById((savedId,), "Saved")
				self.assertIsNotNone(storage.addHistory(replace(image, imageData=b"old")))
				with storage._readConnection() as connection:
					before = tuple(connection.iterdump())

				with patch.object(storageModule, "MAX_TOTAL_IMAGE_BYTES", 8):
					self.assertIsNone(storage.addHistory(replace(image, imageData=b"newer")))

				with storage._readConnection() as connection:
					self.assertEqual(before, tuple(connection.iterdump()))
			finally:
				storage.close()

	def testRemoveMissingFilesOnlyUpdatesSelectedScope(self) -> None:
		"""Clean selected file groups without changing other references."""
		with TemporaryDirectory() as directory:
			root = Path(directory)
			storage = ClipboardStorage(root / "files.db", root / "missing-history.json")
			storage.createCategory("Saved")
			fileGroup = ClipboardItem(
				ClipboardItemType.FILES,
				files=("kept.txt", "missing.txt"),
				canUpload=False,
			)
			historyId = storage.addHistory(fileGroup)
			assert historyId is not None
			storage.copyHistoryItemsToCategoryById((historyId,), "Saved")

			changed, removed, replacements = storage.removeMissingFileReferences(
				"Saved",
				(historyId,),
				frozenset({"missing.txt"}),
			)

			self.assertEqual((changed, removed), (1, 1))
			self.assertEqual(storage.getItemById(None, historyId).files, ("kept.txt", "missing.txt"))
			categoryItems = storage.getItems("Saved")
			self.assertEqual(len(categoryItems), 1)
			self.assertNotEqual(categoryItems[0].itemId, historyId)
			self.assertEqual(
				storage.getItemById("Saved", categoryItems[0].itemId).files,
				("kept.txt",),
			)
			self.assertEqual(replacements, {historyId: categoryItems[0].itemId})
			storage.close()

	def testRemoveMissingFilesDeletesFullyMissingGroups(self) -> None:
		"""Delete selected file groups when no paths remain."""
		with TemporaryDirectory() as directory:
			root = Path(directory)
			storage = ClipboardStorage(root / "files.db", root / "missing-history.json")
			fileGroup = ClipboardItem(ClipboardItemType.FILES, files=("missing.txt",))
			itemId = storage.addHistory(fileGroup)
			assert itemId is not None

			changed, removed, replacements = storage.removeMissingFileReferences(
				None,
				(itemId,),
				frozenset({"missing.txt"}),
			)

			self.assertEqual((changed, removed), (1, 1))
			self.assertEqual(replacements, {})
			self.assertFalse(storage.history)
			storage.close()

	def testRemoveMissingHistoryFilesKeepsSelectedPositionOnCollision(self) -> None:
		"""Keep a cleaned history row when the resulting file group already exists."""
		with TemporaryDirectory() as directory:
			root = Path(directory)
			storage = ClipboardStorage(root / "files.db", root / "missing-history.json")
			existingId = storage.addHistory(ClipboardItem(ClipboardItemType.FILES, files=("kept.txt",)))
			selectedId = storage.addHistory(
				ClipboardItem(ClipboardItemType.FILES, files=("missing.txt", "kept.txt")),
			)
			assert existingId is not None
			assert selectedId is not None

			changed, removed, replacements = storage.removeMissingFileReferences(
				None,
				(selectedId,),
				frozenset({"missing.txt"}),
			)

			self.assertEqual((changed, removed), (1, 1))
			history = storage.history
			self.assertEqual(2, len(history))
			self.assertNotEqual(history[0].itemId, existingId)
			self.assertEqual(storage.getItemById(None, history[0].itemId).files, ("kept.txt",))
			self.assertEqual(history[1].itemId, existingId)
			self.assertEqual(replacements, {selectedId: history[0].itemId})
			storage.close()

	def testRemoveMissingCategoryFilesKeepsSelectedPositionOnCollision(self) -> None:
		"""Keep a cleaned category row when the resulting file group already exists."""
		with TemporaryDirectory() as directory:
			root = Path(directory)
			storage = ClipboardStorage(root / "files.db", root / "missing-history.json")
			storage.createCategory("Saved")
			existingId = storage.addHistory(ClipboardItem(ClipboardItemType.FILES, files=("kept.txt",)))
			selectedId = storage.addHistory(
				ClipboardItem(ClipboardItemType.FILES, files=("missing.txt", "kept.txt")),
			)
			assert existingId is not None
			assert selectedId is not None
			storage.copyHistoryItemsToCategoryById((existingId,), "Saved")
			storage.copyHistoryItemsToCategoryById((selectedId,), "Saved")

			changed, removed, replacements = storage.removeMissingFileReferences(
				"Saved",
				(selectedId,),
				frozenset({"missing.txt"}),
			)

			self.assertEqual((changed, removed), (1, 1))
			categoryItems = storage.getItems("Saved")
			self.assertEqual(2, len(categoryItems))
			self.assertNotEqual(categoryItems[0].itemId, existingId)
			self.assertEqual(
				storage.getItemById("Saved", categoryItems[0].itemId).files,
				("kept.txt",),
			)
			self.assertEqual(categoryItems[1].itemId, existingId)
			self.assertEqual(replacements, {selectedId: categoryItems[0].itemId})
			storage.close()

	def testReadConnectionPinsOneSnapshot(self) -> None:
		"""Keep a compound read on one snapshot across a concurrent commit."""
		with TemporaryDirectory() as directory:
			root = Path(directory)
			storage = ClipboardStorage(root / "snapshot.db", root / "missing-history.json")
			itemId = storage.addHistory(ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="committed"))
			assert itemId is not None
			writeFinished = Event()
			writeErrors: list[Exception] = []

			def deleteItem() -> None:
				"""Commit a deletion while the reader retains its earlier snapshot."""
				try:
					storage.deleteItemsById(None, (itemId,))
				except Exception as error:
					writeErrors.append(error)
				finally:
					writeFinished.set()

			writer = Thread(target=deleteItem)
			try:
				with storage._readConnection() as connection:
					firstRow = connection.execute(
						"SELECT itemId FROM history ORDER BY sortOrder LIMIT 1",
					).fetchone()
					writer.start()
					writeCompletedDuringRead = writeFinished.wait(5)
					secondRow = connection.execute(
						"SELECT itemId FROM history ORDER BY sortOrder LIMIT 1",
					).fetchone()
				writer.join(5)
				self.assertTrue(writeCompletedDuringRead, "Writer waited for the read transaction")
				self.assertFalse(writer.is_alive(), "Write thread did not finish")
				self.assertFalse(writeErrors, writeErrors)
				self.assertIsNotNone(firstRow)
				self.assertIsNotNone(secondRow)
				self.assertEqual(itemId, firstRow[0])
				self.assertEqual(itemId, secondRow[0])
			finally:
				if writer.ident is not None:
					writer.join(5)
				storage.close()

	def testStorageSelfCheck(self) -> None:
		"""Require the complete storage self-check to succeed."""
		_runSelfCheck()
