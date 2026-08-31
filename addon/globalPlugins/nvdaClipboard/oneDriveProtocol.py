# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Encode, validate, and merge OneDrive synchronization protocol data."""

from __future__ import annotations

from base64 import b64decode, b64encode
from collections.abc import Mapping
from dataclasses import replace
import json
import re
from typing import Any, cast
from uuid import UUID

import addonHandler

from .clipboardData import getPngImageInfo, isImageSizeSafe
from .imageCodec import isPngImageDecodable
from .storage import (
	MAX_HISTORY_ITEMS,
	MAX_IMAGE_HISTORY_ITEMS,
	MAX_TOTAL_IMAGE_BYTES,
	StorageError,
	getItemPayloadHash,
	isOneDriveSyncItem,
)

from .storageModels import (
	ClipboardItem,
	ClipboardItemType,
	OneDriveCategoryItemState,
	OneDriveCategoryState,
	OneDriveHistoryState,
	OneDriveSyncSnapshot,
	SyncVersion,
)


addonHandler.initTranslation()


MANIFEST_SCHEMA_VERSION = 2
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
ITEM_SCHEMA_VERSION = 1
MAX_ITEM_FILE_BYTES = 96 * 1024 * 1024
_MAX_SYNC_CLOCK = (1 << 63) - 1

# Translators: Error shown when OneDrive synchronization data fails validation.
_INVALID_SYNC_DATA_MESSAGE = _("OneDrive synchronization data is invalid")


class OneDriveError(RuntimeError):
	"""Report a safe OneDrive authentication, transport, or data error."""

	def __init__(
		self,
		message: str,
		*,
		statusCode: int | None = None,
		retryAfter: int | None = None,
	) -> None:
		"""Create an error with optional HTTP retry metadata."""
		super().__init__(message)
		self.statusCode = statusCode
		self.retryAfter = retryAfter


class _ManifestSizeLimitError(OneDriveError):
	"""Report that synchronization metadata exceeds its bounded cloud representation."""


class _ItemSizeLimitError(OneDriveError):
	"""Report that one local item cannot fit in the bounded cloud representation."""


def _emptySnapshot(accountId: str | None = None) -> OneDriveSyncSnapshot:
	"""Return an empty synchronization snapshot."""
	return OneDriveSyncSnapshot(
		accountId=accountId,
		history=(),
		categories=(),
		categoryItems=(),
	)


def _versionToJson(version: SyncVersion) -> dict[str, object]:
	"""Encode a synchronization version for JSON."""
	return {"clock": version.clock, "operationId": version.operationId}


def _versionFromJson(value: object) -> SyncVersion:
	"""Decode a synchronization version while retaining a local successor."""
	if not isinstance(value, Mapping):
		raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
	clock = value.get("clock")
	operationId = value.get("operationId")
	if (
		not isinstance(clock, int)
		or isinstance(clock, bool)
		or not 0 <= clock < _MAX_SYNC_CLOCK - 1
		or not isinstance(operationId, str)
	):
		raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
	try:
		parsedOperationId = UUID(operationId)
	except ValueError as error:
		raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE) from error
	if str(parsedOperationId) != operationId:
		raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
	return SyncVersion(clock=clock, operationId=operationId)


def _hashFromJson(value: object, *, optional: bool = False) -> bytes | None:
	"""Decode a lowercase SHA-256 hex value from JSON."""
	if value is None and optional:
		return None
	if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
		raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
	return bytes.fromhex(value)


def _encodeManifest(snapshot: OneDriveSyncSnapshot) -> bytes:
	"""Encode a local or merged synchronization snapshot as bounded JSON."""
	payload = {
		"schemaVersion": MANIFEST_SCHEMA_VERSION,
		"generation": _versionToJson(snapshot.generation),
		"history": [
			{
				"key": state.dedupKey.hex(),
				"payload": state.payloadHash.hex() if state.payloadHash is not None else None,
				"deleted": state.deleted,
				"version": _versionToJson(state.version),
			}
			for state in snapshot.history
		],
		"categories": [
			{
				"key": state.nameFolded,
				"name": state.name,
				"deleted": state.deleted,
				"orderVersion": _versionToJson(state.orderVersion),
				"version": _versionToJson(state.version),
			}
			for state in snapshot.categories
		],
		"categoryItems": [
			{
				"categoryKey": state.nameFolded,
				"payload": state.payloadHash.hex(),
				"deleted": state.deleted,
				"version": _versionToJson(state.version),
			}
			for state in snapshot.categoryItems
		],
	}
	data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
	if len(data) > MAX_MANIFEST_BYTES:
		raise _ManifestSizeLimitError(
			# Translators: Error shown when the combined history/category manifest is too large.
			_("The OneDrive synchronization index exceeds the supported size"),
		)
	return data


