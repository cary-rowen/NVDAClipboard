# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Read and update the OneDrive synchronization state stored beside clipboard history."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from hashlib import sha256
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

from ..clipboard.payload import MAX_IMAGE_BYTES, MAX_RICH_FORMAT_BYTES
from .models import (
	CategoryNotFoundError,
	ClipboardItem,
	ClipboardItemType,
	ItemNotFoundError,
	OneDriveAccountMismatchError,
	OneDriveCategoryItemState,
	OneDriveCategoryState,
	OneDriveHistoryState,
	OneDriveLocalChangeError,
	OneDriveSyncSnapshot,
	StorageError,
	StorageFormatError,
	SyncVersion,
	ZERO_OPERATION_ID,
)


if TYPE_CHECKING:
	import sqlite3


MAX_ONE_DRIVE_TEXT_BYTES = 24 * 1024 * 1024
_MAX_SYNC_CLOCK = (1 << 63) - 1


ONE_DRIVE_ITEM_SQL = f"""
	items.canUpload = 1
	AND items.contentType != '{ClipboardItemType.FILES.value}'
	AND octet_length(items.text) <= {MAX_ONE_DRIVE_TEXT_BYTES}
	AND COALESCE(length(items.html), 0) + COALESCE(length(items.rtf), 0) <= {MAX_RICH_FORMAT_BYTES}
	AND COALESCE(length(items.imageData), 0) <= {MAX_IMAGE_BYTES}
	AND NOT EXISTS (
		SELECT 1 FROM oneDriveExcludedPayloads
		WHERE oneDriveExcludedPayloads.payloadHash = items.payloadHash
	)
"""


def _seedOneDriveState(connection: sqlite3.Connection) -> None:
	"""Create live synchronization records for existing uploadable local data."""
	historyRows = connection.execute(
		f"""
		SELECT items.dedupKey, items.payloadHash
		FROM history JOIN items USING (itemId)
		WHERE {ONE_DRIVE_ITEM_SQL}
		ORDER BY history.sortOrder DESC
		""",
	)
	for row in historyRows:
		_ = connection.execute(
			_HISTORY_STATE_INSERT_SQL,
			_historyStateRow(
				OneDriveHistoryState(
					dedupKey=cast(bytes, row["dedupKey"]),
					payloadHash=cast(bytes, row["payloadHash"]),
					version=_nextSyncVersion(connection),
					deleted=False,
					dirty=True,
				),
			),
		)
	categoryRows = connection.execute(
		"SELECT name, nameFolded FROM categories ORDER BY sortOrder",
	)
	for row in categoryRows:
		version = _nextSyncVersion(connection)
		_ = connection.execute(
			_CATEGORY_STATE_INSERT_SQL,
			_categoryStateRow(
				OneDriveCategoryState(
					nameFolded=cast(str, row["nameFolded"]),
					name=cast(str, row["name"]),
					orderVersion=version,
					version=version,
					deleted=False,
					dirty=True,
				),
			),
		)
	categoryItemRows = connection.execute(
		f"""
		SELECT categories.nameFolded, items.payloadHash
		FROM categoryItems
		JOIN categories USING (categoryId)
		JOIN items USING (itemId)
		WHERE {ONE_DRIVE_ITEM_SQL}
		ORDER BY categories.sortOrder, categoryItems.sortOrder DESC
		""",
	)
	for row in categoryItemRows:
		_ = connection.execute(
			_CATEGORY_ITEM_STATE_INSERT_SQL,
			_categoryItemStateRow(
				OneDriveCategoryItemState(
					nameFolded=cast(str, row["nameFolded"]),
					payloadHash=cast(bytes, row["payloadHash"]),
					version=_nextSyncVersion(connection),
					deleted=False,
					dirty=True,
				),
			),
		)


