"""Tests for standalone clipboard storage."""

# ruff: noqa: F821 -- The copied self-check resolves names from the dynamically loaded module.

from __future__ import annotations

from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
from uuid import UUID


_MODULE_DIRECTORY = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard"
_PACKAGE_NAME = "nvdaClipboardStorageTests"
_PACKAGE = ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_MODULE_DIRECTORY)]
sys.modules[_PACKAGE_NAME] = _PACKAGE
_SPEC = importlib.util.spec_from_file_location(
	f"{_PACKAGE_NAME}.storage",
	_MODULE_DIRECTORY / "storage.py",
)
assert _SPEC is not None and _SPEC.loader is not None
storageModule = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = storageModule
_LOG_HANDLER = ModuleType("logHandler")
_LOG_HANDLER.log = Mock()
with patch.dict(sys.modules, {"logHandler": _LOG_HANDLER}):
	_SPEC.loader.exec_module(storageModule)
for _name, _value in vars(storageModule).items():
	if not _name.startswith("__"):
		globals()[_name] = _value


# Keep this linear cross-feature scenario intact so storage interactions are exercised in one database lifecycle.
def _runSelfCheck() -> None:  # noqa: C901
	"""Exercise migration, synchronization state, batch references, and reopening."""
	existingOrder = ((1, True), (2, False), (3, True))
	assert _mergeSynchronizedOrder(existingOrder, (1, 3)) == (1, 2, 3)
	assert _mergeSynchronizedOrder(existingOrder, (3, 1)) == (3, 2, 1)
	assert _mergeSynchronizedOrder(existingOrder, (4, 3, 1)) == (4, 3, 2, 1)
	assert _mergeSynchronizedOrder(existingOrder, (3, 4, 1)) == (3, 2, 4, 1)
	validationVersion = SyncVersion(1, "00000000-0000-0000-0000-000000000001")
	_validateOneDriveSnapshot(
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
		try:
			_openDatabase(futureDataPath)
		except UnsupportedSchemaVersionError:
			pass
		else:
			raise AssertionError("A future schema version was accepted")
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
		version1DedupKey = getItemDedupKey(version1Item)
		assert not version1Snapshot.history
		assert not version1Snapshot.categories
		assert not version1Snapshot.categoryItems
		preSignInDeletedItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="deleted before sign-in")
		preSignInDeletedId = version1Storage.addHistory(preSignInDeletedItem)
		assert preSignInDeletedId is not None
		version1Storage.deleteHistoryItemsById((preSignInDeletedId,))
		version1Storage.renameCategory("Version 1 category", "Renamed before sign-in")
		preSignInDeletedKey = getItemDedupKey(preSignInDeletedItem)
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
		version1Storage.deleteHistoryItemsById((version1ItemId,))
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
			state.dedupKey != getItemDedupKey(evictedItem)
			for state in version1Storage.getOneDriveSnapshot().history
		)
		privateItem = ClipboardItem(
			ClipboardItemType.PLAIN_TEXT,
			text="private",
			canUpload=False,
		)
		assert version1Storage.addHistory(privateItem) is not None
		assert all(
			state.dedupKey != getItemDedupKey(privateItem)
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
		oversizedDedupKey = getItemDedupKey(oversizedItem)
		oversizedPayloadHash = getItemPayloadHash(oversizedItem)
		with version1Storage._transaction() as connection:
			_recordHistoryLive(connection, oversizedDedupKey, oversizedPayloadHash)
			_recordCategoryItemState(
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
		assert oversizedItemId in {item.itemId for item in version1Storage.getCategoryItems("Local only")}
		remoteItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="remote change")
		remotePayloadHash = getItemPayloadHash(remoteItem)
		remoteState = OneDriveHistoryState(
			getItemDedupKey(remoteItem),
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
		assert oversizedItemId in {item.itemId for item in version1Storage.getCategoryItems("Local only")}
		assert version1Storage.getOneDriveItem(oversizedPayloadHash) is None
		with version1Storage._transaction() as connection:
			_recordHistoryLive(connection, oversizedDedupKey, oversizedPayloadHash)
			_recordCategoryItemState(
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
		try:
			reservationStorage.reserveOneDriveVersions(expectedSnapshot, 300, 1)
		except OneDriveLocalChangeError:
			pass
		else:
			raise AssertionError("A stale local synchronization snapshot was accepted")
		currentSnapshot = reservationStorage.getOneDriveSnapshot()
		futureVersions = reservationStorage.reserveOneDriveVersions(currentSnapshot, 1_000, 2)
		futureItem = ClipboardItem(ClipboardItemType.PLAIN_TEXT, text="future local change")
		assert reservationStorage.addHistory(futureItem) is not None
		futureState = next(
			state
			for state in reservationStorage.getOneDriveSnapshot().history
			if state.dedupKey == getItemDedupKey(futureItem)
		)
		assert futureState.version.clock > futureVersions[-1].clock
		try:
			reservationStorage.reserveOneDriveVersions(
				reservationStorage.getOneDriveSnapshot(),
				_MAX_SYNC_CLOCK - 1,
				0,
			)
		except ValueError:
			pass
		else:
			raise AssertionError("A clock without a local successor was observed")
		try:
			_validateSyncVersion(SyncVersion(_MAX_SYNC_CLOCK, _ZERO_OPERATION_ID))
		except StorageFormatError:
			pass
		else:
			raise AssertionError("A terminal synchronization clock was accepted")
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
		committedMiddleOrderVersion = SyncVersion(middleOrderVersion.clock, _ZERO_OPERATION_ID)
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
			state
			for state in afterGenerationSnapshot.history
			if state.dedupKey == getItemDedupKey(baselineItem)
		)
		concurrentGenerationState = next(
			state
			for state in afterGenerationSnapshot.history
			if state.dedupKey == getItemDedupKey(concurrentGenerationItem)
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
		categoryRaceItems = categoryRaceStorage.getCategoryItems("Target")
		assert len(categoryRaceItems) == 1
		assert (
			categoryRaceStorage.getCategoryItemById("Target", categoryRaceItems[0].itemId) == categoryRaceItem
		)
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
		mixedItems = categoryStorage.getCategoryItems("Mixed")
		assert len(mixedItems) == 1
		assert categoryStorage.getCategoryItemById("Mixed", mixedItems[0].itemId) == privateCategoryItem
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
		positionedItem, positionedIndex, itemCount = storage.getHistorySummaryAt(99, -1)
		assert positionedItem is not None
		assert (positionedItem.textPreview, positionedIndex, itemCount) == ("gap latest", 1, 3)
		storage.createCategory("Saved")
		plainLatestId = next(item.itemId for item in storage.history if item.textPreview == "latest")
		storage.copyHistoryItemsToCategoryById((plainLatestId,), "Saved")
		assert tuple(item.textPreview for item in storage.getCategoryItems("Saved")) == ("latest",)
		formattedId = storage.addHistory(
			ClipboardItem(
				ClipboardItemType.FORMATTED_TEXT,
				text="latest",
				html=b"<b>latest</b>",
			),
		)
		assert formattedId is not None
		assert storage.history[0].contentType == ClipboardItemType.FORMATTED_TEXT
		assert storage.getHistoryItemContentById(formattedId) == ClipboardItemContent(
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
		categoryItemId = storage.getCategoryItems("Saved")[0].itemId
		assert (
			storage.getCategoryItemById("Saved", categoryItemId).contentType == ClipboardItemType.PLAIN_TEXT
		)
		assert storage.addHistory(storage.getHistoryItemById(storage.history[0].itemId)) == formattedId
		storage.createCategory("Moved")
		plainHistoryId = next(item.itemId for item in storage.history if item.textPreview == "older")
		storage.copyHistoryItemsToCategoryById((formattedId, formattedId, plainHistoryId), "Saved")
		savedItemIds = tuple(item.itemId for item in storage.getCategoryItems("Saved"))
		assert savedItemIds == (formattedId, plainHistoryId, plainLatestId)
		try:
			storage.moveCategoryItemsById("Saved", (formattedId, -1), "Moved")
		except ItemNotFoundError:
			pass
		else:
			raise AssertionError("A partial category move was committed")
		assert tuple(item.itemId for item in storage.getCategoryItems("Saved")) == savedItemIds
		assert not storage.getCategoryItems("Moved")
		storage.moveCategoryItemsById("Saved", savedItemIds, "Moved")
		assert not storage.getCategoryItems("Saved")
		assert tuple(item.itemId for item in storage.getCategoryItems("Moved")) == savedItemIds
		storage.deleteCategoryItemsById("Moved", savedItemIds)
		assert not storage.getCategoryItems("Moved")
		historyItemIds = tuple(item.itemId for item in storage.history)
		try:
			storage.deleteHistoryItemsById((formattedId, -1))
		except ItemNotFoundError:
			pass
		else:
			raise AssertionError("A partial history deletion was committed")
		assert tuple(item.itemId for item in storage.history) == historyItemIds
		olderItemIds = tuple(
			item.itemId for item in storage.history if item.textPreview in ("older", "gap latest")
		)
		storage.deleteHistoryItemsById(olderItemIds)
		assert all(item.itemId not in olderItemIds for item in storage.history)
		longText = "x" * (SUMMARY_TEXT_LIMIT + 1) + "search tail"
		longTextId = storage.addHistory(ClipboardItem(ClipboardItemType.PLAIN_TEXT, text=longText))
		assert longTextId is not None and "search tail" not in storage.history[0].textPreview
		assert storage.getHistoryItemContentById(longTextId).text == longText
		files = (r"C:\one\first.txt", r"C:\two\second.txt", r"C:\three\target.py")
		filesId = storage.addHistory(ClipboardItem(ClipboardItemType.FILES, files=files))
		assert filesId is not None and storage.history[0].filesPreview == ("first.txt", "second.txt")
		assert storage.getHistoryItemContentById(filesId).files == files
		imageItem = ClipboardItem(
			ClipboardItemType.IMAGE,
			imageData=b"abc",
			imageWidth=1,
			imageHeight=1,
			imageBitDepth=24,
		)
		imageId = storage.addHistory(imageItem)
		assert imageId is not None
		historyContents = tuple(storage.iterHistoryItemContents())
		assert tuple(item.itemId for item in historyContents) == tuple(
			item.itemId for item in storage.history
		)
		contentById = {item.itemId: item for item in historyContents}
		assert contentById[longTextId].text == longText
		assert contentById[filesId].files == files
		assert contentById[imageId].imageByteCount == len(imageItem.imageData or b"")
		storage.copyHistoryItemsToCategoryById((filesId, longTextId), "Saved")
		categoryContents = tuple(storage.iterCategoryItemContents("Saved"))
		assert tuple(item.itemId for item in categoryContents) == (filesId, longTextId)
		with storage._transaction() as connection:
			connection.execute("UPDATE items SET imageData = ? WHERE itemId = ?", (b"abd", imageId))
		try:
			storage.getHistoryItemById(imageId)
		except StorageFormatError:
			pass
		else:
			raise AssertionError("A payload with a stale hash was accepted")
		replacementImageId = storage.addHistory(imageItem)
		assert replacementImageId is not None and replacementImageId != imageId
		assert storage.getHistoryItemById(replacementImageId) == imageItem
		with storage._transaction() as connection:
			connection.execute(
				"UPDATE items SET imageByteCount = imageByteCount + 1 WHERE itemId = ?",
				(replacementImageId,),
			)
		try:
			storage.getHistoryItemById(replacementImageId)
		except StorageFormatError:
			pass
		else:
			raise AssertionError("An incorrect image byte count was accepted")
		with storage._transaction() as connection:
			connection.execute(
				"UPDATE items SET imageByteCount = imageByteCount - 1 WHERE itemId = ?",
				(replacementImageId,),
			)
		storage.close()
		reopened = ClipboardStorage(dataPath, version2HistoryPath)
		assert reopened.getHistoryItemById(formattedId).html == b"<b>latest</b>"
		reopened.close()


class StorageSelfCheckTests(unittest.TestCase):
	"""Run the storage module's isolated behavior checks."""

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
					storage.deleteHistoryItemsById((itemId,))
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