def _decodeManifest(data: bytes) -> OneDriveSyncSnapshot:
	"""Decode and strictly validate a downloaded synchronization manifest."""
	try:
		payload = json.loads(data.decode("utf-8"))
	except (ValueError, UnicodeError, RecursionError) as error:
		raise OneDriveError(
			# Translators: Error shown when a OneDrive synchronization file is malformed.
			_("The OneDrive synchronization index is not valid JSON"),
		) from error
	schemaVersion = payload.get("schemaVersion") if isinstance(payload, Mapping) else None
	if type(schemaVersion) is not int or schemaVersion != MANIFEST_SCHEMA_VERSION:
		raise OneDriveError(
			# Translators: Error shown for an unknown OneDrive synchronization schema.
			_("The OneDrive synchronization index uses an unsupported version"),
		)
	generation = _versionFromJson(payload.get("generation"))
	historyValues = _manifestList(payload, "history")
	categoryValues = _manifestList(payload, "categories")
	categoryItemValues = _manifestList(payload, "categoryItems")
	history: list[OneDriveHistoryState] = []
	historyKeys: set[bytes] = set()
	for value in historyValues:
		if not isinstance(value, Mapping):
			raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
		key = cast(bytes, _hashFromJson(value.get("key")))
		deleted = value.get("deleted")
		payloadHash = _hashFromJson(value.get("payload"), optional=True)
		if not isinstance(deleted, bool) or deleted == (payloadHash is not None) or key in historyKeys:
			raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
		historyKeys.add(key)
		history.append(
			OneDriveHistoryState(
				dedupKey=key,
				payloadHash=payloadHash,
				version=_versionFromJson(value.get("version")),
				deleted=deleted,
			),
		)
	categories: list[OneDriveCategoryState] = []
	categoryKeys: set[str] = set()
	for value in categoryValues:
		if not isinstance(value, Mapping):
			raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
		nameFolded = value.get("key")
		name = value.get("name")
		deleted = value.get("deleted")
		if (
			not isinstance(nameFolded, str)
			or not nameFolded
			or not isinstance(name, str)
			or not name.strip()
			or name.strip().casefold() != nameFolded
			or not isinstance(deleted, bool)
			or nameFolded in categoryKeys
		):
			raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
		try:
			nameFolded.encode("utf-8")
			name.strip().encode("utf-8")
		except UnicodeError as error:
			raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE) from error
		categoryKeys.add(nameFolded)
		categories.append(
			OneDriveCategoryState(
				nameFolded=nameFolded,
				name=name.strip(),
				orderVersion=_versionFromJson(value.get("orderVersion")),
				version=_versionFromJson(value.get("version")),
				deleted=deleted,
			),
		)
	categoryItems: list[OneDriveCategoryItemState] = []
	categoryItemKeys: set[tuple[str, bytes]] = set()
	for value in categoryItemValues:
		if not isinstance(value, Mapping):
			raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
		nameFolded = value.get("categoryKey")
		payloadHash = cast(bytes, _hashFromJson(value.get("payload")))
		deleted = value.get("deleted")
		key = (nameFolded, payloadHash)
		if (
			not isinstance(nameFolded, str)
			or nameFolded not in categoryKeys
			or not isinstance(deleted, bool)
			or key in categoryItemKeys
		):
			raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
		categoryItemKeys.add(cast(tuple[str, bytes], key))
		categoryItems.append(
			OneDriveCategoryItemState(
				nameFolded=nameFolded,
				payloadHash=payloadHash,
				version=_versionFromJson(value.get("version")),
				deleted=deleted,
			),
		)
	return OneDriveSyncSnapshot(
		accountId=None,
		history=tuple(history),
		categories=tuple(categories),
		categoryItems=tuple(categoryItems),
		generation=generation,
	)


