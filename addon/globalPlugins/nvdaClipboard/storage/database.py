# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Persist fixed-format clipboard entries and user categories in SQLite."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, cast

from logHandler import log
from ..clipboard.payload import MAX_IMAGE_BYTES, MAX_RICH_FORMAT_BYTES, normalizeNewlinesForWin32
from .._native import loadExtensionModule
from .models import (
	CategoryExistsError,
	CategoryNameError,
	CategoryNotEmptyError,
	CategoryNotFoundError,
	ClipboardItem,
	ClipboardItemContent,
	ClipboardItemSummary,
	ClipboardItemType,
	ImageStorageLimitError,
	InvalidItemError,
	ItemNotFoundError,
	OneDriveAccountMismatchError,
	OneDriveLocalChangeError,
	OneDriveSyncSnapshot,
	ReservedCategoryNameError,
	StorageError,
	StorageFormatError,
	SyncVersion,
	UnsupportedSchemaVersionError,
	ZERO_OPERATION_ID,
)
from .syncState import (
	allocateOneDriveVersions,
	bindOneDriveState,
	categoryOrderVersion,
	hasDirtyOneDriveState,
	isOneDriveSyncItem,
	mergeCategoryItemStates,
	mergeCategoryStates,
	mergeHistoryStates,
	observeOneDriveClock,
	ONE_DRIVE_ITEM_SQL,
	oneDriveSnapshotFromConnection,
	reconcileOneDriveCategories,
	reconcileOneDriveCategoryItems,
	reconcileOneDriveHistory,
	recordCategoryItem,
	recordCategoryState,
	recordHistoryItem,
	recordHistoryRemoval,
	recordHistoryState,
	removeCompactedOneDriveCategories,
	removeOrphanedOneDriveExclusions,
	replaceOneDriveSnapshot,
	replayConcurrentOneDriveChanges,
	requiredOneDrivePayloadHashes,
	reviveCategoryIfNeeded,
	tombstoneIneligibleOneDriveItems,
	tombstoneOneDrivePayload,
	validateOneDriveSnapshot,
	validateOneDriveVersionReservation,
	validateSyncHash,
)


if TYPE_CHECKING:
	import sqlite3
else:
	# Load the bundled SQLite extension; type checkers see the equivalent stdlib sqlite3 API.
	sqlite3 = loadExtensionModule("_sqlite3", "_sqlite3.pyd")


SCHEMA_VERSION = 4
MAX_HISTORY_ITEMS = 1000
MAX_LOCAL_ONLY_HISTORY_ITEMS = 1000
MAX_IMAGE_HISTORY_ITEMS = 20
MAX_LOCAL_ONLY_IMAGE_HISTORY_ITEMS = 20
MAX_TOTAL_IMAGE_BYTES = 256 * 1024 * 1024
SUMMARY_TEXT_LIMIT = 160
FILE_PREVIEW_LIMIT = 2

DATA_FILENAME = "nvdaClipboard.db"
VERSION_2_HISTORY_FILENAME = "cphistory.db"
HISTORY_CATEGORY_NAME = "Clipboard history"


class _ImageLimitReached(Exception):
	pass


def getDefaultDataPath() -> Path:
	"""Return the SQLite storage path in the active NVDA configuration directory."""
	import globalVars

	configPath = cast(str | os.PathLike[str] | None, getattr(globalVars.appArgs, "configPath", None))
	if configPath is None:
		raise StorageError
	return Path(configPath) / DATA_FILENAME


def _nextAvailablePath(path: Path) -> Path:
	"""Return a path that does not overwrite an existing recovery artifact."""
	if not path.exists():
		return path
	index = 1
	while True:
		candidate = Path(f"{path}.{index}")
		if not candidate.exists():
			return candidate
		index += 1


def _moveAside(path: Path, suffix: str) -> Path:
	"""Move a source file to a non-overwriting recovery path."""
	destination = _nextAvailablePath(Path(f"{path}{suffix}"))
	os.replace(path, destination)
	return destination


def _moveDatabaseAside(path: Path, suffix: str) -> Path:
	"""Move a database and any WAL sidecars to matching recovery paths."""
	destination = _moveAside(path, suffix)
	for sidecarSuffix in ("-wal", "-shm"):
		sidecar = Path(f"{path}{sidecarSuffix}")
		if sidecar.exists():
			os.replace(sidecar, _nextAvailablePath(Path(f"{destination}{sidecarSuffix}")))
	return destination


def _normalizeVersion2History(items: Iterable[str]) -> list[str]:
	"""Return non-empty version 2 history text in first-occurrence order."""
	normalized: list[str] = []
	seen: set[str] = set()
	for item in items:
		item = item.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")
		if not item or item in seen:
			continue
		seen.add(item)
		normalized.append(item)
	return normalized


def _readVersion2History(path: Path) -> list[str]:
	"""Read the JSON string list written by version 2 releases."""
	try:
		with path.open("r", encoding="utf-8-sig") as historyFile:
			rawData = cast(object, json.load(historyFile))
	except (UnicodeError, json.JSONDecodeError) as error:
		raise StorageFormatError(path) from error
	if not isinstance(rawData, list):
		raise StorageFormatError(path)
	itemObjects = cast(list[object], rawData)
	if not all(isinstance(item, str) for item in itemObjects):
		raise StorageFormatError(path)
	return _normalizeVersion2History(cast(list[str], itemObjects))


def _openDatabase(path: Path) -> sqlite3.Connection:
	"""Open, configure, and initialize one SQLite connection."""
	path.parent.mkdir(parents=True, exist_ok=True)
	connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
	try:
		connection.row_factory = sqlite3.Row
		_ = connection.execute("PRAGMA foreign_keys = ON")
		_ = connection.execute("PRAGMA busy_timeout = 5000")
		_initializeSchema(connection)
		_ = connection.execute("PRAGMA journal_mode = WAL")
		_ = connection.execute("PRAGMA synchronous = NORMAL")
	except Exception:
		connection.close()
		raise
	return connection


def _openReadOnlyDatabase(path: Path) -> sqlite3.Connection:
	"""Open a read-only connection without blocking stored writes."""
	# A plain path, unlike a file: URI, also works for UNC paths: the bundled SQLite rejects URI authorities.
	connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
	try:
		connection.row_factory = sqlite3.Row
		_ = connection.execute("PRAGMA query_only = ON")
		_ = connection.execute("PRAGMA foreign_keys = ON")
		_ = connection.execute("PRAGMA busy_timeout = 5000")
	except Exception:
		connection.close()
		raise
	return connection


def _isCorruptDatabaseError(error: sqlite3.DatabaseError) -> bool:
	"""Return whether SQLite identified a database as corrupt or not a database."""
	errorCode = getattr(error, "sqlite_errorcode", None)
	return isinstance(errorCode, int) and (errorCode & 0xFF) in {
		sqlite3.SQLITE_CORRUPT,
		sqlite3.SQLITE_NOTADB,
	}


def _openDatabaseWithRecovery(path: Path) -> sqlite3.Connection:
	"""Open a database, replacing only data that is certainly malformed."""
	try:
		return _openDatabase(path)
	except sqlite3.DatabaseError as error:
		if not _isCorruptDatabaseError(error):
			raise
		recoveryError = error
	except StorageFormatError as error:
		recoveryError = error
	if path.exists():
		try:
			_moveDatabaseAside(path, ".corrupt")
		except OSError:
			log.debugWarning("Could not preserve an unreadable NVDA Clipboard database.", exc_info=True)
			raise
		log.warning(
			f"Moved an unreadable NVDA Clipboard database aside after {type(recoveryError).__name__}.",
		)
	return _openDatabase(path)


