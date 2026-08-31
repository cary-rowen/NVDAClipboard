# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Persist fixed-format clipboard entries and user categories in SQLite."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import RLock
from typing import cast
from uuid import UUID, uuid4

from logHandler import log
from .clipboardData import MAX_IMAGE_BYTES, MAX_RICH_FORMAT_BYTES
from . import _sqlite3 as sqlite3
from .storageModels import (
	ClipboardItem,
	ClipboardItemContent,
	ClipboardItemSummary,
	ClipboardItemType,
	OneDriveCategoryItemState,
	OneDriveCategoryState,
	OneDriveHistoryState,
	OneDriveSyncSnapshot,
	SyncVersion,
	_ZERO_OPERATION_ID,
)


SCHEMA_VERSION = 4
MAX_HISTORY_ITEMS = 1000
MAX_LOCAL_ONLY_HISTORY_ITEMS = 1000
MAX_IMAGE_HISTORY_ITEMS = 20
MAX_LOCAL_ONLY_IMAGE_HISTORY_ITEMS = 20
MAX_TOTAL_IMAGE_BYTES = 256 * 1024 * 1024
MAX_ONE_DRIVE_TEXT_BYTES = 24 * 1024 * 1024
SUMMARY_TEXT_LIMIT = 160
FILE_PREVIEW_LIMIT = 2
_MAX_SYNC_CLOCK = (1 << 63) - 1

DATA_FILENAME = "nvdaClipboard.db"
VERSION_2_HISTORY_FILENAME = "cphistory.db"
HISTORY_CATEGORY_NAME = "Clipboard history"


_ONE_DRIVE_ITEM_SQL = f"""
	items.canUpload = 1
	AND items.contentType != '{ClipboardItemType.FILES.value}'
	AND length(CAST(items.text AS BLOB)) <= {MAX_ONE_DRIVE_TEXT_BYTES}
	AND COALESCE(length(items.html), 0) + COALESCE(length(items.rtf), 0) <= {MAX_RICH_FORMAT_BYTES}
	AND COALESCE(length(items.imageData), 0) <= {MAX_IMAGE_BYTES}
	AND NOT EXISTS (
		SELECT 1 FROM oneDriveExcludedPayloads
		WHERE oneDriveExcludedPayloads.payloadHash = items.payloadHash
	)
"""


class StorageError(Exception):
	"""Base error for clipboard storage failures."""


class StorageFormatError(StorageError):
	"""Raised when persisted data does not have the expected structure."""


class UnsupportedSchemaVersionError(StorageError):
	"""Raised when a database uses an unsupported schema version."""

	def __init__(self, foundVersion: int, supportedVersion: int = SCHEMA_VERSION) -> None:
		super().__init__(foundVersion, supportedVersion)
		self.foundVersion = foundVersion
		self.supportedVersion = supportedVersion


class InvalidItemError(StorageError):
	"""Raised when a clipboard entry has inconsistent fixed-format fields."""


class ImageStorageLimitError(StorageError):
	"""Raised when a category entry cannot fit within image storage limits."""


class ItemNotFoundError(StorageError):
	"""Raised when a stored clipboard entry no longer exists."""


class CategoryNameError(StorageError):
	"""Raised when a category name is empty after trimming whitespace."""


class ReservedCategoryNameError(StorageError):
	"""Raised when a category name is reserved for clipboard history."""


class CategoryExistsError(StorageError):
	"""Raised when a category name already exists case-insensitively."""


class CategoryNotFoundError(StorageError):
	"""Raised when a requested user category does not exist."""


class CategoryNotEmptyError(StorageError):
	"""Raised when deletion is requested for a non-empty category."""


class OneDriveAccountMismatchError(StorageError):
	"""Raised when synchronization targets a different bound account."""


class OneDriveLocalChangeError(StorageError):
	"""Raised when local synchronization state changed before version reservation."""


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
		connection.execute("PRAGMA foreign_keys = ON")
		connection.execute("PRAGMA busy_timeout = 5000")
		_initializeSchema(connection)
		connection.execute("PRAGMA journal_mode = WAL")
		connection.execute("PRAGMA synchronous = NORMAL")
	except Exception:
		connection.close()
		raise
	return connection