def _manifestList(payload: Mapping[str, object], name: str) -> list[object]:
	"""Return one required manifest array with a defensive record limit."""
	value = payload.get(name)
	if not isinstance(value, list) or len(value) > 100_000:
		raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
	return cast(list[object], value)


def _bytesToJson(data: bytes | None) -> str | None:
	"""Encode optional binary item data for JSON."""
	return None if data is None else b64encode(data).decode("ascii")


def _bytesFromJson(value: object) -> bytes | None:
	"""Decode optional strict Base64 item data from JSON."""
	if value is None:
		return None
	if not isinstance(value, str):
		raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
	try:
		return b64decode(value, validate=True)
	except ValueError as error:
		raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE) from error


def _encodeItem(item: ClipboardItem) -> bytes:
	"""Encode one permitted immutable clipboard item as bounded JSON."""
	_validateSyncItem(item)
	payload = {
		"schemaVersion": ITEM_SCHEMA_VERSION,
		"contentType": item.contentType.value,
		"text": item.text,
		"html": _bytesToJson(item.html),
		"rtf": _bytesToJson(item.rtf),
		"image": _bytesToJson(item.imageData),
		"imageWidth": item.imageWidth,
		"imageHeight": item.imageHeight,
		"imageBitDepth": item.imageBitDepth,
	}
	data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
	if len(data) > MAX_ITEM_FILE_BYTES:
		raise _ItemSizeLimitError(
			# Translators: Error shown when one history record cannot fit in the cloud format.
			_("A clipboard history item exceeds the supported OneDrive size"),
		)
	return data


def _decodeItem(data: bytes, expectedHash: bytes) -> ClipboardItem:
	"""Decode one downloaded item and verify its content-addressed hash."""
	try:
		payload = json.loads(data.decode("utf-8"))
	except (ValueError, UnicodeError, RecursionError) as error:
		raise OneDriveError(
			# Translators: Error shown when a OneDrive history item is malformed.
			_("A OneDrive clipboard history item is not valid JSON"),
		) from error
	if not isinstance(payload, Mapping) or payload.get("schemaVersion") != ITEM_SCHEMA_VERSION:
		raise OneDriveError(
			# Translators: Error shown for an unknown OneDrive history-item schema.
			_("A OneDrive clipboard history item uses an unsupported version"),
		)
	try:
		contentType = ClipboardItemType(payload.get("contentType"))
	except (TypeError, ValueError) as error:
		raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE) from error
	text = payload.get("text")
	if not isinstance(text, str):
		raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
	metadata = tuple(payload.get(name) for name in ("imageWidth", "imageHeight", "imageBitDepth"))
	if any(
		value is not None and (not isinstance(value, int) or isinstance(value, bool)) for value in metadata
	):
		raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
	item = ClipboardItem(
		contentType=contentType,
		text=text,
		html=_bytesFromJson(payload.get("html")),
		rtf=_bytesFromJson(payload.get("rtf")),
		imageData=_bytesFromJson(payload.get("image")),
		imageWidth=cast(int | None, metadata[0]),
		imageHeight=cast(int | None, metadata[1]),
		imageBitDepth=cast(int | None, metadata[2]),
		canUpload=True,
	)
	if _validateSyncItem(item) != expectedHash:
		raise OneDriveError(
			# Translators: Error shown when cloud item content does not match its manifest hash.
			_("A OneDrive clipboard history item failed its integrity check"),
		)
	return item


def _validateSyncItem(item: ClipboardItem) -> bytes:
	"""Validate one cloud item and return its payload hash."""
	try:
		isSyncItem = isOneDriveSyncItem(item)
		if not isSyncItem or item.files:
			raise OneDriveError(
				# Translators: Error shown when a prohibited clipboard item reaches cloud synchronization.
				_("A clipboard history item is not permitted for OneDrive synchronization"),
			)
		if item.imageData is not None:
			info = getPngImageInfo(item.imageData)
			if (
				info is None
				or info != (item.imageWidth, item.imageHeight, item.imageBitDepth)
				or not isImageSizeSafe(info[0], info[1], max(32, info[2]))
				or not isPngImageDecodable(item.imageData, info)
			):
				raise OneDriveError(
					# Translators: Error shown when a cloud image fails PNG validation.
					_("A OneDrive clipboard image is invalid"),
				)
		# The storage hash helper validates the fixed content-type field combinations.
		return getItemPayloadHash(item)
	except (StorageError, UnicodeError) as error:
		raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE) from error