def _initializeSchema(connection: sqlite3.Connection) -> None:
	"""Create the fixed schema or validate its user version."""
	versionRow = connection.execute("PRAGMA user_version").fetchone()
	if versionRow is None:
		raise StorageFormatError
	version = cast(int, versionRow[0])
	if version not in (0, 1, SCHEMA_VERSION):
		raise UnsupportedSchemaVersionError(version, SCHEMA_VERSION)
	if version in (1, SCHEMA_VERSION):
		requiredTables = {
			"items",
			"history",
			"categories",
			"categoryItems",
		}
		if version == SCHEMA_VERSION:
			requiredTables.update(
				{
					"oneDriveSync",
					"oneDriveHistory",
					"oneDriveCategories",
					"oneDriveCategoryItems",
					"oneDriveExcludedPayloads",
				},
			)
		tableRows = connection.execute(
			"SELECT name FROM sqlite_master WHERE type = 'table'",
		).fetchall()
		if not requiredTables.issubset({cast(str, row[0]) for row in tableRows}):
			raise StorageFormatError
		_ = connection.execute(
			"CREATE INDEX IF NOT EXISTS categoryItems_itemId_idx ON categoryItems (itemId)",
		)
		if version == SCHEMA_VERSION:
			return
	if version == 0:
		_ = connection.executescript(
			f"""
			BEGIN IMMEDIATE;
			CREATE TABLE items (
				itemId INTEGER PRIMARY KEY,
				contentType TEXT NOT NULL CHECK (contentType IN (
					'{ClipboardItemType.PLAIN_TEXT.value}',
					'{ClipboardItemType.FORMATTED_TEXT.value}',
					'{ClipboardItemType.IMAGE.value}',
					'{ClipboardItemType.TEXT_AND_IMAGE.value}',
					'{ClipboardItemType.FILES.value}'
				)),
				text TEXT NOT NULL,
				textPreview TEXT NOT NULL,
				html BLOB,
				rtf BLOB,
				imageData BLOB,
				imageByteCount INTEGER NOT NULL,
				imageWidth INTEGER,
				imageHeight INTEGER,
				imageBitDepth INTEGER,
				filesJson TEXT NOT NULL,
				fileCount INTEGER NOT NULL,
				filesPreviewJson TEXT NOT NULL,
				canUpload INTEGER NOT NULL CHECK (canUpload IN (0, 1)),
				dedupKey BLOB NOT NULL,
				payloadHash BLOB NOT NULL
			);
			CREATE INDEX items_dedupKey_idx ON items (dedupKey);
			CREATE INDEX items_payloadHash_idx ON items (payloadHash);
			CREATE TABLE history (
				itemId INTEGER PRIMARY KEY REFERENCES items (itemId) ON DELETE CASCADE,
				sortOrder INTEGER NOT NULL
			);
			CREATE INDEX history_sortOrder_idx ON history (sortOrder);
			CREATE TABLE categories (
				categoryId INTEGER PRIMARY KEY,
				name TEXT NOT NULL,
				nameFolded TEXT NOT NULL UNIQUE,
				sortOrder INTEGER NOT NULL
			);
			CREATE INDEX categories_sortOrder_idx ON categories (sortOrder);
			CREATE TABLE categoryItems (
				categoryId INTEGER NOT NULL REFERENCES categories (categoryId) ON DELETE CASCADE,
				itemId INTEGER NOT NULL REFERENCES items (itemId) ON DELETE CASCADE,
				sortOrder INTEGER NOT NULL,
				PRIMARY KEY (categoryId, itemId)
			);
			CREATE INDEX categoryItems_sortOrder_idx ON categoryItems (categoryId, sortOrder);
			CREATE INDEX categoryItems_itemId_idx ON categoryItems (itemId);
			PRAGMA user_version = 1;
			COMMIT;
			""",
		)
	_migrateSchemaVersion1(connection)


def _migrateSchemaVersion1(connection: sqlite3.Connection) -> None:
	"""Transactionally add OneDrive synchronization state to a version 1 database."""
	_ = connection.execute("BEGIN IMMEDIATE")
	try:
		_ = connection.execute(
			"""
			CREATE TABLE oneDriveSync (
				singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
				accountId TEXT,
				logicalClock INTEGER NOT NULL CHECK (logicalClock >= 0),
				generationClock INTEGER NOT NULL CHECK (generationClock >= 0),
				generationOperationId TEXT NOT NULL
			)
			""",
		)
		_ = connection.execute(
			"""
			INSERT INTO oneDriveSync (
				singleton, accountId, logicalClock, generationClock, generationOperationId
			) VALUES (1, NULL, 0, 0, ?)
			""",
			(ZERO_OPERATION_ID,),
		)
		_ = connection.execute(
			"""
			CREATE TABLE oneDriveHistory (
				dedupKey BLOB PRIMARY KEY CHECK (length(dedupKey) = 32),
				payloadHash BLOB CHECK (payloadHash IS NULL OR length(payloadHash) = 32),
				clock INTEGER NOT NULL CHECK (clock >= 0),
				operationId TEXT NOT NULL,
				deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
				dirty INTEGER NOT NULL CHECK (dirty IN (0, 1)),
				CHECK ((deleted = 0 AND payloadHash IS NOT NULL) OR (deleted = 1 AND payloadHash IS NULL))
			)
			""",
		)
		_ = connection.execute(
			"""
			CREATE TABLE oneDriveCategories (
				nameFolded TEXT PRIMARY KEY,
				name TEXT NOT NULL,
				orderClock INTEGER NOT NULL CHECK (orderClock >= 0),
				orderOperationId TEXT NOT NULL,
				clock INTEGER NOT NULL CHECK (clock >= 0),
				operationId TEXT NOT NULL,
				deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
				dirty INTEGER NOT NULL CHECK (dirty IN (0, 1))
			)
			""",
		)
		_ = connection.execute(
			"""
			CREATE TABLE oneDriveCategoryItems (
				nameFolded TEXT NOT NULL,
				payloadHash BLOB NOT NULL CHECK (length(payloadHash) = 32),
				clock INTEGER NOT NULL CHECK (clock >= 0),
				operationId TEXT NOT NULL,
				deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
				dirty INTEGER NOT NULL CHECK (dirty IN (0, 1)),
				PRIMARY KEY (nameFolded, payloadHash)
			)
			""",
		)
		_ = connection.execute(
			"""
			CREATE TABLE oneDriveExcludedPayloads (
				payloadHash BLOB PRIMARY KEY CHECK (length(payloadHash) = 32)
			)
			""",
		)
		_ = connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
		_ = connection.execute("COMMIT")
	except Exception:
		with suppress(sqlite3.Error):
			_ = connection.execute("ROLLBACK")
		raise


def _digestParts(parts: Iterable[bytes]) -> bytes:
	"""Return an unambiguous SHA-256 digest of byte parts."""
	digest = sha256()
	for part in parts:
		digest.update(len(part).to_bytes(8, "little"))
		digest.update(part)
	return digest.digest()


def _dedupKey(item: ClipboardItem) -> bytes:
	"""Return the history identity derived from an entry's primary content."""
	if item.contentType in (
		ClipboardItemType.PLAIN_TEXT,
		ClipboardItemType.FORMATTED_TEXT,
		ClipboardItemType.TEXT_AND_IMAGE,
	):
		return _digestParts((b"text", item.text.encode("utf-8")))
	if item.contentType == ClipboardItemType.IMAGE:
		assert item.imageData is not None
		return _digestParts((b"image", item.imageData))
	return _digestParts(
		(
			b"files",
			*(os.path.normcase(filePath).casefold().encode("utf-8") for filePath in item.files),
		),
	)


def _payloadHash(item: ClipboardItem) -> bytes:
	"""Return a digest covering every immutable entry field."""
	return _digestParts(
		(
			item.contentType.value.encode("ascii"),
			item.text.encode("utf-8"),
			item.html or b"",
			item.rtf or b"",
			item.imageData or b"",
			str(item.imageWidth or 0).encode("ascii"),
			str(item.imageHeight or 0).encode("ascii"),
			str(item.imageBitDepth or 0).encode("ascii"),
			*(filePath.encode("utf-8") for filePath in item.files),
			b"1" if item.canUpload else b"0",
		),
	)


def getItemPayloadHash(item: ClipboardItem) -> bytes:
	"""Return the immutable payload identity used by local and cloud storage."""
	_validateItem(item)
	return _payloadHash(item)