def _openReadOnlyDatabase(path: Path) -> sqlite3.Connection:
	"""Open a read-only connection without blocking stored writes."""
	connection = sqlite3.connect(
		f"{path.resolve().as_uri()}?mode=ro",
		uri=True,
		timeout=5,
		check_same_thread=False,
	)
	try:
		connection.row_factory = sqlite3.Row
		connection.execute("PRAGMA foreign_keys = ON")
		connection.execute("PRAGMA busy_timeout = 5000")
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
		raise UnsupportedSchemaVersionError(version)
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
		if version == SCHEMA_VERSION:
			return
	if version == 0:
		connection.executescript(
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
			PRAGMA user_version = 1;
			COMMIT;
			""",
		)
	_migrateSchemaVersion1(connection)


def _migrateSchemaVersion1(connection: sqlite3.Connection) -> None:
	"""Transactionally add OneDrive synchronization state to a version 1 database."""
	connection.execute("BEGIN IMMEDIATE")
	try:
		connection.execute(
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
		connection.execute(
			"""
			INSERT INTO oneDriveSync (
				singleton, accountId, logicalClock, generationClock, generationOperationId
			) VALUES (1, NULL, 0, 0, ?)
			""",
			(_ZERO_OPERATION_ID,),
		)
		connection.execute(
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
		connection.execute(
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
		connection.execute(
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
		connection.execute(
			"""
			CREATE TABLE oneDriveExcludedPayloads (
				payloadHash BLOB PRIMARY KEY CHECK (length(payloadHash) = 32)
			)
			""",
		)
		connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
		connection.execute("COMMIT")
	except Exception:
		with suppress(sqlite3.Error):
			connection.execute("ROLLBACK")
		raise


def _seedOneDriveState(connection: sqlite3.Connection) -> None:
	"""Create live synchronization records for existing uploadable local data."""
	historyRows = connection.execute(
		f"""
		SELECT items.dedupKey, items.payloadHash
		FROM history JOIN items USING (itemId)
		WHERE {_ONE_DRIVE_ITEM_SQL}
		ORDER BY history.sortOrder DESC
		""",
	)
	for row in historyRows:
		version = _nextSyncVersion(connection)
		connection.execute(
			"""
			INSERT INTO oneDriveHistory (
				dedupKey, payloadHash, clock, operationId, deleted, dirty
			) VALUES (?, ?, ?, ?, 0, 1)
			""",
			(
				cast(bytes, row["dedupKey"]),
				cast(bytes, row["payloadHash"]),
				version.clock,
				version.operationId,
			),
		)
	categoryRows = connection.execute(
		"SELECT name, nameFolded FROM categories ORDER BY sortOrder",
	)
	for row in categoryRows:
		version = _nextSyncVersion(connection)
		connection.execute(
			"""
			INSERT INTO oneDriveCategories (
				nameFolded, name, orderClock, orderOperationId,
				clock, operationId, deleted, dirty
			) VALUES (?, ?, ?, ?, ?, ?, 0, 1)
			""",
			(
				cast(str, row["nameFolded"]),
				cast(str, row["name"]),
				version.clock,
				version.operationId,
				version.clock,
				version.operationId,
			),
		)
	categoryItemRows = connection.execute(
		f"""
		SELECT categories.nameFolded, items.payloadHash
		FROM categoryItems
		JOIN categories USING (categoryId)
		JOIN items USING (itemId)
		WHERE {_ONE_DRIVE_ITEM_SQL}
		ORDER BY categories.sortOrder, categoryItems.sortOrder DESC
		""",
	)
	for row in categoryItemRows:
		version = _nextSyncVersion(connection)
		connection.execute(
			"""
			INSERT INTO oneDriveCategoryItems (
				nameFolded, payloadHash, clock, operationId, deleted, dirty
			) VALUES (?, ?, ?, ?, 0, 1)
			""",
			(
				cast(str, row["nameFolded"]),
				cast(bytes, row["payloadHash"]),
				version.clock,
				version.operationId,
			),
		)


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


def getItemDedupKey(item: ClipboardItem) -> bytes:
	"""Return the stable history identity used by local and cloud storage."""
	_validateItem(item)
	return _dedupKey(item)


def getItemPayloadHash(item: ClipboardItem) -> bytes:
	"""Return the immutable payload identity used by local and cloud storage."""
	_validateItem(item)
	return _payloadHash(item)


def _nextSyncVersion(connection: sqlite3.Connection) -> SyncVersion:
	"""Advance the database logical clock and return a unique version."""
	row = connection.execute("SELECT logicalClock FROM oneDriveSync WHERE singleton = 1").fetchone()
	if row is None:
		raise StorageFormatError
	clock = cast(int, row[0]) + 1
	if clock >= _MAX_SYNC_CLOCK:
		raise StorageError("OneDrive logical clock exhausted")
	connection.execute("UPDATE oneDriveSync SET logicalClock = ? WHERE singleton = 1", (clock,))
	return SyncVersion(clock, str(uuid4()))


def _hasBoundOneDriveAccount(connection: sqlite3.Connection) -> bool:
	"""Return whether synchronization metadata belongs to an account."""
	row = connection.execute("SELECT accountId FROM oneDriveSync WHERE singleton = 1").fetchone()
	if row is None:
		raise StorageFormatError
	return row[0] is not None


def isOneDriveSyncItem(item: ClipboardItem) -> bool:
	"""Return whether an item may be represented in OneDrive synchronization state."""
	return (
		item.canUpload
		and item.contentType != ClipboardItemType.FILES
		and len(item.text.encode("utf-8")) <= MAX_ONE_DRIVE_TEXT_BYTES
		and len(item.html or b"") + len(item.rtf or b"") <= MAX_RICH_FORMAT_BYTES
		and len(item.imageData or b"") <= MAX_IMAGE_BYTES
	)


def _recordHistoryLive(
	connection: sqlite3.Connection,
	dedupKey: bytes,
	payloadHash: bytes,
) -> None:
	"""Record a new live version for one history identity."""
	if not _hasBoundOneDriveAccount(connection):
		return
	version = _nextSyncVersion(connection)
	connection.execute(
		"""
		INSERT INTO oneDriveHistory (
			dedupKey, payloadHash, clock, operationId, deleted, dirty
		) VALUES (?, ?, ?, ?, 0, 1)
		ON CONFLICT (dedupKey) DO UPDATE SET
			payloadHash = excluded.payloadHash,
			clock = excluded.clock,
			operationId = excluded.operationId,
			deleted = 0,
			dirty = 1
		""",
		(dedupKey, payloadHash, version.clock, version.operationId),
	)


def _recordHistoryDeleted(connection: sqlite3.Connection, dedupKey: bytes) -> None:
	"""Record an explicit deletion for one history identity."""
	if not _hasBoundOneDriveAccount(connection):
		return
	version = _nextSyncVersion(connection)
	connection.execute(
		"""
		INSERT INTO oneDriveHistory (
			dedupKey, payloadHash, clock, operationId, deleted, dirty
		) VALUES (?, NULL, ?, ?, 1, 1)
		ON CONFLICT (dedupKey) DO UPDATE SET
			payloadHash = NULL,
			clock = excluded.clock,
			operationId = excluded.operationId,
			deleted = 1,
			dirty = 1
		""",
		(dedupKey, version.clock, version.operationId),
	)


def _recordCategoryLive(
	connection: sqlite3.Connection,
	nameFolded: str,
	name: str,
	orderVersion: SyncVersion | None = None,
) -> None:
	"""Record a live category while retaining its established order by default."""
	if not _hasBoundOneDriveAccount(connection):
		return
	version = _nextSyncVersion(connection)
	if orderVersion is None:
		row = connection.execute(
			"SELECT orderClock, orderOperationId FROM oneDriveCategories WHERE nameFolded = ?",
			(nameFolded,),
		).fetchone()
		orderVersion = version if row is None else SyncVersion(cast(int, row[0]), cast(str, row[1]))
	connection.execute(
		"""
		INSERT INTO oneDriveCategories (
			nameFolded, name, orderClock, orderOperationId,
			clock, operationId, deleted, dirty
		) VALUES (?, ?, ?, ?, ?, ?, 0, 1)
		ON CONFLICT (nameFolded) DO UPDATE SET
			name = excluded.name,
			orderClock = excluded.orderClock,
			orderOperationId = excluded.orderOperationId,
			clock = excluded.clock,
			operationId = excluded.operationId,
			deleted = 0,
			dirty = 1
		""",
		(
			nameFolded,
			name,
			orderVersion.clock,
			orderVersion.operationId,
			version.clock,
			version.operationId,
		),
	)


def _recordCategoryDeleted(
	connection: sqlite3.Connection,
	nameFolded: str,
	name: str,
) -> None:
	"""Record an explicit category deletion while retaining its order identity."""
	if not _hasBoundOneDriveAccount(connection):
		return
	version = _nextSyncVersion(connection)
	row = connection.execute(
		"SELECT orderClock, orderOperationId FROM oneDriveCategories WHERE nameFolded = ?",
		(nameFolded,),
	).fetchone()
	orderVersion = version if row is None else SyncVersion(cast(int, row[0]), cast(str, row[1]))
	connection.execute(
		"""
		INSERT INTO oneDriveCategories (
			nameFolded, name, orderClock, orderOperationId,
			clock, operationId, deleted, dirty
		) VALUES (?, ?, ?, ?, ?, ?, 1, 1)
		ON CONFLICT (nameFolded) DO UPDATE SET
			name = excluded.name,
			orderClock = excluded.orderClock,
			orderOperationId = excluded.orderOperationId,
			clock = excluded.clock,
			operationId = excluded.operationId,
			deleted = 1,
			dirty = 1
		""",
		(
			nameFolded,
			name,
			orderVersion.clock,
			orderVersion.operationId,
			version.clock,
			version.operationId,
		),
	)


def _recordCategoryItemState(
	connection: sqlite3.Connection,
	nameFolded: str,
	payloadHash: bytes,
	*,
	deleted: bool,
) -> None:
	"""Record a live or deleted category membership."""
	if not _hasBoundOneDriveAccount(connection):
		return
	version = _nextSyncVersion(connection)
	connection.execute(
		"""
		INSERT INTO oneDriveCategoryItems (
			nameFolded, payloadHash, clock, operationId, deleted, dirty
		) VALUES (?, ?, ?, ?, ?, 1)
		ON CONFLICT (nameFolded, payloadHash) DO UPDATE SET
			clock = excluded.clock,
			operationId = excluded.operationId,
			deleted = excluded.deleted,
			dirty = 1
		""",
		(nameFolded, payloadHash, version.clock, version.operationId, int(deleted)),
	)


def _recordHistoryItem(connection: sqlite3.Connection, itemId: int) -> None:
	"""Record the current history item, removing any previously uploadable replacement."""
	row = connection.execute(
		f"""
		SELECT items.dedupKey, items.payloadHash,
			CASE WHEN {_ONE_DRIVE_ITEM_SQL} THEN 1 ELSE 0 END AS isOneDrive
		FROM items WHERE itemId = ?
		""",
		(itemId,),
	).fetchone()
	if row is None:
		raise ItemNotFoundError(itemId)
	dedupKey = cast(bytes, row["dedupKey"])
	if bool(cast(int, row["isOneDrive"])):
		_recordHistoryLive(connection, dedupKey, cast(bytes, row["payloadHash"]))
		return
	stateRow = connection.execute(
		"SELECT deleted FROM oneDriveHistory WHERE dedupKey = ?",
		(dedupKey,),
	).fetchone()
	if stateRow is not None and not bool(cast(int, stateRow[0])):
		_recordHistoryDeleted(connection, dedupKey)


def _recordCategoryItem(
	connection: sqlite3.Connection,
	nameFolded: str,
	itemId: int,
	*,
	deleted: bool,
) -> None:
	"""Record a category membership only when its payload may be uploaded."""
	row = connection.execute(
		f"""
		SELECT items.payloadHash,
			CASE WHEN {_ONE_DRIVE_ITEM_SQL} THEN 1 ELSE 0 END AS isOneDrive
		FROM items WHERE itemId = ?
		""",
		(itemId,),
	).fetchone()
	if row is None:
		raise ItemNotFoundError(itemId)
	payloadHash = cast(bytes, row["payloadHash"])
	if not bool(cast(int, row["isOneDrive"])):
		stateRow = connection.execute(
			"""
			SELECT deleted FROM oneDriveCategoryItems
			WHERE nameFolded = ? AND payloadHash = ?
			""",
			(nameFolded, payloadHash),
		).fetchone()
		if stateRow is not None and not bool(cast(int, stateRow[0])):
			_recordCategoryItemState(connection, nameFolded, payloadHash, deleted=True)
		return
	_recordCategoryItemState(
		connection,
		nameFolded,
		payloadHash,
		deleted=deleted,
	)


def _reviveCategoryIfNeeded(connection: sqlite3.Connection, categoryId: int) -> str:
	"""Ensure a locally used category has a live synchronization state."""
	row = connection.execute(
		"""
		SELECT name, nameFolded FROM categories WHERE categoryId = ?
		""",
		(categoryId,),
	).fetchone()
	if row is None:
		raise CategoryNotFoundError(categoryId)
	nameFolded = cast(str, row["nameFolded"])
	_recordCategoryLive(connection, nameFolded, cast(str, row["name"]))
	return nameFolded


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
			" ".join(item.text[: SUMMARY_TEXT_LIMIT * 2].split())[:SUMMARY_TEXT_LIMIT],
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
	item: ClipboardItem,
	payloadHash: bytes,
) -> int | None:
	"""Return the newest intact entry with exactly matching immutable content."""
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
		storedItem = _rowToItem(row)
	except StorageError:
		return None
	return cast(int, row["itemId"]) if storedItem == item else None


def _moveHistoryItemToFront(connection: sqlite3.Connection, itemId: int) -> None:
	connection.execute("DELETE FROM history WHERE itemId = ?", (itemId,))
	connection.execute("UPDATE history SET sortOrder = sortOrder + 1")
	connection.execute("INSERT INTO history (itemId, sortOrder) VALUES (?, 0)", (itemId,))


def _moveCategoryItemsToFront(
	connection: sqlite3.Connection,
	categoryId: int,
	itemIds: tuple[int, ...],
) -> None:
	"""Place an ordered group of category entries at the front."""
	itemIds = tuple(dict.fromkeys(itemIds))
	if not itemIds:
		return
	connection.executemany(
		"DELETE FROM categoryItems WHERE categoryId = ? AND itemId = ?",
		((categoryId, itemId) for itemId in itemIds),
	)
	connection.execute(
		"UPDATE categoryItems SET sortOrder = sortOrder + ? WHERE categoryId = ?",
		(len(itemIds), categoryId),
	)
	connection.executemany(
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
	connection.execute(
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
	connection.execute(
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
		itemId = _findReusableItemId(connection, item, payloadHash)
		if itemId is None:
			itemId = _insertItem(connection, item, dedupKey, payloadHash)
	else:
		itemId = cast(int, row[0])
	_moveHistoryItemToFront(connection, itemId)
	_recordHistoryItem(connection, itemId)


def _garbageCollect(connection: sqlite3.Connection) -> None:
	connection.execute(
		"""
		DELETE FROM items
		WHERE NOT EXISTS (SELECT 1 FROM history WHERE history.itemId = items.itemId)
		AND NOT EXISTS (SELECT 1 FROM categoryItems WHERE categoryItems.itemId = items.itemId)
		""",
	)
	connection.execute(
		"""
		DELETE FROM oneDriveExcludedPayloads
		WHERE NOT EXISTS (
			SELECT 1 FROM items
			WHERE items.payloadHash = oneDriveExcludedPayloads.payloadHash
		)
		""",
	)


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
				CASE WHEN {_ONE_DRIVE_ITEM_SQL} THEN 1 ELSE 0 END AS isOneDrive
			FROM items WHERE itemId = ?
			""",
			(itemId,),
		).fetchone()
		if row is not None:
			rows.append(row)
	connection.executemany(
		"DELETE FROM history WHERE itemId = ?",
		((cast(int, row["itemId"]),) for row in rows),
	)
	for row in rows:
		dedupKey = cast(bytes, row["dedupKey"])
		isUploadable = bool(cast(int, row["isOneDrive"]))
		if explicit:
			stateRow = connection.execute(
				"SELECT deleted FROM oneDriveHistory WHERE dedupKey = ?",
				(dedupKey,),
			).fetchone()
			if isUploadable or (stateRow is not None and not bool(cast(int, stateRow[0]))):
				_recordHistoryDeleted(connection, dedupKey)
		else:
			connection.execute(
				"""
				DELETE FROM oneDriveHistory
				WHERE dedupKey = ? AND payloadHash = ? AND deleted = 0
				""",
				(dedupKey, cast(bytes, row["payloadHash"])),
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
			CASE WHEN {_ONE_DRIVE_ITEM_SQL} THEN 1 ELSE 0 END AS isOneDrive
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
		WHERE items.imageByteCount > 0 AND NOT ({_ONE_DRIVE_ITEM_SQL})
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
		WHERE items.imageByteCount > 0 AND NOT ({_ONE_DRIVE_ITEM_SQL})
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
		WHERE items.imageByteCount > 0 AND NOT ({_ONE_DRIVE_ITEM_SQL})
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


def _validateSyncVersion(version: SyncVersion) -> None:
	"""Validate an untrusted synchronization version."""
	if (
		not isinstance(version, SyncVersion)
		or type(version.clock) is not int
		or not 0 <= version.clock < _MAX_SYNC_CLOCK
		or not isinstance(version.operationId, str)
	):
		raise StorageFormatError
	try:
		UUID(version.operationId)
	except ValueError as error:
		raise StorageFormatError from error


def _validateSyncHash(value: bytes) -> None:
	"""Validate a SHA-256 identity received from synchronization data."""
	if not isinstance(value, bytes) or len(value) != sha256().digest_size:
		raise StorageFormatError


def _validateOneDriveSnapshot(
	snapshot: OneDriveSyncSnapshot,
) -> None:
	"""Validate a complete untrusted synchronization snapshot."""
	if (
		not isinstance(snapshot, OneDriveSyncSnapshot)
		or (
			snapshot.accountId is not None
			and (
				not isinstance(snapshot.accountId, str)
				or not snapshot.accountId.strip()
				or "\0" in snapshot.accountId
			)
		)
		or not all(
			isinstance(states, tuple)
			for states in (snapshot.history, snapshot.categories, snapshot.categoryItems)
		)
	):
		raise StorageFormatError
	_validateSyncVersion(snapshot.generation)
	historyKeys: set[bytes] = set()
	for state in snapshot.history:
		if not isinstance(state, OneDriveHistoryState):
			raise StorageFormatError
		_validateSyncHash(state.dedupKey)
		_validateSyncVersion(state.version)
		if type(state.deleted) is not bool or type(state.dirty) is not bool or state.dedupKey in historyKeys:
			raise StorageFormatError
		if state.deleted:
			if state.payloadHash is not None:
				raise StorageFormatError
		elif state.payloadHash is None:
			raise StorageFormatError
		else:
			_validateSyncHash(state.payloadHash)
		historyKeys.add(state.dedupKey)
	categoryKeys: set[str] = set()
	for state in snapshot.categories:
		if not isinstance(state, OneDriveCategoryState):
			raise StorageFormatError
		_validateSyncVersion(state.orderVersion)
		_validateSyncVersion(state.version)
		if (
			type(state.deleted) is not bool
			or type(state.dirty) is not bool
			or not isinstance(state.name, str)
			or not isinstance(state.nameFolded, str)
			or not state.name
			or state.name != state.name.strip()
			or "\0" in state.name
			or state.name.casefold() != state.nameFolded
			or state.nameFolded in categoryKeys
		):
			raise StorageFormatError
		categoryKeys.add(state.nameFolded)
	categoryItemKeys: set[tuple[str, bytes]] = set()
	for state in snapshot.categoryItems:
		if not isinstance(state, OneDriveCategoryItemState):
			raise StorageFormatError
		_validateSyncHash(state.payloadHash)
		_validateSyncVersion(state.version)
		key = (state.nameFolded, state.payloadHash)
		if (
			type(state.deleted) is not bool
			or type(state.dirty) is not bool
			or not isinstance(state.nameFolded, str)
			or state.nameFolded not in categoryKeys
			or key in categoryItemKeys
		):
			raise StorageFormatError
		categoryItemKeys.add(key)


def _validateOneDriveItem(payloadHash: bytes, item: ClipboardItem) -> None:
	"""Validate one downloaded payload before it enters local storage."""
	_validateSyncHash(payloadHash)
	if not isinstance(item, ClipboardItem):
		raise StorageFormatError
	try:
		_validateItem(item)
	except (InvalidItemError, TypeError, AttributeError) as error:
		raise StorageFormatError from error
	if not isOneDriveSyncItem(item) or _payloadHash(item) != payloadHash:
		raise StorageFormatError


def _versionFromRow(row: sqlite3.Row) -> SyncVersion:
	"""Return the primary synchronization version stored in a state row."""
	return SyncVersion(cast(int, row["clock"]), cast(str, row["operationId"]))


def _mergeHistoryStates(
	connection: sqlite3.Connection,
	states: tuple[OneDriveHistoryState, ...],
) -> bool:
	"""Merge history states that are newer than their transaction-current local versions."""
	hasChanged = False
	for state in states:
		row = connection.execute(
			"""
			SELECT payloadHash, clock, operationId, deleted, dirty
			FROM oneDriveHistory WHERE dedupKey = ?
			""",
			(state.dedupKey,),
		).fetchone()
		if row is not None and state.version < _versionFromRow(row):
			continue
		if row is not None and state.version == _versionFromRow(row):
			if (
				cast(bytes | None, row["payloadHash"]) != state.payloadHash
				or bool(cast(int, row["deleted"])) != state.deleted
			):
				raise StorageFormatError
			if bool(cast(int, row["dirty"])) != state.dirty:
				connection.execute(
					"UPDATE oneDriveHistory SET dirty = ? WHERE dedupKey = ?",
					(int(state.dirty), state.dedupKey),
				)
			continue
		connection.execute(
			"""
			INSERT INTO oneDriveHistory (
				dedupKey, payloadHash, clock, operationId, deleted, dirty
			) VALUES (?, ?, ?, ?, ?, ?)
			ON CONFLICT (dedupKey) DO UPDATE SET
				payloadHash = excluded.payloadHash,
				clock = excluded.clock,
				operationId = excluded.operationId,
				deleted = excluded.deleted,
				dirty = excluded.dirty
			""",
			(
				state.dedupKey,
				state.payloadHash,
				state.version.clock,
				state.version.operationId,
				int(state.deleted),
				int(state.dirty),
			),
		)
		hasChanged = True
	return hasChanged


def _mergeCategoryStates(
	connection: sqlite3.Connection,
	states: tuple[OneDriveCategoryState, ...],
) -> bool:
	"""Merge category states that are newer than their transaction-current local versions."""
	hasChanged = False
	for state in states:
		row = connection.execute(
			"""
			SELECT name, orderClock, orderOperationId, clock, operationId, deleted, dirty
			FROM oneDriveCategories WHERE nameFolded = ?
			""",
			(state.nameFolded,),
		).fetchone()
		orderVersion = state.orderVersion
		if row is not None:
			localVersion = _versionFromRow(row)
			localOrderVersion = SyncVersion(
				cast(int, row["orderClock"]),
				cast(str, row["orderOperationId"]),
			)
			orderVersion = min(orderVersion, localOrderVersion)
			if state.version < localVersion:
				if orderVersion < localOrderVersion:
					connection.execute(
						"""
						UPDATE oneDriveCategories SET orderClock = ?, orderOperationId = ?
						WHERE nameFolded = ?
						""",
						(orderVersion.clock, orderVersion.operationId, state.nameFolded),
					)
					hasChanged = True
				continue
			if state.version == localVersion:
				if cast(str, row["name"]) != state.name or bool(cast(int, row["deleted"])) != state.deleted:
					raise StorageFormatError
				if orderVersion < localOrderVersion or bool(cast(int, row["dirty"])) != state.dirty:
					connection.execute(
						"""
						UPDATE oneDriveCategories
						SET orderClock = ?, orderOperationId = ?, dirty = ?
						WHERE nameFolded = ?
						""",
						(
							orderVersion.clock,
							orderVersion.operationId,
							int(state.dirty),
							state.nameFolded,
						),
					)
					hasChanged = orderVersion < localOrderVersion or hasChanged
				continue
		connection.execute(
			"""
			INSERT INTO oneDriveCategories (
				nameFolded, name, orderClock, orderOperationId,
				clock, operationId, deleted, dirty
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT (nameFolded) DO UPDATE SET
				name = excluded.name,
				orderClock = excluded.orderClock,
				orderOperationId = excluded.orderOperationId,
				clock = excluded.clock,
				operationId = excluded.operationId,
				deleted = excluded.deleted,
				dirty = excluded.dirty
			""",
			(
				state.nameFolded,
				state.name,
				orderVersion.clock,
				orderVersion.operationId,
				state.version.clock,
				state.version.operationId,
				int(state.deleted),
				int(state.dirty),
			),
		)
		hasChanged = True
	return hasChanged


def _mergeCategoryItemStates(
	connection: sqlite3.Connection,
	states: tuple[OneDriveCategoryItemState, ...],
) -> bool:
	"""Merge membership states newer than their transaction-current local versions."""
	hasChanged = False
	for state in states:
		row = connection.execute(
			"""
			SELECT clock, operationId, deleted, dirty FROM oneDriveCategoryItems
			WHERE nameFolded = ? AND payloadHash = ?
			""",
			(state.nameFolded, state.payloadHash),
		).fetchone()
		if row is not None and state.version < _versionFromRow(row):
			continue
		if row is not None and state.version == _versionFromRow(row):
			if bool(cast(int, row["deleted"])) != state.deleted:
				raise StorageFormatError
			if bool(cast(int, row["dirty"])) != state.dirty:
				connection.execute(
					"""
					UPDATE oneDriveCategoryItems SET dirty = ?
					WHERE nameFolded = ? AND payloadHash = ?
					""",
					(int(state.dirty), state.nameFolded, state.payloadHash),
				)
			continue
		connection.execute(
			"""
			INSERT INTO oneDriveCategoryItems (
				nameFolded, payloadHash, clock, operationId, deleted, dirty
			) VALUES (?, ?, ?, ?, ?, ?)
			ON CONFLICT (nameFolded, payloadHash) DO UPDATE SET
				clock = excluded.clock,
				operationId = excluded.operationId,
				deleted = excluded.deleted,
				dirty = excluded.dirty
			""",
			(
				state.nameFolded,
				state.payloadHash,
				state.version.clock,
				state.version.operationId,
				int(state.deleted),
				int(state.dirty),
			),
		)
		hasChanged = True
	return hasChanged


def _observeOneDriveClock(connection: sqlite3.Connection, snapshot: OneDriveSyncSnapshot) -> None:
	"""Advance the local logical clock past every version observed remotely."""
	clocks = [snapshot.generation.clock]
	clocks.extend(state.version.clock for state in snapshot.history)
	clocks.extend(state.version.clock for state in snapshot.categories)
	clocks.extend(state.orderVersion.clock for state in snapshot.categories)
	clocks.extend(state.version.clock for state in snapshot.categoryItems)
	if not clocks:
		return
	connection.execute(
		"""
		UPDATE oneDriveSync SET logicalClock = MAX(logicalClock, ?)
		WHERE singleton = 1
		""",
		(max(clocks),),
	)


def _oneDriveSnapshotFromConnection(connection: sqlite3.Connection) -> OneDriveSyncSnapshot:
	"""Read synchronization metadata using an existing connection or transaction."""
	accountRow = connection.execute(
		"""
		SELECT accountId, generationClock, generationOperationId
		FROM oneDriveSync WHERE singleton = 1
		""",
	).fetchone()
	if accountRow is None:
		raise StorageFormatError
	historyRows = connection.execute(
		"""
		SELECT dedupKey, payloadHash, clock, operationId, deleted, dirty
		FROM oneDriveHistory ORDER BY dedupKey
		""",
	).fetchall()
	categoryRows = connection.execute(
		"""
		SELECT nameFolded, name, orderClock, orderOperationId,
			clock, operationId, deleted, dirty
		FROM oneDriveCategories ORDER BY nameFolded
		""",
	).fetchall()
	categoryItemRows = connection.execute(
		"""
		SELECT nameFolded, payloadHash, clock, operationId, deleted, dirty
		FROM oneDriveCategoryItems ORDER BY nameFolded, payloadHash
		""",
	).fetchall()
	return OneDriveSyncSnapshot(
		accountId=cast(str | None, accountRow["accountId"]),
		history=tuple(
			OneDriveHistoryState(
				dedupKey=cast(bytes, row["dedupKey"]),
				payloadHash=cast(bytes | None, row["payloadHash"]),
				version=_versionFromRow(row),
				deleted=bool(cast(int, row["deleted"])),
				dirty=bool(cast(int, row["dirty"])),
			)
			for row in historyRows
		),
		categories=tuple(
			OneDriveCategoryState(
				nameFolded=cast(str, row["nameFolded"]),
				name=cast(str, row["name"]),
				orderVersion=SyncVersion(
					cast(int, row["orderClock"]),
					cast(str, row["orderOperationId"]),
				),
				version=_versionFromRow(row),
				deleted=bool(cast(int, row["deleted"])),
				dirty=bool(cast(int, row["dirty"])),
			)
			for row in categoryRows
		),
		categoryItems=tuple(
			OneDriveCategoryItemState(
				nameFolded=cast(str, row["nameFolded"]),
				payloadHash=cast(bytes, row["payloadHash"]),
				version=_versionFromRow(row),
				deleted=bool(cast(int, row["deleted"])),
				dirty=bool(cast(int, row["dirty"])),
			)
			for row in categoryItemRows
		),
		generation=SyncVersion(
			cast(int, accountRow["generationClock"]),
			cast(str, accountRow["generationOperationId"]),
		),
	)


def _replaceOneDriveSnapshot(
	connection: sqlite3.Connection,
	snapshot: OneDriveSyncSnapshot,
) -> None:
	"""Replace metadata after a newer compacted synchronization generation wins."""
	connection.execute("DELETE FROM oneDriveHistory")
	connection.execute("DELETE FROM oneDriveCategories")
	connection.execute("DELETE FROM oneDriveCategoryItems")
	connection.executemany(
		"""
		INSERT INTO oneDriveHistory (
			dedupKey, payloadHash, clock, operationId, deleted, dirty
		) VALUES (?, ?, ?, ?, ?, ?)
		""",
		(
			(
				state.dedupKey,
				state.payloadHash,
				state.version.clock,
				state.version.operationId,
				int(state.deleted),
				int(state.dirty),
			)
			for state in snapshot.history
		),
	)
	connection.executemany(
		"""
		INSERT INTO oneDriveCategories (
			nameFolded, name, orderClock, orderOperationId,
			clock, operationId, deleted, dirty
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
		""",
		(
			(
				state.nameFolded,
				state.name,
				state.orderVersion.clock,
				state.orderVersion.operationId,
				state.version.clock,
				state.version.operationId,
				int(state.deleted),
				int(state.dirty),
			)
			for state in snapshot.categories
		),
	)
	connection.executemany(
		"""
		INSERT INTO oneDriveCategoryItems (
			nameFolded, payloadHash, clock, operationId, deleted, dirty
		) VALUES (?, ?, ?, ?, ?, ?)
		""",
		(
			(
				state.nameFolded,
				state.payloadHash,
				state.version.clock,
				state.version.operationId,
				int(state.deleted),
				int(state.dirty),
			)
			for state in snapshot.categoryItems
		),
	)
	connection.execute(
		"""
		UPDATE oneDriveSync SET generationClock = ?, generationOperationId = ?
		WHERE singleton = 1
		""",
		(snapshot.generation.clock, snapshot.generation.operationId),
	)
	_observeOneDriveClock(connection, snapshot)


def _replayConcurrentOneDriveChanges(
	connection: sqlite3.Connection,
	current: OneDriveSyncSnapshot,
	expected: OneDriveSyncSnapshot,
	committed: OneDriveSyncSnapshot,
) -> bool:
	"""Rebase dirty changes made after synchronization began onto a newer generation."""
	expectedHistory = {state.dedupKey: state for state in expected.history}
	committedHistory = {state.dedupKey: state for state in committed.history}
	history = tuple(
		state
		for state in current.history
		if state.dirty
		and expectedHistory.get(state.dedupKey) != state
		and (not state.deleted or state.dedupKey in committedHistory)
	)
	expectedCategories = {state.nameFolded: state for state in expected.categories}
	committedCategories = {state.nameFolded: state for state in committed.categories}
	categories = {
		state.nameFolded: state
		for state in current.categories
		if state.dirty
		and expectedCategories.get(state.nameFolded) != state
		and (not state.deleted or state.nameFolded in committedCategories)
	}
	expectedCategoryItems = {(state.nameFolded, state.payloadHash): state for state in expected.categoryItems}
	committedCategoryItems = {
		(state.nameFolded, state.payloadHash): state for state in committed.categoryItems
	}
	categoryItems = tuple(
		state
		for state in current.categoryItems
		if state.dirty
		and expectedCategoryItems.get((state.nameFolded, state.payloadHash)) != state
		and (not state.deleted or (state.nameFolded, state.payloadHash) in committedCategoryItems)
	)
	currentCategories = {state.nameFolded: state for state in current.categories}
	for state in categoryItems:
		committedCategory = committedCategories.get(state.nameFolded)
		if state.deleted or (committedCategory is not None and not committedCategory.deleted):
			continue
		category = currentCategories.get(state.nameFolded)
		if category is not None and not category.deleted:
			categories[state.nameFolded] = category
	renamedOrderVersions = {
		state.orderVersion: committedCategories[state.nameFolded].orderVersion
		for state in categories.values()
		if state.deleted and state.nameFolded in committedCategories
	}
	for state in sorted(history, key=lambda value: value.version):
		if state.deleted:
			_recordHistoryDeleted(connection, state.dedupKey)
		else:
			_recordHistoryLive(connection, state.dedupKey, cast(bytes, state.payloadHash))
	for state in sorted(categories.values(), key=lambda value: value.orderVersion):
		if state.deleted:
			_recordCategoryDeleted(connection, state.nameFolded, state.name)
		else:
			committedCategory = committedCategories.get(state.nameFolded)
			_recordCategoryLive(
				connection,
				state.nameFolded,
				state.name,
				(
					committedCategory.orderVersion
					if committedCategory is not None
					else renamedOrderVersions.get(state.orderVersion)
				),
			)
	for state in sorted(categoryItems, key=lambda value: value.version):
		_recordCategoryItemState(
			connection,
			state.nameFolded,
			state.payloadHash,
			deleted=state.deleted,
		)
	return bool(history or categories or categoryItems)


def _excludeIneligibleOneDriveItems(connection: sqlite3.Connection) -> bool:
	"""Replace live cloud state for locally available ineligible items with tombstones."""
	historyRows = connection.execute(
		f"""
		SELECT DISTINCT oneDriveHistory.dedupKey
		FROM oneDriveHistory
		JOIN items ON items.payloadHash = oneDriveHistory.payloadHash
		WHERE oneDriveHistory.deleted = 0 AND NOT ({_ONE_DRIVE_ITEM_SQL})
		ORDER BY oneDriveHistory.dedupKey
		""",
	).fetchall()
	categoryItemRows = connection.execute(
		f"""
		SELECT DISTINCT oneDriveCategoryItems.nameFolded, oneDriveCategoryItems.payloadHash
		FROM oneDriveCategoryItems
		JOIN items ON items.payloadHash = oneDriveCategoryItems.payloadHash
		WHERE oneDriveCategoryItems.deleted = 0 AND NOT ({_ONE_DRIVE_ITEM_SQL})
		ORDER BY oneDriveCategoryItems.nameFolded, oneDriveCategoryItems.payloadHash
		""",
	).fetchall()
	for row in historyRows:
		_recordHistoryDeleted(connection, cast(bytes, row["dedupKey"]))
	for row in categoryItemRows:
		_recordCategoryItemState(
			connection,
			cast(str, row["nameFolded"]),
			cast(bytes, row["payloadHash"]),
			deleted=True,
		)
	return bool(historyRows or categoryItemRows)


def _requiredOneDrivePayloadHashes(connection: sqlite3.Connection) -> set[bytes]:
	"""Return payload identities needed to materialize the final live state."""
	rows = connection.execute(
		"SELECT payloadHash FROM oneDriveHistory WHERE deleted = 0",
	).fetchall()
	payloadHashes = {cast(bytes, row[0]) for row in rows}
	rows = connection.execute(
		"""
		SELECT oneDriveCategoryItems.payloadHash
		FROM oneDriveCategoryItems
		JOIN oneDriveCategories USING (nameFolded)
		WHERE oneDriveCategoryItems.deleted = 0 AND oneDriveCategories.deleted = 0
		""",
	).fetchall()
	payloadHashes.update(cast(bytes, row[0]) for row in rows)
	return payloadHashes


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
			storedItem = _rowToItem(row)
			_validateOneDriveItem(payloadHash, storedItem)
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


def _reconcileOneDriveCategories(connection: sqlite3.Connection) -> None:
	"""Materialize winning category states while retaining local-only memberships."""
	stateRows = connection.execute(
		"""
		SELECT nameFolded, name, orderClock, orderOperationId, deleted
		FROM oneDriveCategories
		ORDER BY orderClock, orderOperationId
		""",
	).fetchall()
	for row in stateRows:
		nameFolded = cast(str, row["nameFolded"])
		categoryRow = connection.execute(
			"SELECT categoryId FROM categories WHERE nameFolded = ?",
			(nameFolded,),
		).fetchone()
		if not bool(cast(int, row["deleted"])):
			if categoryRow is None:
				sortRow = connection.execute(
					"SELECT COALESCE(MAX(sortOrder), -1) + 1 FROM categories",
				).fetchone()
				sortOrder = 0 if sortRow is None else cast(int, sortRow[0])
				connection.execute(
					"INSERT INTO categories (name, nameFolded, sortOrder) VALUES (?, ?, ?)",
					(cast(str, row["name"]), nameFolded, sortOrder),
				)
			else:
				connection.execute(
					"UPDATE categories SET name = ? WHERE categoryId = ?",
					(cast(str, row["name"]), cast(int, categoryRow[0])),
				)
			continue
		if categoryRow is None:
			continue
		categoryId = cast(int, categoryRow[0])
		connection.execute(
			f"""
			DELETE FROM categoryItems WHERE categoryId = ? AND itemId IN (
				SELECT itemId FROM items WHERE {_ONE_DRIVE_ITEM_SQL}
			)
			""",
			(categoryId,),
		)
		if (
			connection.execute(
				"SELECT 1 FROM categoryItems WHERE categoryId = ? LIMIT 1",
				(categoryId,),
			).fetchone()
			is None
		):
			connection.execute("DELETE FROM categories WHERE categoryId = ?", (categoryId,))
	liveRows = connection.execute(
		"""
		SELECT categories.categoryId
		FROM oneDriveCategories JOIN categories USING (nameFolded)
		WHERE oneDriveCategories.deleted = 0
		ORDER BY oneDriveCategories.orderClock, oneDriveCategories.orderOperationId,
			oneDriveCategories.nameFolded
		""",
	).fetchall()
	orderedIds = [cast(int, row[0]) for row in liveRows]
	knownIds = set(orderedIds)
	remainingRows = connection.execute(
		"SELECT categoryId FROM categories ORDER BY sortOrder",
	).fetchall()
	orderedIds.extend(cast(int, row[0]) for row in remainingRows if cast(int, row[0]) not in knownIds)
	connection.executemany(
		"UPDATE categories SET sortOrder = ? WHERE categoryId = ?",
		((sortOrder, categoryId) for sortOrder, categoryId in enumerate(orderedIds)),
	)


def _removeCompactedOneDriveCategories(
	connection: sqlite3.Connection,
	expected: OneDriveSyncSnapshot,
	committed: OneDriveSyncSnapshot,
) -> None:
	"""Remove synchronized content from categories omitted by a newer generation."""
	committedKeys = {state.nameFolded for state in committed.categories}
	obsoleteStates = tuple(state for state in expected.categories if state.nameFolded not in committedKeys)
	if not obsoleteStates:
		return
	connection.executemany(
		"""
		UPDATE oneDriveCategories SET deleted = 1
		WHERE nameFolded = ? AND clock = ? AND operationId = ?
		""",
		((state.nameFolded, state.version.clock, state.version.operationId) for state in obsoleteStates),
	)
	_reconcileOneDriveCategories(connection)


def _mergeSynchronizedOrder(
	existingItems: Iterable[tuple[int, bool]],
	synchronizedIds: Iterable[int],
) -> tuple[int, ...]:
	"""Merge cloud ordering into existing synchronized slots without promoting local-only items."""
	existingItems = tuple(existingItems)
	synchronizedIds = tuple(dict.fromkeys(synchronizedIds))
	existingSynchronized = {itemId for itemId, isSynchronized in existingItems if isSynchronized}
	remainingSynchronized = iter(synchronizedIds)
	orderedIds: list[int] = []
	for itemId, isSynchronized in existingItems:
		if not isSynchronized:
			orderedIds.append(itemId)
			continue
		for synchronizedId in remainingSynchronized:
			orderedIds.append(synchronizedId)
			if synchronizedId in existingSynchronized:
				break
	orderedIds.extend(remainingSynchronized)
	return tuple(dict.fromkeys(orderedIds))


def _reconcileOneDriveHistory(
	connection: sqlite3.Connection,
	payloadItemIds: Mapping[bytes, int],
) -> None:
	"""Rebuild uploadable history references from winning synchronization state."""
	existingRows = connection.execute(
		f"""
		SELECT history.itemId, CASE WHEN {_ONE_DRIVE_ITEM_SQL} THEN 1 ELSE 0 END AS isOneDrive
		FROM history JOIN items USING (itemId)
		ORDER BY history.sortOrder
		""",
	).fetchall()
	stateRows = connection.execute(
		"""
		SELECT dedupKey, payloadHash FROM oneDriveHistory
		WHERE deleted = 0 ORDER BY clock DESC, operationId DESC
		""",
	).fetchall()
	synchronizedIds: list[int] = []
	for row in stateRows:
		payloadHash = cast(bytes, row["payloadHash"])
		itemId = payloadItemIds[payloadHash]
		itemRow = connection.execute(
			"SELECT dedupKey FROM items WHERE itemId = ?",
			(itemId,),
		).fetchone()
		if itemRow is None or cast(bytes, itemRow[0]) != cast(bytes, row["dedupKey"]):
			raise StorageFormatError
		synchronizedIds.append(itemId)
	orderedIds = _mergeSynchronizedOrder(
		((cast(int, row["itemId"]), bool(cast(int, row["isOneDrive"]))) for row in existingRows),
		synchronizedIds,
	)
	connection.execute("DELETE FROM history")
	connection.executemany(
		"INSERT INTO history (itemId, sortOrder) VALUES (?, ?)",
		((itemId, sortOrder) for sortOrder, itemId in enumerate(dict.fromkeys(orderedIds))),
	)


def _reconcileOneDriveCategoryItems(
	connection: sqlite3.Connection,
	payloadItemIds: Mapping[bytes, int],
) -> None:
	"""Rebuild uploadable category memberships from winning synchronization state."""
	categoryRows = connection.execute(
		"""
		SELECT categories.categoryId, categories.nameFolded
		FROM categories JOIN oneDriveCategories USING (nameFolded)
		WHERE oneDriveCategories.deleted = 0
		""",
	).fetchall()
	for categoryRow in categoryRows:
		categoryId = cast(int, categoryRow["categoryId"])
		existingRows = connection.execute(
			f"""
			SELECT categoryItems.itemId,
				CASE WHEN {_ONE_DRIVE_ITEM_SQL} THEN 1 ELSE 0 END AS isOneDrive
			FROM categoryItems JOIN items USING (itemId)
			WHERE categoryId = ?
			ORDER BY categoryItems.sortOrder
			""",
			(categoryId,),
		).fetchall()
		stateRows = connection.execute(
			"""
			SELECT payloadHash FROM oneDriveCategoryItems
			WHERE nameFolded = ? AND deleted = 0
			ORDER BY clock DESC, operationId DESC
			""",
			(cast(str, categoryRow["nameFolded"]),),
		).fetchall()
		orderedIds = _mergeSynchronizedOrder(
			((cast(int, row["itemId"]), bool(cast(int, row["isOneDrive"]))) for row in existingRows),
			(payloadItemIds[cast(bytes, row[0])] for row in stateRows),
		)
		connection.execute("DELETE FROM categoryItems WHERE categoryId = ?", (categoryId,))
		connection.executemany(
			"INSERT INTO categoryItems (categoryId, itemId, sortOrder) VALUES (?, ?, ?)",
			((categoryId, itemId, sortOrder) for sortOrder, itemId in enumerate(dict.fromkeys(orderedIds))),
		)


def _reconcileOneDriveState(
	connection: sqlite3.Connection,
	loadItem: Callable[[bytes], ClipboardItem | None],
) -> None:
	"""Materialize all winning live state and enforce existing storage limits."""
	payloadHashes = _requiredOneDrivePayloadHashes(connection)
	payloadItemIds, oneDriveImageBytes = _loadOneDrivePayloadIds(
		connection,
		payloadHashes,
		loadItem,
	)
	_pruneLocalOnlyImageHistory(connection, payloadHashes, oneDriveImageBytes)
	_reconcileOneDriveCategories(connection)
	_reconcileOneDriveHistory(connection, payloadItemIds)
	_reconcileOneDriveCategoryItems(connection, payloadItemIds)
	_enforceHistoryLimits(connection)
	_garbageCollect(connection)
	try:
		_enforceTotalImageBytes(connection, -1)
	except _ImageLimitReached as error:
		raise ImageStorageLimitError(MAX_TOTAL_IMAGE_BYTES) from error
	_garbageCollect(connection)


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
		with self._readConnection() as connection:
			rows = connection.execute(
				f"SELECT {_SUMMARY_COLUMNS} FROM history JOIN items USING (itemId) "
				"ORDER BY history.sortOrder",
			).fetchall()
			return tuple(_rowToSummary(row) for row in rows)

	def getHistorySummaryAt(
		self,
		index: int,
		offset: int = 0,
	) -> tuple[ClipboardItemSummary | None, int, int]:
		"""Clamp an index, apply an offset, and return its history summary, index, and total."""
		with self._readConnection() as connection:
			countRow = connection.execute("SELECT COUNT(*) FROM history").fetchone()
			if countRow is None:
				raise StorageFormatError
			itemCount = cast(int, countRow[0])
			if itemCount == 0:
				return None, 0, 0
			currentIndex = max(0, min(index, itemCount - 1))
			resolvedIndex = max(0, min(currentIndex + offset, itemCount - 1))
			row = connection.execute(
				f"SELECT {_SUMMARY_COLUMNS} FROM history JOIN items USING (itemId) "
				"ORDER BY history.sortOrder LIMIT 1 OFFSET ?",
				(resolvedIndex,),
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
			row = connection.execute(
				"SELECT accountId FROM oneDriveSync WHERE singleton = 1",
			).fetchone()
			if row is None:
				raise StorageFormatError
			currentAccountId = cast(str | None, row[0])
			if currentAccountId not in (None, accountId) and not reset:
				raise OneDriveAccountMismatchError(currentAccountId, accountId)
			if reset or currentAccountId is None:
				connection.execute("DELETE FROM oneDriveHistory")
				connection.execute("DELETE FROM oneDriveCategories")
				connection.execute("DELETE FROM oneDriveCategoryItems")
				connection.execute(
					"""
					UPDATE oneDriveSync
					SET accountId = ?, logicalClock = 0,
						generationClock = 0, generationOperationId = ?
					WHERE singleton = 1
					""",
					(accountId, _ZERO_OPERATION_ID),
				)
				_seedOneDriveState(connection)
			else:
				connection.execute(
					"UPDATE oneDriveSync SET accountId = ? WHERE singleton = 1",
					(accountId,),
				)

	def getOneDriveSnapshot(self) -> OneDriveSyncSnapshot:
		"""Return all synchronization metadata without loading clipboard payload BLOBs."""
		with self._readConnection() as connection:
			return _oneDriveSnapshotFromConnection(connection)

	def hasPendingOneDriveChanges(self) -> bool:
		"""Return whether synchronization metadata contains a dirty local change."""
		with self._readConnection() as connection:
			row = connection.execute(
				"""
				SELECT EXISTS (
					SELECT 1 FROM oneDriveHistory WHERE dirty = 1
					UNION ALL
					SELECT 1 FROM oneDriveCategories WHERE dirty = 1
					UNION ALL
					SELECT 1 FROM oneDriveCategoryItems WHERE dirty = 1
				)
				""",
			).fetchone()
			if row is None:
				raise StorageFormatError
			return bool(cast(int, row[0]))

	def excludeIneligibleOneDriveItems(self) -> bool:
		"""Keep oversized or private local entries while removing them from live cloud state."""
		with self._transaction() as connection:
			return _excludeIneligibleOneDriveItems(connection)

	def excludeOneDrivePayload(self, payloadHash: bytes) -> bool:
		"""Keep one local payload while replacing its live cloud references with tombstones."""
		_validateSyncHash(payloadHash)
		with self._transaction() as connection:
			exclusionCursor = connection.execute(
				"""
				INSERT OR IGNORE INTO oneDriveExcludedPayloads (payloadHash)
				SELECT ? WHERE EXISTS (SELECT 1 FROM items WHERE payloadHash = ?)
				""",
				(payloadHash, payloadHash),
			)
			historyRows = connection.execute(
				"""
				SELECT dedupKey FROM oneDriveHistory
				WHERE payloadHash = ? AND deleted = 0
				""",
				(payloadHash,),
			).fetchall()
			categoryRows = connection.execute(
				"""
				SELECT nameFolded FROM oneDriveCategoryItems
				WHERE payloadHash = ? AND deleted = 0
				""",
				(payloadHash,),
			).fetchall()
			for row in historyRows:
				_recordHistoryDeleted(connection, cast(bytes, row["dedupKey"]))
			for row in categoryRows:
				_recordCategoryItemState(
					connection,
					cast(str, row["nameFolded"]),
					payloadHash,
					deleted=True,
				)
			_enforceHistoryLimits(connection)
			return exclusionCursor.rowcount > 0 or bool(historyRows or categoryRows)

	def getOneDriveLocalOnlyCategoryImageByteCounts(self) -> dict[bytes, int]:
		"""Return local-only image sizes that category membership requires retaining."""
		with self._readConnection() as connection:
			return _localOnlyCategoryImageBytes(connection)

	def getOneDriveItem(self, payloadHash: bytes) -> ClipboardItem | None:
		"""Load one uploadable payload by hash, or return ``None`` when it is absent."""
		_validateSyncHash(payloadHash)
		with self._readConnection() as connection:
			row = connection.execute(
				f"""
				SELECT contentType, text, html, rtf, imageData, imageByteCount,
					imageWidth, imageHeight, imageBitDepth, filesJson, canUpload, payloadHash
				FROM items
				WHERE payloadHash = ? AND {_ONE_DRIVE_ITEM_SQL}
				ORDER BY itemId DESC LIMIT 1
				""",
				(payloadHash,),
			).fetchone()
			return None if row is None else _rowToItem(row)

	def getOneDriveItemImageByteCount(self, payloadHash: bytes) -> int | None:
		"""Return one local synchronized payload's image bytes without loading its content."""
		_validateSyncHash(payloadHash)
		with self._readConnection() as connection:
			row = connection.execute(
				f"""
				SELECT imageByteCount FROM items
				WHERE payloadHash = ? AND {_ONE_DRIVE_ITEM_SQL}
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
		_validateOneDriveSnapshot(expectedSnapshot)
		if (
			type(minimumClock) is not int
			or not 0 <= minimumClock < _MAX_SYNC_CLOCK - 1
			or type(count) is not int
			or not 0 <= count <= 100_000
		):
			raise ValueError(minimumClock, count)
		with self._transaction() as connection:
			if _oneDriveSnapshotFromConnection(connection) != expectedSnapshot:
				raise OneDriveLocalChangeError
			row = connection.execute(
				"SELECT logicalClock FROM oneDriveSync WHERE singleton = 1",
			).fetchone()
			if row is None:
				raise StorageFormatError
			logicalClock = max(cast(int, row[0]), minimumClock)
			if logicalClock >= _MAX_SYNC_CLOCK - 1 or logicalClock + count >= _MAX_SYNC_CLOCK:
				raise ValueError(minimumClock, count)
			versions = tuple(
				SyncVersion(clock, str(uuid4()))
				for clock in range(logicalClock + 1, logicalClock + count + 1)
			)
			connection.execute(
				"UPDATE oneDriveSync SET logicalClock = ? WHERE singleton = 1",
				(logicalClock + count,),
			)
			return versions

	def applyOneDriveMerge(
		self,
		snapshot: OneDriveSyncSnapshot,
		expectedSnapshot: OneDriveSyncSnapshot,
		loadItem: Callable[[bytes], ClipboardItem | None],
	) -> bool:
		"""Atomically apply newer merged cloud state and return whether local state changed."""
		_validateOneDriveSnapshot(snapshot)
		_validateOneDriveSnapshot(expectedSnapshot)
		if not callable(loadItem):
			raise TypeError(loadItem)
		with self._transaction() as connection:
			current = _oneDriveSnapshotFromConnection(connection)
			if current.accountId != snapshot.accountId or current.accountId != expectedSnapshot.accountId:
				raise OneDriveAccountMismatchError(current.accountId, snapshot.accountId)
			if snapshot.generation < current.generation:
				raise OneDriveLocalChangeError
			if snapshot.generation > current.generation:
				if current.generation != expectedSnapshot.generation:
					raise OneDriveLocalChangeError
				_removeCompactedOneDriveCategories(connection, expectedSnapshot, snapshot)
				_replaceOneDriveSnapshot(connection, snapshot)
				_replayConcurrentOneDriveChanges(connection, current, expectedSnapshot, snapshot)
				_reconcileOneDriveState(connection, loadItem)
				return True
			hasChanged = _mergeHistoryStates(connection, snapshot.history)
			hasChanged = _mergeCategoryStates(connection, snapshot.categories) or hasChanged
			hasChanged = _mergeCategoryItemStates(connection, snapshot.categoryItems) or hasChanged
			_observeOneDriveClock(connection, snapshot)
			if hasChanged:
				_reconcileOneDriveState(connection, loadItem)
			return hasChanged

	def addHistory(self, item: ClipboardItem) -> int | None:
		"""Add or move an entry to newest history and return its identifier.

		Return ``None`` only when an image exceeds the single-entry limit or
		cannot fit after pruning uncollected image history.
		"""
		_validateItem(item)
		if len(item.imageData or b"") > MAX_IMAGE_BYTES:
			return None
		dedupKey = _dedupKey(item)
		payloadHash = _payloadHash(item)
		try:
			with self._transaction() as connection:
				itemId = _findReusableItemId(connection, item, payloadHash)
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
					connection.executemany(
						"DELETE FROM history WHERE itemId = ?",
						((cast(int, row[0]),) for row in duplicateRows),
					)
				_moveHistoryItemToFront(connection, itemId)
				_recordHistoryItem(connection, itemId)
				_enforceHistoryLimits(connection)
				if wasInserted and item.imageData is not None:
					_enforceTotalImageBytes(connection, itemId)
				_garbageCollect(connection)
				return itemId
		except _ImageLimitReached:
			return None

	def getHistoryItemById(self, itemId: int) -> ClipboardItem:
		"""Load one complete entry after confirming it still belongs to history."""
		with self._readConnection() as connection:
			self._requireHistoryItem(connection, itemId)
			return self._getItem(connection, itemId)

	def getHistoryItemContentById(self, itemId: int) -> ClipboardItemContent:
		"""Load displayable history content by stable identifier without binary payloads."""
		with self._readConnection() as connection:
			self._requireHistoryItem(connection, itemId)
			return self._getItemContent(connection, itemId)

	def getCategoryItems(self, categoryName: str) -> tuple[ClipboardItemSummary, ...]:
		"""Return newest-first category metadata without loading payload BLOBs."""
		with self._readConnection() as connection:
			categoryId = self._categoryId(connection, categoryName)
			rows = connection.execute(
				f"SELECT {_SUMMARY_COLUMNS} FROM categoryItems JOIN items USING (itemId) "
				"WHERE categoryId = ? ORDER BY categoryItems.sortOrder",
				(categoryId,),
			).fetchall()
			return tuple(_rowToSummary(row) for row in rows)

	def getCategoryItemById(self, categoryName: str, itemId: int) -> ClipboardItem:
		"""Load one complete entry after confirming it still belongs to a category."""
		with self._readConnection() as connection:
			categoryId = self._categoryId(connection, categoryName)
			self._requireCategoryItem(connection, categoryId, itemId)
			return self._getItem(connection, itemId)

	def getCategoryItemContentById(self, categoryName: str, itemId: int) -> ClipboardItemContent:
		"""Load displayable category content by stable identifier without binary payloads."""
		with self._readConnection() as connection:
			categoryId = self._categoryId(connection, categoryName)
			self._requireCategoryItem(connection, categoryId, itemId)
			return self._getItemContent(connection, itemId)

	def iterHistoryItemContents(self) -> Iterator[ClipboardItemContent]:
		"""Yield history content in display order without loading binary payloads."""
		return self._iterItemContents()

	def iterCategoryItemContents(self, categoryName: str) -> Iterator[ClipboardItemContent]:
		"""Yield one category's content in display order without loading binary payloads."""
		return self._iterItemContents(categoryName)

	def createCategory(self, name: str) -> str:
		"""Create an empty user category and return its trimmed name."""
		with self._transaction() as connection:
			normalizedName = self._validateNewCategoryName(connection, name)
			row = connection.execute("SELECT COALESCE(MAX(sortOrder), -1) + 1 FROM categories").fetchone()
			sortOrder = 0 if row is None else cast(int, row[0])
			connection.execute(
				"INSERT INTO categories (name, nameFolded, sortOrder) VALUES (?, ?, ?)",
				(normalizedName, normalizedName.casefold(), sortOrder),
			)
			_recordCategoryLive(connection, normalizedName.casefold(), normalizedName)
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
				connection.execute(
					"UPDATE categories SET name = ? WHERE categoryId = ?",
					(normalizedName, categoryId),
				)
				_recordCategoryLive(connection, newNameFolded, normalizedName)
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
			_recordCategoryDeleted(connection, oldNameFolded, actualOldName)
			orderRow = connection.execute(
				"""
				SELECT orderClock, orderOperationId
				FROM oneDriveCategories WHERE nameFolded = ?
				""",
				(oldNameFolded,),
			).fetchone()
			_recordCategoryLive(
				connection,
				newNameFolded,
				normalizedName,
				(None if orderRow is None else SyncVersion(cast(int, orderRow[0]), cast(str, orderRow[1]))),
			)
			for row in itemRows:
				_recordCategoryItem(connection, oldNameFolded, cast(int, row[0]), deleted=True)
			for row in reversed(itemRows):
				_recordCategoryItem(connection, newNameFolded, cast(int, row[0]), deleted=False)
			connection.execute(
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
			_recordCategoryDeleted(connection, actualName.casefold(), actualName)
			connection.execute("DELETE FROM categories WHERE categoryId = ?", (categoryId,))

	def addCategoryItem(self, categoryName: str, item: ClipboardItem) -> None:
		"""Add an immutable entry to the front of a category."""
		_validateItem(item)
		if len(item.imageData or b"") > MAX_IMAGE_BYTES:
			raise ImageStorageLimitError(MAX_IMAGE_BYTES)
		payloadHash = _payloadHash(item)
		try:
			with self._transaction() as connection:
				categoryId = self._categoryId(connection, categoryName)
				nameFolded = _reviveCategoryIfNeeded(connection, categoryId)
				itemId = _findReusableItemId(connection, item, payloadHash)
				wasInserted = itemId is None
				if itemId is None:
					itemId = _insertItem(connection, item, payloadHash=payloadHash)
				_moveCategoryItemsToFront(connection, categoryId, (itemId,))
				_recordCategoryItem(connection, nameFolded, itemId, deleted=False)
				if wasInserted and item.imageData is not None:
					_enforceTotalImageBytes(connection, itemId)
				_garbageCollect(connection)
		except _ImageLimitReached as error:
			raise ImageStorageLimitError(MAX_TOTAL_IMAGE_BYTES) from error

	def deleteHistoryItemsById(self, itemIds: tuple[int, ...]) -> None:
		"""Delete stable history entries without deleting category copies."""
		if not itemIds:
			return
		with self._transaction() as connection:
			for itemId in itemIds:
				self._requireHistoryItem(connection, itemId)
			_deleteHistoryReferences(connection, itemIds, explicit=True)
			_garbageCollect(connection)

	def removeMissingHistoryFileReferences(
		self,
		itemIds: tuple[int, ...],
		missingPaths: frozenset[str],
	) -> tuple[int, int, int, dict[int, int]]:
		"""Remove missing paths from selected history file groups."""
		return self._removeMissingFileReferences(itemIds, missingPaths)

	def deleteCategoryItemsById(self, categoryName: str, itemIds: tuple[int, ...]) -> None:
		"""Delete stable category entries without changing history."""
		if not itemIds:
			return
		with self._transaction() as connection:
			categoryId = self._categoryId(connection, categoryName)
			nameFolded = self._categoryName(connection, categoryId).casefold()
			for itemId in itemIds:
				self._requireCategoryItem(connection, categoryId, itemId)
				_recordCategoryItem(connection, nameFolded, itemId, deleted=True)
			connection.executemany(
				"DELETE FROM categoryItems WHERE categoryId = ? AND itemId = ?",
				((categoryId, itemId) for itemId in itemIds),
			)
			_garbageCollect(connection)

	def removeMissingCategoryFileReferences(
		self,
		categoryName: str,
		itemIds: tuple[int, ...],
		missingPaths: frozenset[str],
	) -> tuple[int, int, int, dict[int, int]]:
		"""Remove missing paths from selected category file groups."""
		return self._removeMissingFileReferences(itemIds, missingPaths, categoryName)

	def _removeMissingFileReferences(
		self,
		itemIds: tuple[int, ...],
		missingPaths: frozenset[str],
		categoryName: str | None = None,
	) -> tuple[int, int, int, dict[int, int]]:
		"""Remove missing paths from selected history or category file groups."""
		if not itemIds or not missingPaths:
			return (0, 0, 0, {})
		with self._transaction() as connection:
			categoryId = None
			nameFolded = None
			if categoryName is not None:
				categoryId = self._categoryId(connection, categoryName)
				nameFolded = self._categoryName(connection, categoryId).casefold()
			changed = 0
			deleted = 0
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
					_recordCategoryItem(connection, nameFolded, itemId, deleted=True)
				if remainingFiles:
					replacement = ClipboardItem(
						ClipboardItemType.FILES,
						files=tuple(remainingFiles),
						canUpload=item.canUpload,
					)
					payloadHash = _payloadHash(replacement)
					replacementId = _findReusableItemId(connection, replacement, payloadHash)
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
						_recordCategoryItem(connection, nameFolded, replacementId, deleted=False)
					replacementIds[itemId] = replacementId
				else:
					if categoryId is None:
						_deleteHistoryReferences(connection, (itemId,), explicit=True)
					else:
						connection.execute(
							"DELETE FROM categoryItems WHERE categoryId = ? AND itemId = ?",
							(categoryId, itemId),
						)
					deleted += 1
			if changed:
				_garbageCollect(connection)
			return (changed, deleted, removed, replacementIds)

	def copyHistoryItemsToCategoryById(self, itemIds: tuple[int, ...], categoryName: str) -> None:
		"""Reference ordered stable history entries at the front of a user category."""
		if not itemIds:
			return
		with self._transaction() as connection:
			orderedItemIds = tuple(dict.fromkeys(itemIds))
			for itemId in orderedItemIds:
				self._requireHistoryItem(connection, itemId)
			categoryId = self._categoryId(connection, categoryName)
			nameFolded = _reviveCategoryIfNeeded(connection, categoryId)
			_moveCategoryItemsToFront(connection, categoryId, orderedItemIds)
			for itemId in reversed(orderedItemIds):
				_recordCategoryItem(connection, nameFolded, itemId, deleted=False)

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
			targetNameFolded = _reviveCategoryIfNeeded(connection, targetId)
			for itemId in orderedItemIds:
				_recordCategoryItem(connection, sourceNameFolded, itemId, deleted=True)
			connection.executemany(
				"DELETE FROM categoryItems WHERE categoryId = ? AND itemId = ?",
				((sourceId, itemId) for itemId in orderedItemIds),
			)
			_moveCategoryItemsToFront(connection, targetId, orderedItemIds)
			for itemId in reversed(orderedItemIds):
				_recordCategoryItem(connection, targetNameFolded, itemId, deleted=False)

	@contextmanager
	def _readConnection(self) -> Iterator[sqlite3.Connection]:
		"""Yield one serialized read transaction pinned to a WAL snapshot."""
		with self._readLock:
			connection = self._requireReaderConnection()
			try:
				with connection:
					connection.execute("BEGIN")
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

	def _iterItemContents(self, categoryName: str | None = None) -> Iterator[ClipboardItemContent]:
		"""Stream payload-free content through a dedicated read-only connection."""
		connection: sqlite3.Connection | None = None
		try:
			with self._lock:
				self._requireConnection()
			connection = _openReadOnlyDatabase(self.path)
			if categoryName is None:
				rows = connection.execute(
					f"SELECT {_CONTENT_COLUMNS} FROM history JOIN items USING (itemId) "
					"ORDER BY history.sortOrder",
				)
			else:
				categoryId = self._categoryId(connection, categoryName)
				rows = connection.execute(
					f"SELECT {_CONTENT_COLUMNS} FROM categoryItems JOIN items USING (itemId) "
					"WHERE categoryId = ? ORDER BY categoryItems.sortOrder",
					(categoryId,),
				)
			for row in rows:
				yield _rowToContent(row)
		except sqlite3.Error as error:
			raise StorageError(self.path) from error
		finally:
			if connection is not None:
				connection.close()

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