def _stateVersion(state: object) -> SyncVersion:
	"""Return a typed version from one synchronization state record."""
	return cast(SyncVersion, getattr(state, "version"))


def _cleanSnapshot(snapshot: OneDriveSyncSnapshot) -> OneDriveSyncSnapshot:
	"""Remove local-only dirty markers from a proposed committed snapshot."""
	return replace(
		snapshot,
		history=tuple(replace(state, dirty=False) for state in snapshot.history),
		categories=tuple(replace(state, dirty=False) for state in snapshot.categories),
		categoryItems=tuple(replace(state, dirty=False) for state in snapshot.categoryItems),
	)


def _cloudSnapshot(snapshot: OneDriveSyncSnapshot) -> OneDriveSyncSnapshot:
	"""Return the canonical payload represented by a cloud manifest."""
	return replace(_cleanSnapshot(snapshot), accountId=None)


def _preferNewer(left: Any, right: Any) -> Any:
	"""Choose the deterministically newer state record."""
	leftKey = (_stateVersion(left).clock, _stateVersion(left).operationId)
	rightKey = (_stateVersion(right).clock, _stateVersion(right).operationId)
	if leftKey != rightKey:
		return replace(left if leftKey > rightKey else right, dirty=False)
	if isinstance(left, OneDriveCategoryState) and isinstance(right, OneDriveCategoryState):
		isConsistent = replace(left, orderVersion=right.orderVersion, dirty=right.dirty) == right
	else:
		isConsistent = replace(left, dirty=right.dirty) == right
	if not isConsistent:
		raise OneDriveError(_INVALID_SYNC_DATA_MESSAGE)
	return replace(left, dirty=False)


def _generationRebaseStates(
	local: OneDriveSyncSnapshot,
	remote: OneDriveSyncSnapshot,
) -> tuple[
	tuple[OneDriveHistoryState, ...],
	tuple[OneDriveCategoryState, ...],
	tuple[OneDriveCategoryItemState, ...],
]:
	"""Return dirty old-generation states that still change the compacted baseline."""
	remoteHistory = {state.dedupKey: state for state in remote.history}
	history = tuple(
		sorted(
			(
				state
				for state in local.history
				if state.dirty and (not state.deleted or state.dedupKey in remoteHistory)
			),
			key=lambda state: state.version,
		),
	)
	remoteCategories = {state.nameFolded: state for state in remote.categories}
	categories = {
		state.nameFolded: state
		for state in local.categories
		if state.dirty and (not state.deleted or state.nameFolded in remoteCategories)
	}
	remoteCategoryItems = {(state.nameFolded, state.payloadHash): state for state in remote.categoryItems}
	categoryItems = tuple(
		sorted(
			(
				state
				for state in local.categoryItems
				if state.dirty
				and (not state.deleted or (state.nameFolded, state.payloadHash) in remoteCategoryItems)
			),
			key=lambda state: state.version,
		),
	)
	localCategories = {state.nameFolded: state for state in local.categories}
	for state in categoryItems:
		remoteCategory = remoteCategories.get(state.nameFolded)
		if state.deleted or (remoteCategory is not None and not remoteCategory.deleted):
			continue
		category = localCategories.get(state.nameFolded)
		if category is not None and not category.deleted:
			categories[state.nameFolded] = category
	return (
		history,
		tuple(sorted(categories.values(), key=lambda state: state.orderVersion)),
		categoryItems,
	)