def _validateItem(item: ClipboardItem) -> None:
	"""Validate the field combinations for one fixed clipboard entry type."""
	if not isinstance(item.contentType, ClipboardItemType) or not isinstance(item.canUpload, bool):
		raise InvalidItemError(item)
	if item.html is not None and not isinstance(item.html, bytes):
		raise InvalidItemError(item)
	if item.rtf is not None and not isinstance(item.rtf, bytes):
		raise InvalidItemError(item)
	if item.imageData is not None and not isinstance(item.imageData, bytes):
		raise InvalidItemError(item)
	if item.html == b"" or item.rtf == b"" or item.imageData == b"" or "\0" in item.text:
		raise InvalidItemError(item)
	if len(item.html or b"") + len(item.rtf or b"") > MAX_RICH_FORMAT_BYTES:
		raise InvalidItemError(item)
	if any(
		value is not None and value <= 0 for value in (item.imageWidth, item.imageHeight, item.imageBitDepth)
	):
		raise InvalidItemError(item)
	hasImage = item.imageData is not None
	imageMetadata = (item.imageWidth, item.imageHeight, item.imageBitDepth)
	if (hasImage and not all(value is not None for value in imageMetadata)) or (
		not hasImage and any(value is not None for value in imageMetadata)
	):
		raise InvalidItemError(item)
	if any(not isinstance(filePath, str) or not filePath or "\0" in filePath for filePath in item.files):
		raise InvalidItemError(item)
	if item.contentType == ClipboardItemType.PLAIN_TEXT:
		isValid = (
			bool(item.text) and item.html is None and item.rtf is None and not hasImage and not item.files
		)
	elif item.contentType == ClipboardItemType.FORMATTED_TEXT:
		isValid = (
			bool(item.text)
			and (item.html is not None or item.rtf is not None)
			and not hasImage
			and not item.files
		)
	elif item.contentType == ClipboardItemType.IMAGE:
		isValid = not item.text and item.html is None and item.rtf is None and hasImage and not item.files
	elif item.contentType == ClipboardItemType.TEXT_AND_IMAGE:
		isValid = bool(item.text) and hasImage and not item.files
	else:
		isValid = (
			bool(item.files) and not item.text and item.html is None and item.rtf is None and not hasImage
		)
	if not isValid:
		raise InvalidItemError(item)


def _insertItem(
	connection: sqlite3.Connection,
	item: ClipboardItem,
	dedupKey: bytes | None = None,
	payloadHash: bytes | None = None,
) -> int:
	"""Insert one validated immutable entry and return its identifier."""
	filesJson = json.dumps(item.files, ensure_ascii=False)
	filesPreviewJson = json.dumps(
		tuple(Path(filePath).name or filePath for filePath in item.files[:FILE_PREVIEW_LIMIT]),
		ensure_ascii=False,
	)
	cursor = connection.execute(
		"""
		INSERT INTO items (
			contentType, text, textPreview, html, rtf, imageData, imageByteCount,
			imageWidth, imageHeight, imageBitDepth, filesJson, fileCount, filesPreviewJson,
			canUpload, dedupKey, payloadHash
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		""",
		(
			item.contentType.value,
			item.text,
			" ".join(item.text.lstrip()[: SUMMARY_TEXT_LIMIT * 2].split())[:SUMMARY_TEXT_LIMIT],
			item.html,
			item.rtf,
			item.imageData,
			len(item.imageData or b""),
			item.imageWidth,
			item.imageHeight,
			item.imageBitDepth,
			filesJson,
			len(item.files),
			filesPreviewJson,
			int(item.canUpload),
			dedupKey or _dedupKey(item),
			payloadHash or _payloadHash(item),
		),
	)
	if cursor.lastrowid is None:
		raise StorageError
	return cursor.lastrowid


def _findReusableItemId(
	connection: sqlite3.Connection,
	payloadHash: bytes,
) -> int | None:
	"""Return the newest intact entry with exactly matching immutable content.

	The SHA-256 payload hash covers every immutable field, so an intact row whose
	verified hash matches is treated as the same content without a field comparison.
	"""
	row = connection.execute(
		"""
		SELECT itemId, contentType, text, html, rtf, imageData, imageByteCount,
			imageWidth, imageHeight, imageBitDepth, filesJson, canUpload, payloadHash
		FROM items WHERE payloadHash = ? ORDER BY itemId DESC LIMIT 1
		""",
		(payloadHash,),
	).fetchone()
	if row is None:
		return None
	try:
		# Reading the row re-verifies its stored hash, so corrupted rows are never reused.
		_ = _rowToItem(row)
	except StorageError:
		return None
	return cast(int, row["itemId"])


def _moveHistoryItemToFront(connection: sqlite3.Connection, itemId: int) -> None:
	_ = connection.execute("DELETE FROM history WHERE itemId = ?", (itemId,))
	_ = connection.execute("UPDATE history SET sortOrder = sortOrder + 1")
	_ = connection.execute("INSERT INTO history (itemId, sortOrder) VALUES (?, 0)", (itemId,))


def _moveCategoryItemsToFront(
	connection: sqlite3.Connection,
	categoryId: int,
	itemIds: tuple[int, ...],
) -> None:
	"""Place an ordered group of category entries at the front."""
	itemIds = tuple(dict.fromkeys(itemIds))
	if not itemIds:
		return
	_ = connection.executemany(
		"DELETE FROM categoryItems WHERE categoryId = ? AND itemId = ?",
		((categoryId, itemId) for itemId in itemIds),
	)
	_ = connection.execute(
		"UPDATE categoryItems SET sortOrder = sortOrder + ? WHERE categoryId = ?",
		(len(itemIds), categoryId),
	)
	_ = connection.executemany(
		"INSERT INTO categoryItems (categoryId, itemId, sortOrder) VALUES (?, ?, ?)",
		((categoryId, itemId, sortOrder) for sortOrder, itemId in enumerate(itemIds)),
	)


def _replaceHistoryReference(
	connection: sqlite3.Connection,
	oldItemId: int,
	newItemId: int,
) -> None:
	"""Replace one history reference while preserving its position."""
	if oldItemId == newItemId:
		return
	_ = connection.execute(
		"UPDATE history SET itemId = ? WHERE itemId = ?",
		(newItemId, oldItemId),
	)


def _replaceCategoryReference(
	connection: sqlite3.Connection,
	categoryId: int,
	oldItemId: int,
	newItemId: int,
) -> None:
	"""Replace one category reference while preserving its position."""
	if oldItemId == newItemId:
		return
	_ = connection.execute(
		"UPDATE categoryItems SET itemId = ? WHERE categoryId = ? AND itemId = ?",
		(newItemId, categoryId, oldItemId),
	)


def _historyReferencesItem(connection: sqlite3.Connection, itemId: int) -> bool:
	"""Return whether history already contains one item reference."""
	return connection.execute("SELECT 1 FROM history WHERE itemId = ?", (itemId,)).fetchone() is not None


def _categoryReferencesItem(connection: sqlite3.Connection, categoryId: int, itemId: int) -> bool:
	"""Return whether one category already contains one item reference."""
	return (
		connection.execute(
			"SELECT 1 FROM categoryItems WHERE categoryId = ? AND itemId = ?",
			(categoryId, itemId),
		).fetchone()
		is not None
	)


def _prependVersion2HistoryItem(connection: sqlite3.Connection, item: ClipboardItem) -> None:
	"""Merge one version 2 item at the newest end of history."""
	dedupKey = _dedupKey(item)
	row = connection.execute(
		"SELECT history.itemId FROM history JOIN items USING (itemId) WHERE items.dedupKey = ? LIMIT 1",
		(dedupKey,),
	).fetchone()
	if row is None:
		payloadHash = _payloadHash(item)
		itemId = _findReusableItemId(connection, payloadHash)
		if itemId is None:
			itemId = _insertItem(connection, item, dedupKey, payloadHash)
	else:
		itemId = cast(int, row[0])
	_moveHistoryItemToFront(connection, itemId)
	recordHistoryItem(connection, itemId)


def _garbageCollect(connection: sqlite3.Connection) -> None:
	_ = connection.execute(
		"""
		DELETE FROM items
		WHERE NOT EXISTS (SELECT 1 FROM history WHERE history.itemId = items.itemId)
		AND NOT EXISTS (SELECT 1 FROM categoryItems WHERE categoryItems.itemId = items.itemId)
		""",
	)
	removeOrphanedOneDriveExclusions(connection)


def _deleteHistoryReferences(
	connection: sqlite3.Connection,
	itemIds: Iterable[int],
	*,
	explicit: bool,
) -> None:
	"""Delete history references and update or discard their synchronization state."""
	rows: list[sqlite3.Row] = []
	for itemId in dict.fromkeys(itemIds):
		row = connection.execute(
			f"""
			SELECT items.itemId, items.dedupKey, items.payloadHash,
				CASE WHEN {ONE_DRIVE_ITEM_SQL} THEN 1 ELSE 0 END AS isOneDrive
			FROM items WHERE itemId = ?
			""",
			(itemId,),
		).fetchone()
		if row is not None:
			rows.append(row)
	_ = connection.executemany(
		"DELETE FROM history WHERE itemId = ?",
		((cast(int, row["itemId"]),) for row in rows),
	)
	for row in rows:
		recordHistoryRemoval(
			connection,
			cast(bytes, row["dedupKey"]),
			cast(bytes, row["payloadHash"]),
			isUploadable=bool(cast(int, row["isOneDrive"])),
			explicit=explicit,
		)