def bindOneDriveState(connection: sqlite3.Connection, accountId: str, reset: bool) -> None:
	"""Bind synchronization state to one account, optionally resetting its baseline."""
	row = connection.execute(
		"SELECT accountId FROM oneDriveSync WHERE singleton = 1",
	).fetchone()
	if row is None:
		raise StorageFormatError
	currentAccountId = cast(str | None, row[0])
	if currentAccountId not in (None, accountId) and not reset:
		raise OneDriveAccountMismatchError(currentAccountId, accountId)
	if reset or currentAccountId is None:
		_ = connection.execute("DELETE FROM oneDriveHistory")
		_ = connection.execute("DELETE FROM oneDriveCategories")
		_ = connection.execute("DELETE FROM oneDriveCategoryItems")
		_ = connection.execute(
			"""
			UPDATE oneDriveSync
			SET accountId = ?, logicalClock = 0,
				generationClock = 0, generationOperationId = ?
			WHERE singleton = 1
			""",
			(accountId, ZERO_OPERATION_ID),
		)
		_seedOneDriveState(connection)
	else:
		_ = connection.execute(
			"UPDATE oneDriveSync SET accountId = ? WHERE singleton = 1",
			(accountId,),
		)


def _nextSyncVersion(connection: sqlite3.Connection) -> SyncVersion:
	"""Advance the database logical clock and return a unique version."""
	row = connection.execute("SELECT logicalClock FROM oneDriveSync WHERE singleton = 1").fetchone()
	if row is None:
		raise StorageFormatError
	clock = cast(int, row[0]) + 1
	if clock >= _MAX_SYNC_CLOCK:
		raise StorageError("OneDrive logical clock exhausted")
	_ = connection.execute("UPDATE oneDriveSync SET logicalClock = ? WHERE singleton = 1", (clock,))
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


_HISTORY_STATE_INSERT_SQL = """
	INSERT INTO oneDriveHistory (
		dedupKey, payloadHash, clock, operationId, deleted, dirty
	) VALUES (?, ?, ?, ?, ?, ?)
"""


_HISTORY_STATE_UPSERT_SQL = f"""{_HISTORY_STATE_INSERT_SQL}
	ON CONFLICT (dedupKey) DO UPDATE SET
		payloadHash = excluded.payloadHash,
		clock = excluded.clock,
		operationId = excluded.operationId,
		deleted = excluded.deleted,
		dirty = excluded.dirty
"""


_CATEGORY_STATE_INSERT_SQL = """
	INSERT INTO oneDriveCategories (
		nameFolded, name, orderClock, orderOperationId,
		clock, operationId, deleted, dirty
	) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""


_CATEGORY_STATE_UPSERT_SQL = f"""{_CATEGORY_STATE_INSERT_SQL}
	ON CONFLICT (nameFolded) DO UPDATE SET
		name = excluded.name,
		orderClock = excluded.orderClock,
		orderOperationId = excluded.orderOperationId,
		clock = excluded.clock,
		operationId = excluded.operationId,
		deleted = excluded.deleted,
		dirty = excluded.dirty
"""


_CATEGORY_ITEM_STATE_INSERT_SQL = """
	INSERT INTO oneDriveCategoryItems (
		nameFolded, payloadHash, clock, operationId, deleted, dirty
	) VALUES (?, ?, ?, ?, ?, ?)
"""


_CATEGORY_ITEM_STATE_UPSERT_SQL = f"""{_CATEGORY_ITEM_STATE_INSERT_SQL}
	ON CONFLICT (nameFolded, payloadHash) DO UPDATE SET
		clock = excluded.clock,
		operationId = excluded.operationId,
		deleted = excluded.deleted,
		dirty = excluded.dirty