def _rebaseLocalSnapshot(
	local: OneDriveSyncSnapshot,
	remote: OneDriveSyncSnapshot,
	states: tuple[
		tuple[OneDriveHistoryState, ...],
		tuple[OneDriveCategoryState, ...],
		tuple[OneDriveCategoryItemState, ...],
	],
	versions: tuple[SyncVersion, ...],
) -> OneDriveSyncSnapshot:
	"""Assign current-generation versions to selected dirty local states."""
	history, categories, categoryItems = states
	if len(versions) != len(history) + len(categories) + len(categoryItems):
		raise ValueError(versions)
	versionIterator = iter(versions)
	rebasedHistory = tuple(replace(state, version=next(versionIterator), dirty=False) for state in history)
	remoteCategories = {state.nameFolded: state for state in remote.categories}
	renamedOrderVersions = {
		state.orderVersion: state.orderVersion for state in local.categories if state.deleted
	}
	renamedOrderVersions.update(
		{
			state.orderVersion: remoteCategories[state.nameFolded].orderVersion
			for state in local.categories
			if state.deleted and state.nameFolded in remoteCategories
		},
	)
	rebasedCategories: list[OneDriveCategoryState] = []
	for state in categories:
		version = next(versionIterator)
		remoteCategory = remoteCategories.get(state.nameFolded)
		rebasedCategories.append(
			replace(
				state,
				orderVersion=(
					remoteCategory.orderVersion
					if remoteCategory is not None
					else renamedOrderVersions.get(state.orderVersion, version)
				),
				version=version,
				dirty=False,
			),
		)
	rebasedCategoryItems = tuple(
		replace(state, version=next(versionIterator), dirty=False) for state in categoryItems
	)
	return OneDriveSyncSnapshot(
		accountId=local.accountId,
		history=rebasedHistory,
		categories=tuple(rebasedCategories),
		categoryItems=rebasedCategoryItems,
		generation=remote.generation,
	)


def _mergeSnapshots(local: OneDriveSyncSnapshot, remote: OneDriveSyncSnapshot) -> OneDriveSyncSnapshot:
	"""Merge independent entity states without dropping either side's newer operation."""
	if local.generation != remote.generation:
		raise ValueError(local.generation, remote.generation)
	history = {state.dedupKey: state for state in remote.history}
	for state in local.history:
		history[state.dedupKey] = (
			_preferNewer(history[state.dedupKey], state) if state.dedupKey in history else state
		)
	categories = {state.nameFolded: state for state in remote.categories}
	for state in local.categories:
		if state.nameFolded in categories:
			winner = _preferNewer(categories[state.nameFolded], state)
			orderVersion = min(
				categories[state.nameFolded].orderVersion,
				state.orderVersion,
				key=lambda value: (value.clock, value.operationId),
			)
			categories[state.nameFolded] = replace(winner, orderVersion=orderVersion)
		else:
			categories[state.nameFolded] = state
	categoryItems = {(state.nameFolded, state.payloadHash): state for state in remote.categoryItems}
	for state in local.categoryItems:
		key = (state.nameFolded, state.payloadHash)
		categoryItems[key] = _preferNewer(categoryItems[key], state) if key in categoryItems else state
	return _cleanSnapshot(
		OneDriveSyncSnapshot(
			accountId=local.accountId,
			history=tuple(sorted(history.values(), key=lambda value: value.dedupKey)),
			categories=tuple(sorted(categories.values(), key=lambda value: value.nameFolded)),
			categoryItems=tuple(
				sorted(categoryItems.values(), key=lambda value: (value.nameFolded, value.payloadHash)),
			),
			generation=local.generation,
		),
	)


def _livePayloadHashes(snapshot: OneDriveSyncSnapshot) -> set[bytes]:
	"""Return payload hashes referenced by live history or live categories."""
	liveCategories = {state.nameFolded for state in snapshot.categories if not state.deleted}
	result = {
		cast(bytes, state.payloadHash)
		for state in snapshot.history
		if not state.deleted and state.payloadHash is not None
	}
	result.update(
		state.payloadHash
		for state in snapshot.categoryItems
		if not state.deleted and state.nameFolded in liveCategories
	)
	return result


def _maximumClock(snapshot: OneDriveSyncSnapshot) -> int:
	"""Return the greatest entity or ordering clock in a synchronization snapshot."""
	clocks = [snapshot.generation.clock]
	clocks.extend(state.version.clock for state in snapshot.history)
	clocks.extend(state.version.clock for state in snapshot.categories)
	clocks.extend(state.orderVersion.clock for state in snapshot.categories)
	clocks.extend(state.version.clock for state in snapshot.categoryItems)
	return max(clocks, default=0)


