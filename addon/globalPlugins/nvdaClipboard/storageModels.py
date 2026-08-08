# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Define immutable clipboard storage records without loading SQLite."""

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


_ZERO_OPERATION_ID = "00000000-0000-0000-0000-000000000000"
_ZERO_SYNC_VERSION = SyncVersion(0, _ZERO_OPERATION_ID)


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