"""


def _historyStateRow(state: OneDriveHistoryState) -> tuple[bytes, bytes | None, int, str, int, int]:
	"""Return SQL parameters for ``_HISTORY_STATE_*_SQL``."""
	return (
		state.dedupKey,
		state.payloadHash,
		state.version.clock,
		state.version.operationId,
		int(state.deleted),
		int(state.dirty),
	)


def _categoryStateRow(state: OneDriveCategoryState) -> tuple[str, str, int, str, int, str, int, int]:
	"""Return SQL parameters for ``_CATEGORY_STATE_*_SQL``."""
	return (
		state.nameFolded,
		state.name,
		state.orderVersion.clock,
		state.orderVersion.operationId,
		state.version.clock,
		state.version.operationId,
		int(state.deleted),
		int(state.dirty),
	)


def _categoryItemStateRow(state: OneDriveCategoryItemState) -> tuple[str, bytes, int, str, int, int]:
	"""Return SQL parameters for ``_CATEGORY_ITEM_STATE_*_SQL``."""
	return (
		state.nameFolded,
		state.payloadHash,
		state.version.clock,
		state.version.operationId,
		int(state.deleted),
		int(state.dirty),
	)


def recordHistoryState(
	connection: sqlite3.Connection,
	dedupKey: bytes,
	payloadHash: bytes | None,
) -> None:
	"""Record a new live version for one history identity, or a deletion when ``payloadHash`` is None."""
	if not _hasBoundOneDriveAccount(connection):
		return
	state = OneDriveHistoryState(
		dedupKey=dedupKey,
		payloadHash=payloadHash,
		version=_nextSyncVersion(connection),
		deleted=payloadHash is None,
		dirty=True,
	)
	_ = connection.execute(_HISTORY_STATE_UPSERT_SQL, _historyStateRow(state))


def categoryOrderVersion(connection: sqlite3.Connection, nameFolded: str) -> SyncVersion | None:
	"""Return the established synchronization order of a category, if it has one."""
	row = connection.execute(
		"SELECT orderClock, orderOperationId FROM oneDriveCategories WHERE nameFolded = ?",
		(nameFolded,),
	).fetchone()
	return None if row is None else SyncVersion(cast(int, row[0]), cast(str, row[1]))


def recordCategoryState(
	connection: sqlite3.Connection,
	nameFolded: str,
	name: str,
	*,
	deleted: bool,
	orderVersion: SyncVersion | None = None,
) -> None:
	"""Record a live or deleted category while retaining its established order by default."""
	if not _hasBoundOneDriveAccount(connection):
		return
	version = _nextSyncVersion(connection)
	if orderVersion is None:
		establishedOrderVersion = categoryOrderVersion(connection, nameFolded)
		orderVersion = version if establishedOrderVersion is None else establishedOrderVersion
	state = OneDriveCategoryState(
		nameFolded=nameFolded,
		name=name,
		orderVersion=orderVersion,
		version=version,
		deleted=deleted,
		dirty=True,
	)
	_ = connection.execute(_CATEGORY_STATE_UPSERT_SQL, _categoryStateRow(state))


def recordCategoryItemState(
	connection: sqlite3.Connection,
	nameFolded: str,
	payloadHash: bytes,
	*,
	deleted: bool,
) -> None:
	"""Record a live or deleted category membership."""
	if not _hasBoundOneDriveAccount(connection):
		return
	state = OneDriveCategoryItemState(
		nameFolded=nameFolded,
		payloadHash=payloadHash,
		version=_nextSyncVersion(connection),
		deleted=deleted,
		dirty=True,
	)
	_ = connection.execute(_CATEGORY_ITEM_STATE_UPSERT_SQL, _categoryItemStateRow(state))


def recordHistoryItem(connection: sqlite3.Connection, itemId: int) -> None:
	"""Record the current history item, removing any previously uploadable replacement."""
	row = connection.execute(
		f"""
		SELECT items.dedupKey, items.payloadHash,
			CASE WHEN {ONE_DRIVE_ITEM_SQL} THEN 1 ELSE 0 END AS isOneDrive
		FROM items WHERE itemId = ?
		""",
		(itemId,),
	).fetchone()
	if row is None:
		raise ItemNotFoundError(itemId)
	dedupKey = cast(bytes, row["dedupKey"])
	if bool(cast(int, row["isOneDrive"])):
		recordHistoryState(connection, dedupKey, cast(bytes, row["payloadHash"]))
		return
	stateRow = connection.execute(
		"SELECT deleted FROM oneDriveHistory WHERE dedupKey = ?",
		(dedupKey,),
	).fetchone()
	if stateRow is not None and not bool(cast(int, stateRow[0])):
		recordHistoryState(connection, dedupKey, None)


def recordHistoryRemoval(
	connection: sqlite3.Connection,
	dedupKey: bytes,
	payloadHash: bytes,
	*,
	isUploadable: bool,
	explicit: bool,
) -> None:
	"""Record a deleted history reference, or discard the live state of an internally evicted one."""
	if explicit:
		stateRow = connection.execute(
			"SELECT deleted FROM oneDriveHistory WHERE dedupKey = ?",
			(dedupKey,),
		).fetchone()
		if isUploadable or (stateRow is not None and not bool(cast(int, stateRow[0]))):
			recordHistoryState(connection, dedupKey, None)
	else:
		_ = connection.execute(
			"""
			DELETE FROM oneDriveHistory
			WHERE dedupKey = ? AND payloadHash = ? AND deleted = 0
			""",
			(dedupKey, payloadHash),
		)


def recordCategoryItem(
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
			CASE WHEN {ONE_DRIVE_ITEM_SQL} THEN 1 ELSE 0 END AS isOneDrive
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
			recordCategoryItemState(connection, nameFolded, payloadHash, deleted=True)
		return
	recordCategoryItemState(
		connection,
		nameFolded,
		payloadHash,
		deleted=deleted,
	)


def reviveCategoryIfNeeded(connection: sqlite3.Connection, categoryId: int) -> str:
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
	recordCategoryState(connection, nameFolded, cast(str, row["name"]), deleted=False)
	return nameFolded


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


def validateSyncHash(value: bytes) -> None:
	"""Validate a SHA-256 identity received from synchronization data."""
	if not isinstance(value, bytes) or len(value) != sha256().digest_size:
		raise StorageFormatError


def validateOneDriveSnapshot(
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
		validateSyncHash(state.dedupKey)
		_validateSyncVersion(state.version)
		if type(state.deleted) is not bool or type(state.dirty) is not bool or state.dedupKey in historyKeys:
			raise StorageFormatError
		if state.deleted:
			if state.payloadHash is not None:
				raise StorageFormatError
		elif state.payloadHash is None:
			raise StorageFormatError
		else:
			validateSyncHash(state.payloadHash)
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
		validateSyncHash(state.payloadHash)
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


def _versionFromRow(row: sqlite3.Row) -> SyncVersion:
	"""Return the primary synchronization version stored in a state row."""
	return SyncVersion(cast(int, row["clock"]), cast(str, row["operationId"]))


def mergeHistoryStates(
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
				_ = connection.execute(
					"UPDATE oneDriveHistory SET dirty = ? WHERE dedupKey = ?",
					(int(state.dirty), state.dedupKey),
				)
			continue
		_ = connection.execute(_HISTORY_STATE_UPSERT_SQL, _historyStateRow(state))
		hasChanged = True
	return hasChanged


def mergeCategoryStates(
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
					_ = connection.execute(
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
					_ = connection.execute(
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
		_ = connection.execute(
			_CATEGORY_STATE_UPSERT_SQL,
			_categoryStateRow(replace(state, orderVersion=orderVersion)),
		)
		hasChanged = True
	return hasChanged


def mergeCategoryItemStates(
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
				_ = connection.execute(
					"""
					UPDATE oneDriveCategoryItems SET dirty = ?
					WHERE nameFolded = ? AND payloadHash = ?
					""",
					(int(state.dirty), state.nameFolded, state.payloadHash),
				)
			continue
		_ = connection.execute(_CATEGORY_ITEM_STATE_UPSERT_SQL, _categoryItemStateRow(state))
		hasChanged = True
	return hasChanged


def observeOneDriveClock(connection: sqlite3.Connection, snapshot: OneDriveSyncSnapshot) -> None:
	"""Advance the local logical clock past every version observed remotely."""
	clocks = [snapshot.generation.clock]
	clocks.extend(state.version.clock for state in snapshot.history)
	clocks.extend(state.version.clock for state in snapshot.categories)
	clocks.extend(state.orderVersion.clock for state in snapshot.categories)
	clocks.extend(state.version.clock for state in snapshot.categoryItems)
	if not clocks:
		return
	_ = connection.execute(
		"""
		UPDATE oneDriveSync SET logicalClock = MAX(logicalClock, ?)
		WHERE singleton = 1
		""",
		(max(clocks),),
	)


def oneDriveSnapshotFromConnection(connection: sqlite3.Connection) -> OneDriveSyncSnapshot:
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


def hasDirtyOneDriveState(connection: sqlite3.Connection) -> bool:
	"""Return whether synchronization metadata contains a dirty local change."""
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


def validateOneDriveVersionReservation(
	expectedSnapshot: OneDriveSyncSnapshot,
	minimumClock: int,
	count: int,
) -> None:
	"""Validate untrusted version reservation arguments before any transaction starts."""
	validateOneDriveSnapshot(expectedSnapshot)
	if (
		type(minimumClock) is not int
		or not 0 <= minimumClock < _MAX_SYNC_CLOCK - 1
		or type(count) is not int
		or not 0 <= count <= 100_000
	):
		raise ValueError(minimumClock, count)


def allocateOneDriveVersions(
	connection: sqlite3.Connection,
	expectedSnapshot: OneDriveSyncSnapshot,
	minimumClock: int,
	count: int,
) -> tuple[SyncVersion, ...]:
	"""Reserve versions unless synchronization entities changed since a snapshot."""
	if oneDriveSnapshotFromConnection(connection) != expectedSnapshot:
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
		SyncVersion(clock, str(uuid4())) for clock in range(logicalClock + 1, logicalClock + count + 1)
	)
	_ = connection.execute(
		"UPDATE oneDriveSync SET logicalClock = ? WHERE singleton = 1",
		(logicalClock + count,),
	)
	return versions


def replaceOneDriveSnapshot(
	connection: sqlite3.Connection,
	snapshot: OneDriveSyncSnapshot,
) -> None:
	"""Replace metadata after a newer compacted synchronization generation wins."""
	_ = connection.execute("DELETE FROM oneDriveHistory")
	_ = connection.execute("DELETE FROM oneDriveCategories")
	_ = connection.execute("DELETE FROM oneDriveCategoryItems")
	_ = connection.executemany(_HISTORY_STATE_INSERT_SQL, map(_historyStateRow, snapshot.history))
	_ = connection.executemany(_CATEGORY_STATE_INSERT_SQL, map(_categoryStateRow, snapshot.categories))
	_ = connection.executemany(
		_CATEGORY_ITEM_STATE_INSERT_SQL,
		map(_categoryItemStateRow, snapshot.categoryItems),
	)
	_ = connection.execute(
		"""
		UPDATE oneDriveSync SET generationClock = ?, generationOperationId = ?
		WHERE singleton = 1
		""",
		(snapshot.generation.clock, snapshot.generation.operationId),
	)
	observeOneDriveClock(connection, snapshot)


def replayConcurrentOneDriveChanges(
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
		recordHistoryState(connection, state.dedupKey, None if state.deleted else state.payloadHash)
	for state in sorted(categories.values(), key=lambda value: value.orderVersion):
		if state.deleted:
			recordCategoryState(connection, state.nameFolded, state.name, deleted=True)
		else:
			committedCategory = committedCategories.get(state.nameFolded)
			recordCategoryState(
				connection,
				state.nameFolded,
				state.name,
				deleted=False,
				orderVersion=(
					committedCategory.orderVersion
					if committedCategory is not None
					else renamedOrderVersions.get(state.orderVersion)
				),
			)
	for state in sorted(categoryItems, key=lambda value: value.version):
		recordCategoryItemState(
			connection,
			state.nameFolded,
			state.payloadHash,
			deleted=state.deleted,
		)
	return bool(history or categories or categoryItems)


def tombstoneIneligibleOneDriveItems(connection: sqlite3.Connection) -> bool:
	"""Replace live cloud state for locally available ineligible items with tombstones."""
	historyRows = connection.execute(
		f"""
		SELECT DISTINCT oneDriveHistory.dedupKey
		FROM oneDriveHistory
		JOIN items ON items.payloadHash = oneDriveHistory.payloadHash
		WHERE oneDriveHistory.deleted = 0 AND NOT ({ONE_DRIVE_ITEM_SQL})
		ORDER BY oneDriveHistory.dedupKey
		""",
	).fetchall()
	categoryItemRows = connection.execute(
		f"""
		SELECT DISTINCT oneDriveCategoryItems.nameFolded, oneDriveCategoryItems.payloadHash
		FROM oneDriveCategoryItems
		JOIN items ON items.payloadHash = oneDriveCategoryItems.payloadHash
		WHERE oneDriveCategoryItems.deleted = 0 AND NOT ({ONE_DRIVE_ITEM_SQL})
		ORDER BY oneDriveCategoryItems.nameFolded, oneDriveCategoryItems.payloadHash
		""",
	).fetchall()
	for row in historyRows:
		recordHistoryState(connection, cast(bytes, row["dedupKey"]), None)
	for row in categoryItemRows:
		recordCategoryItemState(
			connection,
			cast(str, row["nameFolded"]),
			cast(bytes, row["payloadHash"]),
			deleted=True,
		)
	return bool(historyRows or categoryItemRows)


def tombstoneOneDrivePayload(connection: sqlite3.Connection, payloadHash: bytes) -> bool:
	"""Keep one local payload while replacing its live cloud references with tombstones.

	Return whether the exclusion list or any synchronization state changed.
	"""
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
		recordHistoryState(connection, cast(bytes, row["dedupKey"]), None)
	for row in categoryRows:
		recordCategoryItemState(
			connection,
			cast(str, row["nameFolded"]),
			payloadHash,
			deleted=True,
		)
	return exclusionCursor.rowcount > 0 or bool(historyRows or categoryRows)


def removeOrphanedOneDriveExclusions(connection: sqlite3.Connection) -> None:
	"""Forget synchronization exclusions whose payloads are no longer stored locally."""
	_ = connection.execute(
		"""
		DELETE FROM oneDriveExcludedPayloads
		WHERE NOT EXISTS (
			SELECT 1 FROM items
			WHERE items.payloadHash = oneDriveExcludedPayloads.payloadHash
		)
		""",
	)


def requiredOneDrivePayloadHashes(connection: sqlite3.Connection) -> set[bytes]:
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


def reconcileOneDriveCategories(connection: sqlite3.Connection) -> None:
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
				_ = connection.execute(
					"INSERT INTO categories (name, nameFolded, sortOrder) VALUES (?, ?, ?)",
					(cast(str, row["name"]), nameFolded, sortOrder),
				)
			else:
				_ = connection.execute(
					"UPDATE categories SET name = ? WHERE categoryId = ?",
					(cast(str, row["name"]), cast(int, categoryRow[0])),
				)
			continue
		if categoryRow is None:
			continue
		categoryId = cast(int, categoryRow[0])
		_ = connection.execute(
			f"""
			DELETE FROM categoryItems WHERE categoryId = ? AND itemId IN (
				SELECT itemId FROM items WHERE {ONE_DRIVE_ITEM_SQL}
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
			_ = connection.execute("DELETE FROM categories WHERE categoryId = ?", (categoryId,))
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
	_ = connection.executemany(
		"UPDATE categories SET sortOrder = ? WHERE categoryId = ?",
		((sortOrder, categoryId) for sortOrder, categoryId in enumerate(orderedIds)),
	)


def removeCompactedOneDriveCategories(
	connection: sqlite3.Connection,
	expected: OneDriveSyncSnapshot,
	committed: OneDriveSyncSnapshot,
) -> None:
	"""Remove synchronized content from categories omitted by a newer generation."""
	committedKeys = {state.nameFolded for state in committed.categories}
	obsoleteStates = tuple(state for state in expected.categories if state.nameFolded not in committedKeys)
	if not obsoleteStates:
		return
	_ = connection.executemany(
		"""
		UPDATE oneDriveCategories SET deleted = 1
		WHERE nameFolded = ? AND clock = ? AND operationId = ?
		""",
		((state.nameFolded, state.version.clock, state.version.operationId) for state in obsoleteStates),
	)
	reconcileOneDriveCategories(connection)


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


def reconcileOneDriveHistory(
	connection: sqlite3.Connection,
	payloadItemIds: Mapping[bytes, int],
) -> None:
	"""Rebuild uploadable history references from winning synchronization state."""
	existingRows = connection.execute(
		f"""
		SELECT history.itemId, CASE WHEN {ONE_DRIVE_ITEM_SQL} THEN 1 ELSE 0 END AS isOneDrive
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
	_ = connection.execute("DELETE FROM history")
	_ = connection.executemany(
		"INSERT INTO history (itemId, sortOrder) VALUES (?, ?)",
		((itemId, sortOrder) for sortOrder, itemId in enumerate(dict.fromkeys(orderedIds))),
	)


def reconcileOneDriveCategoryItems(
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
				CASE WHEN {ONE_DRIVE_ITEM_SQL} THEN 1 ELSE 0 END AS isOneDrive
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
		_ = connection.execute("DELETE FROM categoryItems WHERE categoryId = ?", (categoryId,))
		_ = connection.executemany(
			"INSERT INTO categoryItems (categoryId, itemId, sortOrder) VALUES (?, ?, ?)",
			((categoryId, itemId, sortOrder) for sortOrder, itemId in enumerate(dict.fromkeys(orderedIds))),
		)