def _historyOverflowIds(items: Iterable[tuple[int, bool, bool]]) -> set[int]:
	"""Return entries beyond independent item and image limits for both history pools."""
	items = tuple(items)
	oneDriveItems = [item for item in items if item[2]]
	localOnlyItems = [item for item in items if not item[2]]
	overflowIds: set[int] = set()
	for pool, itemLimit, imageLimit in (
		(oneDriveItems, MAX_HISTORY_ITEMS, MAX_IMAGE_HISTORY_ITEMS),
		(localOnlyItems, MAX_LOCAL_ONLY_HISTORY_ITEMS, MAX_LOCAL_ONLY_IMAGE_HISTORY_ITEMS),
	):
		overflowIds.update(itemId for itemId, _hasImage, _isOneDrive in pool[itemLimit:])
		imageItems = [item for item in pool if item[1]]
		overflowIds.update(itemId for itemId, _hasImage, _isOneDrive in imageItems[imageLimit:])
	return overflowIds


def _enforceHistoryLimits(connection: sqlite3.Connection) -> None:
	"""Remove oldest synchronized and local-only history beyond their independent limits."""
	rows = connection.execute(
		f"""
		SELECT history.itemId, items.imageByteCount,
			CASE WHEN {ONE_DRIVE_ITEM_SQL} THEN 1 ELSE 0 END AS isOneDrive
		FROM history JOIN items USING (itemId)
		ORDER BY history.sortOrder
		""",
	).fetchall()
	overflowIds = _historyOverflowIds(
		(
			cast(int, row["itemId"]),
			cast(int, row["imageByteCount"]) > 0,
			bool(cast(int, row["isOneDrive"])),
		)
		for row in rows
	)
	if overflowIds:
		_deleteHistoryReferences(connection, overflowIds, explicit=False)
	_garbageCollect(connection)


def _totalImageBytes(connection: sqlite3.Connection) -> int:
	row = connection.execute("SELECT COALESCE(SUM(imageByteCount), 0) FROM items").fetchone()
	return 0 if row is None else cast(int, row[0])


def _localOnlyImageBytes(
	connection: sqlite3.Connection,
	oneDrivePayloadHashes: Iterable[bytes] = (),
) -> int:
	"""Return retained local-only image bytes not shared with the supplied cloud payloads."""
	oneDrivePayloadHashes = frozenset(oneDrivePayloadHashes)
	rows = connection.execute(
		f"""
		SELECT items.payloadHash, items.imageByteCount
		FROM items
		WHERE items.imageByteCount > 0 AND NOT ({ONE_DRIVE_ITEM_SQL})
		AND (
			EXISTS (SELECT 1 FROM history WHERE history.itemId = items.itemId)
			OR EXISTS (SELECT 1 FROM categoryItems WHERE categoryItems.itemId = items.itemId)
		)
		""",
	)
	return sum(
		cast(int, row["imageByteCount"])
		for row in rows
		if cast(bytes, row["payloadHash"]) not in oneDrivePayloadHashes
	)


def _localOnlyCategoryImageBytes(connection: sqlite3.Connection) -> dict[bytes, int]:
	"""Return image byte counts retained by local-only category memberships."""
	rows = connection.execute(
		f"""
		SELECT items.payloadHash, items.imageByteCount
		FROM items
		WHERE items.imageByteCount > 0 AND NOT ({ONE_DRIVE_ITEM_SQL})
		AND EXISTS (
			SELECT 1 FROM categoryItems WHERE categoryItems.itemId = items.itemId
		)
		""",
	)
	return {cast(bytes, row["payloadHash"]): cast(int, row["imageByteCount"]) for row in rows}


def _pruneLocalOnlyImageHistory(
	connection: sqlite3.Connection,
	oneDrivePayloadHashes: set[bytes],
	oneDriveImageBytes: int,
) -> None:
	"""Prune oldest uncollected local-only images until cloud payloads can fit."""
	totalBytes = _localOnlyImageBytes(connection, oneDrivePayloadHashes) + oneDriveImageBytes
	if totalBytes <= MAX_TOTAL_IMAGE_BYTES:
		return
	rows = connection.execute(
		f"""
		SELECT history.itemId, items.payloadHash, items.imageByteCount
		FROM history JOIN items USING (itemId)
		WHERE items.imageByteCount > 0 AND NOT ({ONE_DRIVE_ITEM_SQL})
		AND NOT EXISTS (
			SELECT 1 FROM categoryItems WHERE categoryItems.itemId = history.itemId
		)
		ORDER BY history.sortOrder DESC
		""",
	)
	for row in rows:
		if cast(bytes, row["payloadHash"]) in oneDrivePayloadHashes:
			continue
		_deleteHistoryReferences(connection, (cast(int, row["itemId"]),), explicit=False)
		totalBytes -= cast(int, row["imageByteCount"])
		if totalBytes <= MAX_TOTAL_IMAGE_BYTES:
			break
	if totalBytes > MAX_TOTAL_IMAGE_BYTES:
		raise ImageStorageLimitError(MAX_TOTAL_IMAGE_BYTES)
	_garbageCollect(connection)


def _enforceTotalImageBytes(connection: sqlite3.Connection, protectedItemId: int) -> None:
	"""Prune oldest uncollected image history until the hard byte limit is met."""
	while _totalImageBytes(connection) > MAX_TOTAL_IMAGE_BYTES:
		row = connection.execute(
			"""
			SELECT history.itemId
			FROM history JOIN items USING (itemId)
			WHERE items.imageByteCount > 0
			AND history.itemId != ?
			AND NOT EXISTS (
				SELECT 1 FROM categoryItems WHERE categoryItems.itemId = history.itemId
			)
			ORDER BY history.sortOrder DESC
			LIMIT 1
			""",
			(protectedItemId,),
		).fetchone()
		if row is None:
			raise _ImageLimitReached
		_deleteHistoryReferences(connection, (cast(int, row[0]),), explicit=False)
		_garbageCollect(connection)


def _importVersion2History(connection: sqlite3.Connection, history: list[str]) -> None:
	for text in reversed(history):
		_prependVersion2HistoryItem(
			connection,
			ClipboardItem(ClipboardItemType.PLAIN_TEXT, text=text),
		)
	_enforceHistoryLimits(connection)


def migrateVersion2History(
	dataPath: Path | str | None = None,
	version2HistoryPath: Path | str | None = None,
) -> bool:
	"""Migrate and archive the JSON text history written by version 2 releases."""
	targetPath = Path(dataPath) if dataPath is not None else getDefaultDataPath()
	sourcePath = (
		Path(version2HistoryPath)
		if version2HistoryPath is not None
		else getDefaultDataPath().with_name(VERSION_2_HISTORY_FILENAME)
	)
	if not sourcePath.exists():
		return False
	try:
		history = _readVersion2History(sourcePath)
	except OSError:
		log.debugWarning("Could not read legacy Clipboard Enhancement history for migration.", exc_info=True)
		return False
	except StorageFormatError:
		try:
			_moveAside(sourcePath, ".corrupt")
		except OSError:
			log.debugWarning(
				"Could not preserve malformed legacy Clipboard Enhancement history.",
				exc_info=True,
			)
		else:
			log.warning("Moved malformed legacy Clipboard Enhancement history aside.")
		return False
	connection = _openDatabaseWithRecovery(targetPath)
	try:
		with connection:
			_importVersion2History(connection, history)
		connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
	finally:
		connection.close()
	_moveAside(sourcePath, ".bak")
	return True


_SUMMARY_COLUMNS = """
	items.itemId, items.contentType, items.textPreview,
	items.imageByteCount, items.imageWidth, items.imageHeight,
	items.fileCount, items.filesPreviewJson
"""

_CONTENT_COLUMNS = """
	items.itemId, items.contentType, items.text, items.imageByteCount,
	items.imageWidth, items.imageHeight, items.imageBitDepth, items.filesJson, items.canUpload
"""


def _decodeStringTuple(value: str) -> tuple[str, ...]:
	"""Decode a trusted stored JSON string array with corruption checks."""
	try:
		rawValue = cast(object, json.loads(value))
	except json.JSONDecodeError as error:
		raise StorageFormatError from error
	if not isinstance(rawValue, list):
		raise StorageFormatError
	items = cast(list[object], rawValue)
	if not all(isinstance(item, str) for item in items):
		raise StorageFormatError
	return tuple(cast(list[str], items))