def _normalizationTargets(
	snapshot: OneDriveSyncSnapshot,
	imageBytes: Mapping[bytes, int],
	retainedImageBytes: Mapping[bytes, int],
) -> tuple[tuple[bytes, ...], tuple[tuple[str, bytes], ...]]:
	"""Return live records that must become synthetic tombstones before cloud commit."""
	live = sorted(
		(state for state in snapshot.history if not state.deleted and state.payloadHash is not None),
		key=lambda state: (state.version.clock, state.version.operationId),
		reverse=True,
	)
	liveCategories = {state.nameFolded for state in snapshot.categories if not state.deleted}
	categoryPayloads = {
		state.payloadHash
		for state in snapshot.categoryItems
		if not state.deleted and state.nameFolded in liveCategories
	}
	fixedImageBytes = dict(retainedImageBytes)
	for payloadHash in categoryPayloads:
		fixedImageBytes.setdefault(payloadHash, imageBytes[payloadHash])
	imageTotal = sum(fixedImageBytes.values())
	if imageTotal > MAX_TOTAL_IMAGE_BYTES:
		# Translators: Error shown when merged OneDrive category images exceed the local limit.
		raise OneDriveError(_("OneDrive image data exceeds the 256 MB storage limit"))
	kept: set[bytes] = set()
	historyImagePayloads: set[bytes] = set()
	imageCount = 0
	for state in live:
		if len(kept) >= MAX_HISTORY_ITEMS:
			break
		payloadHash = cast(bytes, state.payloadHash)
		imageByteCount = imageBytes[payloadHash]
		if imageByteCount:
			if imageCount >= MAX_IMAGE_HISTORY_ITEMS:
				continue
			newImageBytes = (
				0 if payloadHash in fixedImageBytes or payloadHash in historyImagePayloads else imageByteCount
			)
			if imageTotal + newImageBytes > MAX_TOTAL_IMAGE_BYTES:
				continue
			imageCount += 1
			imageTotal += newImageBytes
			historyImagePayloads.add(payloadHash)
		kept.add(state.dedupKey)
	historyKeys = tuple(sorted(state.dedupKey for state in live if state.dedupKey not in kept))
	deletedCategories = {state.nameFolded for state in snapshot.categories if state.deleted}
	categoryItemKeys = tuple(
		sorted(
			(state.nameFolded, state.payloadHash)
			for state in snapshot.categoryItems
			if not state.deleted and state.nameFolded in deletedCategories
		),
	)
	return historyKeys, categoryItemKeys


def _applyNormalization(
	snapshot: OneDriveSyncSnapshot,
	historyKeys: tuple[bytes, ...],
	categoryItemKeys: tuple[tuple[str, bytes], ...],
	versions: tuple[SyncVersion, ...],
) -> OneDriveSyncSnapshot:
	"""Apply reserved versions to all required synthetic tombstones."""
	if len(versions) != len(historyKeys) + len(categoryItemKeys):
		raise ValueError(versions)
	historyVersions = dict(zip(historyKeys, versions[: len(historyKeys)]))
	categoryItemVersions = dict(zip(categoryItemKeys, versions[len(historyKeys) :]))
	history = tuple(
		replace(state, payloadHash=None, version=historyVersions[state.dedupKey], deleted=True)
		if state.dedupKey in historyVersions and not state.deleted
		else state
		for state in snapshot.history
	)
	categoryItems = tuple(
		replace(
			state,
			version=categoryItemVersions[(state.nameFolded, state.payloadHash)],
			deleted=True,
		)
		if (state.nameFolded, state.payloadHash) in categoryItemVersions and not state.deleted
		else state
		for state in snapshot.categoryItems
	)
	return replace(snapshot, history=history, categoryItems=categoryItems)


def _tombstoneCount(snapshot: OneDriveSyncSnapshot) -> int:
	"""Return the number of deleted entity records retained for convergence."""
	return (
		sum(state.deleted for state in snapshot.history)
		+ sum(state.deleted for state in snapshot.categories)
		+ sum(state.deleted for state in snapshot.categoryItems)
	)


def _compactSnapshot(
	snapshot: OneDriveSyncSnapshot,
	generation: SyncVersion,
) -> OneDriveSyncSnapshot:
	"""Start a new generation containing only the current live baseline."""
	liveCategories = {state.nameFolded for state in snapshot.categories if not state.deleted}
	return replace(
		_cleanSnapshot(snapshot),
		history=tuple(state for state in snapshot.history if not state.deleted),
		categories=tuple(state for state in snapshot.categories if not state.deleted),
		categoryItems=tuple(
			state
			for state in snapshot.categoryItems
			if not state.deleted and state.nameFolded in liveCategories
		),
		generation=generation,
	)
