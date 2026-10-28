# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Define immutable clipboard storage records and errors without loading SQLite."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ClipboardItemType(StrEnum):
	"""Fixed clipboard entry types supported by storage."""

	PLAIN_TEXT = "plainText"
	FORMATTED_TEXT = "formattedText"
	IMAGE = "image"
	TEXT_AND_IMAGE = "textAndImage"
	FILES = "files"


# Stored entry types that carry navigable clipboard text.
TEXT_ITEM_TYPES = frozenset(
	{ClipboardItemType.PLAIN_TEXT, ClipboardItemType.FORMATTED_TEXT, ClipboardItemType.TEXT_AND_IMAGE},
)


@dataclass(frozen=True, slots=True)
class ClipboardItem:
	"""A complete immutable clipboard entry with image payloads normalized to PNG."""

	contentType: ClipboardItemType
	text: str = ""
	html: bytes | None = None
	rtf: bytes | None = None
	imageData: bytes | None = None
	imageWidth: int | None = None
	imageHeight: int | None = None
	imageBitDepth: int | None = None
	files: tuple[str, ...] = ()
	canUpload: bool = True


@dataclass(frozen=True, slots=True)
class ClipboardItemSummary:
	"""Lightweight entry metadata returned by history and category listings."""

	itemId: int
	contentType: ClipboardItemType
	textPreview: str
	imageByteCount: int
	imageWidth: int | None
	imageHeight: int | None
	fileCount: int
	filesPreview: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClipboardItemContent:
	"""Textual content and image metadata without large binary payloads."""

	itemId: int
	contentType: ClipboardItemType
	text: str
	imageByteCount: int
	imageWidth: int | None
	imageHeight: int | None
	imageBitDepth: int | None
	files: tuple[str, ...]
	canUpload: bool

	@property
	def hasImage(self) -> bool:
		"""Return whether this entry contains stored image data."""
		return self.imageByteCount > 0


@dataclass(frozen=True, order=True, slots=True)
class SyncVersion:
	"""A logical clock value with a globally unique tie breaker."""

	clock: int
	operationId: str


ZERO_OPERATION_ID = "00000000-0000-0000-0000-000000000000"
_ZERO_SYNC_VERSION = SyncVersion(0, ZERO_OPERATION_ID)


@dataclass(frozen=True, slots=True)
class OneDriveHistoryState:
	"""The synchronized state of one history identity."""

	dedupKey: bytes
	payloadHash: bytes | None
	version: SyncVersion
	deleted: bool
	dirty: bool = False


@dataclass(frozen=True, slots=True)
class OneDriveCategoryState:
	"""The synchronized state and position of one category identity."""

	nameFolded: str
	name: str
	orderVersion: SyncVersion
	version: SyncVersion
	deleted: bool
	dirty: bool = False


@dataclass(frozen=True, slots=True)
class OneDriveCategoryItemState:
	"""The synchronized membership state of one category payload."""

	nameFolded: str
	payloadHash: bytes
	version: SyncVersion
	deleted: bool
	dirty: bool = False


@dataclass(frozen=True, slots=True)
class OneDriveSyncSnapshot:
	"""A payload-free snapshot suitable for deterministic cloud merging."""

	accountId: str | None
	history: tuple[OneDriveHistoryState, ...]
	categories: tuple[OneDriveCategoryState, ...]
	categoryItems: tuple[OneDriveCategoryItemState, ...]
	generation: SyncVersion = _ZERO_SYNC_VERSION


class StorageError(Exception):
	"""Base error for clipboard storage failures."""


class StorageFormatError(StorageError):
	"""Raised when persisted data does not have the expected structure."""


class UnsupportedSchemaVersionError(StorageError):
	"""Raised when a database uses an unsupported schema version."""


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