def _rowToSummary(row: sqlite3.Row) -> ClipboardItemSummary:
	"""Convert one metadata-only SQLite row to a public summary."""
	try:
		contentType = ClipboardItemType(cast(str, row["contentType"]))
	except ValueError as error:
		raise StorageFormatError from error
	return ClipboardItemSummary(
		itemId=cast(int, row["itemId"]),
		contentType=contentType,
		textPreview=cast(str, row["textPreview"]),
		imageByteCount=cast(int, row["imageByteCount"]),
		imageWidth=cast(int | None, row["imageWidth"]),
		imageHeight=cast(int | None, row["imageHeight"]),
		fileCount=cast(int, row["fileCount"]),
		filesPreview=_decodeStringTuple(cast(str, row["filesPreviewJson"])),
	)


def _rowToContent(row: sqlite3.Row) -> ClipboardItemContent:
	"""Convert one payload-free SQLite row to displayable entry content."""
	try:
		contentType = ClipboardItemType(cast(str, row["contentType"]))
	except ValueError as error:
		raise StorageFormatError from error
	content = ClipboardItemContent(
		itemId=cast(int, row["itemId"]),
		contentType=contentType,
		text=cast(str, row["text"]),
		imageByteCount=cast(int, row["imageByteCount"]),
		imageWidth=cast(int | None, row["imageWidth"]),
		imageHeight=cast(int | None, row["imageHeight"]),
		imageBitDepth=cast(int | None, row["imageBitDepth"]),
		files=_decodeStringTuple(cast(str, row["filesJson"])),
		canUpload=bool(cast(int, row["canUpload"])),
	)
	hasImage = content.imageByteCount > 0
	imageMetadata = (content.imageWidth, content.imageHeight, content.imageBitDepth)
	if content.imageByteCount < 0:
		raise StorageFormatError
	if hasImage and any(value is None or value <= 0 for value in imageMetadata):
		raise StorageFormatError
	if not hasImage and any(value is not None for value in imageMetadata):
		raise StorageFormatError
	if contentType in (ClipboardItemType.PLAIN_TEXT, ClipboardItemType.FORMATTED_TEXT):
		isValid = bool(content.text) and not hasImage and not content.files
	elif contentType == ClipboardItemType.IMAGE:
		isValid = not content.text and hasImage and not content.files
	elif contentType == ClipboardItemType.TEXT_AND_IMAGE:
		isValid = bool(content.text) and hasImage and not content.files
	else:
		isValid = bool(content.files) and not content.text and not hasImage
	if not isValid:
		raise StorageFormatError
	return content


def _rowToItem(row: sqlite3.Row) -> ClipboardItem:
	"""Convert one full SQLite row to a public immutable entry."""
	try:
		contentType = ClipboardItemType(cast(str, row["contentType"]))
	except ValueError as error:
		raise StorageFormatError from error
	item = ClipboardItem(
		contentType=contentType,
		text=cast(str, row["text"]),
		html=cast(bytes | None, row["html"]),
		rtf=cast(bytes | None, row["rtf"]),
		imageData=cast(bytes | None, row["imageData"]),
		imageWidth=cast(int | None, row["imageWidth"]),
		imageHeight=cast(int | None, row["imageHeight"]),
		imageBitDepth=cast(int | None, row["imageBitDepth"]),
		files=_decodeStringTuple(cast(str, row["filesJson"])),
		canUpload=bool(cast(int, row["canUpload"])),
	)
	_validateItem(item)
	if cast(int, row["imageByteCount"]) != len(item.imageData or b"") or cast(
		bytes,
		row["payloadHash"],
	) != _payloadHash(item):
		raise StorageFormatError
	return item


def _validateOneDriveItem(payloadHash: bytes, item: ClipboardItem) -> None:
	"""Validate one downloaded payload before it enters local storage."""
	validateSyncHash(payloadHash)
	if not isinstance(item, ClipboardItem):
		raise StorageFormatError
	try:
		_validateItem(item)
	except (InvalidItemError, TypeError, AttributeError) as error:
		raise StorageFormatError from error
	if not isOneDriveSyncItem(item) or _payloadHash(item) != payloadHash:
		raise StorageFormatError


def _loadOneDrivePayloadIds(
	connection: sqlite3.Connection,
	payloadHashes: set[bytes],
	loadItem: Callable[[bytes], ClipboardItem | None],
) -> tuple[dict[bytes, int], int]:
	"""Resolve required payload IDs and their unique image byte total."""
	payloadItemIds: dict[bytes, int] = {}
	imageBytes = 0
	for payloadHash in payloadHashes:
		row = connection.execute(
			"""
			SELECT itemId, contentType, text, html, rtf, imageData, imageByteCount,
				imageWidth, imageHeight, imageBitDepth, filesJson, canUpload, payloadHash
			FROM items WHERE payloadHash = ? ORDER BY itemId DESC LIMIT 1
			""",
			(payloadHash,),
		).fetchone()
		if row is not None:
			# Reading the row validates it and verifies its hash against the queried payload hash.
			storedItem = _rowToItem(row)
			if not isOneDriveSyncItem(storedItem):
				raise StorageFormatError
			payloadItemIds[payloadHash] = cast(int, row["itemId"])
			imageBytes += cast(int, row["imageByteCount"])
			continue
		item = loadItem(payloadHash)
		if item is None:
			raise ItemNotFoundError(payloadHash)
		_validateOneDriveItem(payloadHash, item)
		payloadItemIds[payloadHash] = _insertItem(
			connection,
			item,
			_dedupKey(item),
			payloadHash,
		)
		imageBytes += len(item.imageData or b"")
	return payloadItemIds, imageBytes


def _reconcileOneDriveState(
	connection: sqlite3.Connection,
	loadItem: Callable[[bytes], ClipboardItem | None],
) -> None:
	"""Materialize all winning live state and enforce existing storage limits."""
	payloadHashes = requiredOneDrivePayloadHashes(connection)
	payloadItemIds, oneDriveImageBytes = _loadOneDrivePayloadIds(
		connection,
		payloadHashes,
		loadItem,
	)
	_pruneLocalOnlyImageHistory(connection, payloadHashes, oneDriveImageBytes)
	reconcileOneDriveCategories(connection)
	reconcileOneDriveHistory(connection, payloadItemIds)
	reconcileOneDriveCategoryItems(connection, payloadItemIds)
	_enforceHistoryLimits(connection)
	try:
		_enforceTotalImageBytes(connection, -1)
	except _ImageLimitReached as error:
		raise ImageStorageLimitError(MAX_TOTAL_IMAGE_BYTES) from error


class ClipboardStorage:
	"""Manage immutable clipboard entries, history references, and categories."""

	def __init__(
		self,
		dataPath: Path | str | None = None,
		version2HistoryPath: Path | str | None = None,
		reservedCategoryNames: Iterable[str] = (),
	) -> None:
		self.path = Path(dataPath) if dataPath is not None else getDefaultDataPath()
		sourceHistoryPath = (
			Path(version2HistoryPath)
			if version2HistoryPath is not None
			else self.path.with_name(
				VERSION_2_HISTORY_FILENAME,
			)
		)
		self._reservedCategoryNames = {
			name.strip().casefold()
			for name in (HISTORY_CATEGORY_NAME, *reservedCategoryNames)
			if name.strip()
		}
		self._lock = RLock()
		self._readLock = RLock()
		self._connection: sqlite3.Connection | None = None
		self._readerConnection: sqlite3.Connection | None = None
		migrateVersion2History(
			self.path,
			sourceHistoryPath,
		)
		connection = _openDatabaseWithRecovery(self.path)
		try:
			readerConnection = _openReadOnlyDatabase(self.path)
		except Exception:
			connection.close()
			raise
		self._connection = connection
		self._readerConnection = readerConnection

	@property
	def history(self) -> tuple[ClipboardItemSummary, ...]:
		"""Return newest-first history metadata without loading payload BLOBs."""
		return self.getItems()

	def getItems(self, category: str | None = None) -> tuple[ClipboardItemSummary, ...]:
		"""Return newest-first history or category metadata without loading payload BLOBs."""
		with self._readConnection() as connection:
			table, condition, parameters = self._scope(connection, category)
			rows = connection.execute(
				f"SELECT {_SUMMARY_COLUMNS} FROM {table} JOIN items USING (itemId) "
				f"WHERE {condition} ORDER BY {table}.sortOrder",
				parameters,
			).fetchall()
			return tuple(_rowToSummary(row) for row in rows)

	def getSummaryAt(
		self,
		category: str | None,
		index: int,
		offset: int = 0,
	) -> tuple[ClipboardItemSummary | None, int, int]:
		"""Clamp an index, apply an offset, and return one summary, index, and total."""
		with self._readConnection() as connection:
			table, condition, parameters = self._scope(connection, category)
			countRow = connection.execute(
				f"SELECT COUNT(*) FROM {table} WHERE {condition}",
				parameters,
			).fetchone()
			if countRow is None:
				raise StorageFormatError
			itemCount = cast(int, countRow[0])
			if itemCount == 0:
				return None, 0, 0
			currentIndex = max(0, min(index, itemCount - 1))
			resolvedIndex = max(0, min(currentIndex + offset, itemCount - 1))
			row = connection.execute(
				f"SELECT {_SUMMARY_COLUMNS} FROM {table} JOIN items USING (itemId) "
				f"WHERE {condition} ORDER BY {table}.sortOrder LIMIT 1 OFFSET ?",
				(*parameters, resolvedIndex),
			).fetchone()
			if row is None:
				raise StorageFormatError
			return _rowToSummary(row), resolvedIndex, itemCount

	def getLatestHistoryItem(self) -> ClipboardItem | None:
		"""Return the newest complete history item, or ``None`` when history is empty."""
		with self._readConnection() as connection:
			row = connection.execute(
				"SELECT itemId FROM history ORDER BY sortOrder LIMIT 1",
			).fetchone()
			return None if row is None else self._getItem(connection, cast(int, row[0]))

	@property
	def categoryNames(self) -> tuple[str, ...]:
		"""Return user category names in creation order."""
		with self._readConnection() as connection:
			rows = connection.execute("SELECT name FROM categories ORDER BY sortOrder").fetchall()
			return tuple(cast(str, row[0]) for row in rows)

	def close(self) -> None:
		"""Close both SQLite connections; repeated calls are safe."""
		with self._lock, self._readLock:
			writerConnection = self._connection
			readerConnection = self._readerConnection
			self._connection = None
			self._readerConnection = None
			try:
				if readerConnection is not None:
					readerConnection.close()
			finally:
				if writerConnection is not None:
					writerConnection.close()

	def bindOneDriveAccount(self, accountId: str, reset: bool = False) -> None:
		"""Bind synchronization state to one account, optionally resetting its baseline."""
		if (
			not isinstance(accountId, str)
			or not accountId
			or accountId != accountId.strip()
			or "\0" in accountId
			or type(reset) is not bool
		):
			raise ValueError(accountId)
		with self._transaction() as connection:
			bindOneDriveState(connection, accountId, reset)

	def getOneDriveSnapshot(self) -> OneDriveSyncSnapshot:
		"""Return all synchronization metadata without loading clipboard payload BLOBs."""
		with self._readConnection() as connection:
			return oneDriveSnapshotFromConnection(connection)

	def hasPendingOneDriveChanges(self) -> bool:
		"""Return whether synchronization metadata contains a dirty local change."""
		with self._readConnection() as connection:
			return hasDirtyOneDriveState(connection)

	def excludeIneligibleOneDriveItems(self) -> bool:
		"""Keep oversized or private local entries while removing them from live cloud state."""
		with self._transaction() as connection:
			return tombstoneIneligibleOneDriveItems(connection)

	def excludeOneDrivePayload(self, payloadHash: bytes) -> bool:
		"""Keep one local payload while replacing its live cloud references with tombstones."""
		validateSyncHash(payloadHash)
		with self._transaction() as connection:
			hasChanged = tombstoneOneDrivePayload(connection, payloadHash)
			_enforceHistoryLimits(connection)
			return hasChanged

	def getOneDriveLocalOnlyCategoryImageByteCounts(self) -> dict[bytes, int]:
		"""Return local-only image sizes that category membership requires retaining."""
		with self._readConnection() as connection:
			return _localOnlyCategoryImageBytes(connection)

	def getOneDriveItem(self, payloadHash: bytes) -> ClipboardItem | None:
		"""Load one uploadable payload by hash, or return ``None`` when it is absent."""
		validateSyncHash(payloadHash)
		with self._readConnection() as connection:
			row = connection.execute(
				f"""
				SELECT contentType, text, html, rtf, imageData, imageByteCount,
					imageWidth, imageHeight, imageBitDepth, filesJson, canUpload, payloadHash
				FROM items
				WHERE payloadHash = ? AND {ONE_DRIVE_ITEM_SQL}
				ORDER BY itemId DESC LIMIT 1
				""",
				(payloadHash,),
			).fetchone()
			return None if row is None else _rowToItem(row)

	def getOneDriveItemImageByteCount(self, payloadHash: bytes) -> int | None:
		"""Return one local synchronized payload's image bytes without loading its content."""
		validateSyncHash(payloadHash)
		with self._readConnection() as connection:
			row = connection.execute(
				f"""
				SELECT imageByteCount FROM items
				WHERE payloadHash = ? AND {ONE_DRIVE_ITEM_SQL}
				ORDER BY itemId DESC LIMIT 1
				""",
				(payloadHash,),
			).fetchone()
			return None if row is None else cast(int, row[0])

	def reserveOneDriveVersions(
		self,
		expectedSnapshot: OneDriveSyncSnapshot,
		minimumClock: int,
		count: int,
	) -> tuple[SyncVersion, ...]:
		"""Reserve versions unless synchronization entities changed since a snapshot."""
		validateOneDriveVersionReservation(expectedSnapshot, minimumClock, count)
		with self._transaction() as connection:
			return allocateOneDriveVersions(connection, expectedSnapshot, minimumClock, count)

	def applyOneDriveMerge(
		self,
		snapshot: OneDriveSyncSnapshot,
		expectedSnapshot: OneDriveSyncSnapshot,
		loadItem: Callable[[bytes], ClipboardItem | None],
	) -> bool:
		"""Atomically apply newer merged cloud state and return whether local state changed."""
		validateOneDriveSnapshot(snapshot)
		validateOneDriveSnapshot(expectedSnapshot)
		if not callable(loadItem):
			raise TypeError(loadItem)
		with self._transaction() as connection:
			current = oneDriveSnapshotFromConnection(connection)
			if current.accountId != snapshot.accountId or current.accountId != expectedSnapshot.accountId:
				raise OneDriveAccountMismatchError(current.accountId, snapshot.accountId)
			if snapshot.generation < current.generation:
				raise OneDriveLocalChangeError
			if snapshot.generation > current.generation:
				if current.generation != expectedSnapshot.generation:
					raise OneDriveLocalChangeError
				removeCompactedOneDriveCategories(connection, expectedSnapshot, snapshot)
				replaceOneDriveSnapshot(connection, snapshot)
				replayConcurrentOneDriveChanges(connection, current, expectedSnapshot, snapshot)
				_reconcileOneDriveState(connection, loadItem)
				return True
			hasChanged = mergeHistoryStates(connection, snapshot.history)
			hasChanged = mergeCategoryStates(connection, snapshot.categories) or hasChanged
			hasChanged = mergeCategoryItemStates(connection, snapshot.categoryItems) or hasChanged
			observeOneDriveClock(connection, snapshot)
			if hasChanged:
				_reconcileOneDriveState(connection, loadItem)
			return hasChanged

	def addHistory(self, item: ClipboardItem, *, replaceText: str | None = None) -> int | None:
		"""Add or move an entry to newest history and return its identifier.

		Return ``None`` only when an image exceeds the single-entry limit or
		cannot fit after pruning uncollected image history.
		When ``replaceText`` is supplied, replace the newest entry only if its text matches
		after Win32 newline normalization.
		"""
		_validateItem(item)
		if len(item.imageData or b"") > MAX_IMAGE_BYTES:
			return None
		dedupKey = _dedupKey(item)
		payloadHash = _payloadHash(item)
		try:
			with self._transaction() as connection:
				replacedItemId = None
				replacedDedupKey = None
				replacedCanSync = False
				if replaceText is not None:
					row = connection.execute(
						f"""
						SELECT history.itemId, items.dedupKey,
							CASE WHEN {ONE_DRIVE_ITEM_SQL} THEN 1 ELSE 0 END AS isOneDrive,
							items.text
						FROM history JOIN items USING (itemId)
						WHERE history.itemId = (
							SELECT itemId FROM history ORDER BY sortOrder LIMIT 1
						)
							AND items.contentType IN (?, ?, ?)
						""",
						(
							ClipboardItemType.PLAIN_TEXT,
							ClipboardItemType.FORMATTED_TEXT,
							ClipboardItemType.TEXT_AND_IMAGE,
						),
					).fetchone()
					if row is not None and normalizeNewlinesForWin32(
						cast(str, row[3]),
					) == normalizeNewlinesForWin32(replaceText):
						replacedItemId = cast(int, row[0])
						replacedDedupKey = cast(bytes, row[1])
						replacedCanSync = bool(cast(int, row[2]))
				itemId = _findReusableItemId(connection, payloadHash)
				wasInserted = itemId is None
				if itemId is None:
					itemId = _insertItem(connection, item, dedupKey, payloadHash)
				duplicateRows = connection.execute(
					"""
					SELECT history.itemId
					FROM history JOIN items USING (itemId)
					WHERE items.dedupKey = ?
					""",
					(dedupKey,),
				).fetchall()
				if duplicateRows:
					_ = connection.executemany(
						"DELETE FROM history WHERE itemId = ?",
						((cast(int, row[0]),) for row in duplicateRows),
					)
				if replacedItemId is not None and replacedItemId != itemId:
					if replacedCanSync:
						recordHistoryState(connection, cast(bytes, replacedDedupKey), None)
					_ = connection.execute("DELETE FROM history WHERE itemId = ?", (replacedItemId,))
				_moveHistoryItemToFront(connection, itemId)
				recordHistoryItem(connection, itemId)
				_enforceHistoryLimits(connection)
				if wasInserted and item.imageData is not None:
					_enforceTotalImageBytes(connection, itemId)
				return itemId
		except _ImageLimitReached:
			return None

	def getItemById(self, category: str | None, itemId: int) -> ClipboardItem:
		"""Load one complete entry after confirming it still belongs to history or a category."""
		with self._readConnection() as connection:
			self._requireItem(connection, category, itemId)
			return self._getItem(connection, itemId)

	def getItemContentById(self, category: str | None, itemId: int) -> ClipboardItemContent:
		"""Load displayable history or category content by stable identifier without binary payloads."""
		with self._readConnection() as connection:
			self._requireItem(connection, category, itemId)
			return self._getItemContent(connection, itemId)

	def createCategory(self, name: str) -> str:
		"""Create an empty user category and return its trimmed name."""
		with self._transaction() as connection:
			normalizedName = self._validateNewCategoryName(connection, name)
			row = connection.execute("SELECT COALESCE(MAX(sortOrder), -1) + 1 FROM categories").fetchone()
			sortOrder = 0 if row is None else cast(int, row[0])
			_ = connection.execute(
				"INSERT INTO categories (name, nameFolded, sortOrder) VALUES (?, ?, ?)",
				(normalizedName, normalizedName.casefold(), sortOrder),
			)
			recordCategoryState(connection, normalizedName.casefold(), normalizedName, deleted=False)
			return normalizedName

	def renameCategory(self, oldName: str, newName: str) -> str:
		"""Rename a user category while preserving its position and entries."""
		with self._transaction() as connection:
			categoryId = self._categoryId(connection, oldName)
			actualOldName = self._categoryName(connection, categoryId)
			normalizedName = self._validateNewCategoryName(connection, newName, ignoredCategoryId=categoryId)
			if normalizedName == actualOldName:
				return actualOldName
			oldNameFolded = actualOldName.casefold()
			newNameFolded = normalizedName.casefold()
			if oldNameFolded == newNameFolded:
				_ = connection.execute(
					"UPDATE categories SET name = ? WHERE categoryId = ?",
					(normalizedName, categoryId),
				)
				recordCategoryState(connection, newNameFolded, normalizedName, deleted=False)
				return normalizedName
			itemRows = connection.execute(
				"""
				SELECT items.itemId
				FROM categoryItems JOIN items USING (itemId)
				WHERE categoryId = ?
				ORDER BY categoryItems.sortOrder
				""",
				(categoryId,),
			).fetchall()
			recordCategoryState(connection, oldNameFolded, actualOldName, deleted=True)
			recordCategoryState(
				connection,
				newNameFolded,
				normalizedName,
				deleted=False,
				orderVersion=categoryOrderVersion(connection, oldNameFolded),
			)
			for row in itemRows:
				recordCategoryItem(connection, oldNameFolded, cast(int, row[0]), deleted=True)
			for row in reversed(itemRows):
				recordCategoryItem(connection, newNameFolded, cast(int, row[0]), deleted=False)
			_ = connection.execute(
				"UPDATE categories SET name = ?, nameFolded = ? WHERE categoryId = ?",
				(normalizedName, newNameFolded, categoryId),
			)
			return normalizedName

	def deleteCategory(self, name: str) -> None:
		"""Delete an empty user category."""
		with self._transaction() as connection:
			categoryId = self._categoryId(connection, name)
			row = connection.execute(
				"SELECT 1 FROM categoryItems WHERE categoryId = ? LIMIT 1",
				(categoryId,),
			).fetchone()
			if row is not None:
				raise CategoryNotEmptyError(name)
			actualName = self._categoryName(connection, categoryId)
			recordCategoryState(connection, actualName.casefold(), actualName, deleted=True)
			_ = connection.execute("DELETE FROM categories WHERE categoryId = ?", (categoryId,))

	def addCategoryItem(self, categoryName: str, item: ClipboardItem) -> None:
		"""Add an immutable entry to the front of a category."""
		_validateItem(item)
		if len(item.imageData or b"") > MAX_IMAGE_BYTES:
			raise ImageStorageLimitError(MAX_IMAGE_BYTES)
		payloadHash = _payloadHash(item)
		try:
			with self._transaction() as connection:
				categoryId = self._categoryId(connection, categoryName)
				nameFolded = reviveCategoryIfNeeded(connection, categoryId)
				itemId = _findReusableItemId(connection, payloadHash)
				wasInserted = itemId is None
				if itemId is None:
					itemId = _insertItem(connection, item, payloadHash=payloadHash)
				_moveCategoryItemsToFront(connection, categoryId, (itemId,))
				recordCategoryItem(connection, nameFolded, itemId, deleted=False)
				if wasInserted and item.imageData is not None:
					_enforceTotalImageBytes(connection, itemId)
				_garbageCollect(connection)
		except _ImageLimitReached as error:
			raise ImageStorageLimitError(MAX_TOTAL_IMAGE_BYTES) from error

	def deleteItemsById(self, category: str | None, itemIds: tuple[int, ...]) -> None:
		"""Delete stable history or category entries without changing other references."""
		if not itemIds:
			return
		with self._transaction() as connection:
			if category is None:
				for itemId in itemIds:
					self._requireHistoryItem(connection, itemId)
				_deleteHistoryReferences(connection, itemIds, explicit=True)
			else:
				categoryId = self._categoryId(connection, category)
				nameFolded = self._categoryName(connection, categoryId).casefold()
				for itemId in itemIds:
					self._requireCategoryItem(connection, categoryId, itemId)
					recordCategoryItem(connection, nameFolded, itemId, deleted=True)
				_ = connection.executemany(
					"DELETE FROM categoryItems WHERE categoryId = ? AND itemId = ?",
					((categoryId, itemId) for itemId in itemIds),
				)
			_garbageCollect(connection)

	def removeMissingFileReferences(
		self,
		category: str | None,
		itemIds: tuple[int, ...],
		missingPaths: frozenset[str],
	) -> tuple[int, int, dict[int, int]]:
		"""Remove missing paths from selected history or category file groups."""
		if not itemIds or not missingPaths:
			return (0, 0, {})
		with self._transaction() as connection:
			categoryId = None
			nameFolded = None
			if category is not None:
				categoryId = self._categoryId(connection, category)
				nameFolded = self._categoryName(connection, categoryId).casefold()
			changed = 0
			removed = 0
			replacementIds: dict[int, int] = {}
			for itemId in dict.fromkeys(itemIds):
				if categoryId is None:
					self._requireHistoryItem(connection, itemId)
				else:
					self._requireCategoryItem(connection, categoryId, itemId)
				item = self._getItem(connection, itemId)
				if item.contentType != ClipboardItemType.FILES:
					continue
				remainingFiles = [filePath for filePath in item.files if filePath not in missingPaths]
				missingCount = len(item.files) - len(remainingFiles)
				if not missingCount:
					continue
				changed += 1
				removed += missingCount
				if categoryId is not None:
					recordCategoryItem(connection, cast(str, nameFolded), itemId, deleted=True)
				if remainingFiles:
					replacement = ClipboardItem(
						ClipboardItemType.FILES,
						files=tuple(remainingFiles),
						canUpload=item.canUpload,
					)
					payloadHash = _payloadHash(replacement)
					replacementId = _findReusableItemId(connection, payloadHash)
					if categoryId is None:
						if replacementId is None or _historyReferencesItem(connection, replacementId):
							replacementId = _insertItem(connection, replacement, payloadHash=payloadHash)
						_replaceHistoryReference(connection, itemId, replacementId)
					else:
						if replacementId is None or _categoryReferencesItem(
							connection,
							categoryId,
							replacementId,
						):
							replacementId = _insertItem(connection, replacement, payloadHash=payloadHash)
						_replaceCategoryReference(connection, categoryId, itemId, replacementId)
						recordCategoryItem(
							connection,
							cast(str, nameFolded),
							replacementId,
							deleted=False,
						)
					replacementIds[itemId] = replacementId
				else:
					if categoryId is None:
						_deleteHistoryReferences(connection, (itemId,), explicit=True)
					else:
						_ = connection.execute(
							"DELETE FROM categoryItems WHERE categoryId = ? AND itemId = ?",
							(categoryId, itemId),
						)
			if changed:
				_garbageCollect(connection)
			return (changed, removed, replacementIds)

	def copyHistoryItemsToCategoryById(self, itemIds: tuple[int, ...], categoryName: str) -> None:
		"""Reference ordered stable history entries at the front of a user category."""
		if not itemIds:
			return
		with self._transaction() as connection:
			orderedItemIds = tuple(dict.fromkeys(itemIds))
			for itemId in orderedItemIds:
				self._requireHistoryItem(connection, itemId)
			categoryId = self._categoryId(connection, categoryName)
			nameFolded = reviveCategoryIfNeeded(connection, categoryId)
			_moveCategoryItemsToFront(connection, categoryId, orderedItemIds)
			for itemId in reversed(orderedItemIds):
				recordCategoryItem(connection, nameFolded, itemId, deleted=False)

	def moveCategoryItemsById(
		self,
		sourceCategory: str,
		itemIds: tuple[int, ...],
		targetCategory: str,
	) -> None:
		"""Move ordered stable entry references between user categories."""
		if not itemIds:
			return
		with self._transaction() as connection:
			orderedItemIds = tuple(dict.fromkeys(itemIds))
			sourceId = self._categoryId(connection, sourceCategory)
			targetId = self._categoryId(connection, targetCategory)
			if sourceId == targetId:
				raise ValueError(sourceCategory)
			for itemId in orderedItemIds:
				self._requireCategoryItem(connection, sourceId, itemId)
			sourceNameFolded = self._categoryName(connection, sourceId).casefold()
			targetNameFolded = reviveCategoryIfNeeded(connection, targetId)
			for itemId in orderedItemIds:
				recordCategoryItem(connection, sourceNameFolded, itemId, deleted=True)
			_ = connection.executemany(
				"DELETE FROM categoryItems WHERE categoryId = ? AND itemId = ?",
				((sourceId, itemId) for itemId in orderedItemIds),
			)
			_moveCategoryItemsToFront(connection, targetId, orderedItemIds)
			for itemId in reversed(orderedItemIds):
				recordCategoryItem(connection, targetNameFolded, itemId, deleted=False)

	@contextmanager
	def _readConnection(self) -> Iterator[sqlite3.Connection]:
		"""Yield one serialized read transaction pinned to a WAL snapshot."""
		with self._readLock:
			connection = self._requireReaderConnection()
			try:
				with connection:
					_ = connection.execute("BEGIN")
					yield connection
			except sqlite3.Error as error:
				raise StorageError(self.path) from error

	@contextmanager
	def _transaction(self) -> Iterator[sqlite3.Connection]:
		"""Yield one serialized transaction and translate SQLite failures."""
		with self._lock:
			connection = self._requireConnection()
			try:
				with connection:
					yield connection
			except sqlite3.Error as error:
				raise StorageError(self.path) from error

	def _requireConnection(self) -> sqlite3.Connection:
		if self._connection is None:
			raise StorageError(self.path)
		return self._connection

	def _requireReaderConnection(self) -> sqlite3.Connection:
		"""Return the live read-only connection or report closed storage."""
		if self._readerConnection is None:
			raise StorageError(self.path)
		return self._readerConnection

	def _getItem(self, connection: sqlite3.Connection, itemId: int) -> ClipboardItem:
		"""Load one complete entry using an existing lock or transaction."""
		row = connection.execute(
			"""
			SELECT contentType, text, html, rtf, imageData, imageByteCount,
				imageWidth, imageHeight, imageBitDepth, filesJson, canUpload, payloadHash
			FROM items WHERE itemId = ?
			""",
			(itemId,),
		).fetchone()
		if row is None:
			raise ItemNotFoundError(itemId)
		return _rowToItem(row)

	def _getItemContent(self, connection: sqlite3.Connection, itemId: int) -> ClipboardItemContent:
		"""Load displayable content using an existing lock or transaction."""
		row = connection.execute(
			f"SELECT {_CONTENT_COLUMNS} FROM items WHERE itemId = ?",
			(itemId,),
		).fetchone()
		if row is None:
			raise ItemNotFoundError(itemId)
		return _rowToContent(row)

	def iterItemContents(self, category: str | None = None) -> Iterator[ClipboardItemContent]:
		"""Stream payload-free history or category content through a dedicated read-only connection."""
		connection: sqlite3.Connection | None = None
		try:
			with self._lock:
				_ = self._requireConnection()
			connection = _openReadOnlyDatabase(self.path)
			table, condition, parameters = self._scope(connection, category)
			rows = connection.execute(
				f"SELECT {_CONTENT_COLUMNS} FROM {table} JOIN items USING (itemId) "
				f"WHERE {condition} ORDER BY {table}.sortOrder",
				parameters,
			)
			for row in rows:
				yield _rowToContent(row)
		except sqlite3.Error as error:
			raise StorageError(self.path) from error
		finally:
			if connection is not None:
				connection.close()

	def _scope(
		self,
		connection: sqlite3.Connection,
		category: str | None,
	) -> tuple[str, str, tuple[int, ...]]:
		"""Return the reference table, row filter, and filter parameters for history or a category."""
		if category is None:
			return "history", "1", ()
		return "categoryItems", "categoryId = ?", (self._categoryId(connection, category),)

	def _requireItem(self, connection: sqlite3.Connection, category: str | None, itemId: int) -> None:
		"""Raise ``ItemNotFoundError`` unless history or a category references an entry."""
		if category is None:
			self._requireHistoryItem(connection, itemId)
		else:
			self._requireCategoryItem(connection, self._categoryId(connection, category), itemId)

	def _requireHistoryItem(self, connection: sqlite3.Connection, itemId: int) -> None:
		row = connection.execute("SELECT 1 FROM history WHERE itemId = ?", (itemId,)).fetchone()
		if row is None:
			raise ItemNotFoundError(itemId)

	def _requireCategoryItem(
		self,
		connection: sqlite3.Connection,
		categoryId: int,
		itemId: int,
	) -> None:
		row = connection.execute(
			"SELECT 1 FROM categoryItems WHERE categoryId = ? AND itemId = ?",
			(categoryId, itemId),
		).fetchone()
		if row is None:
			raise ItemNotFoundError(itemId)

	def _categoryId(self, connection: sqlite3.Connection, name: str) -> int:
		row = connection.execute(
			"SELECT categoryId FROM categories WHERE nameFolded = ?",
			(name.strip().casefold(),),
		).fetchone()
		if row is None:
			raise CategoryNotFoundError(name)
		return cast(int, row[0])

	def _categoryName(self, connection: sqlite3.Connection, categoryId: int) -> str:
		row = connection.execute(
			"SELECT name FROM categories WHERE categoryId = ?",
			(categoryId,),
		).fetchone()
		if row is None:
			raise CategoryNotFoundError(categoryId)
		return cast(str, row[0])

	def _validateNewCategoryName(
		self,
		connection: sqlite3.Connection,
		name: str,
		ignoredCategoryId: int | None = None,
	) -> str:
		normalizedName = name.strip()
		if not normalizedName:
			raise CategoryNameError(name)
		foldedName = normalizedName.casefold()
		if foldedName in self._reservedCategoryNames:
			raise ReservedCategoryNameError(normalizedName)
		row = connection.execute(
			"SELECT categoryId FROM categories WHERE nameFolded = ?",
			(foldedName,),
		).fetchone()
		if row is not None and cast(int, row[0]) != ignoredCategoryId:
			raise CategoryExistsError(normalizedName)
		return normalizedName
