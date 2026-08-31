# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for standalone OneDrive protocol handling."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import struct
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
from uuid import UUID
import zlib

from tests._module_loader import loadAddonModule


_MODULE_DIRECTORY = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard"
_PACKAGE_NAME = "nvdaClipboardOneDriveProtocolTests"
_PACKAGE = ModuleType(_PACKAGE_NAME)
_PACKAGE.__path__ = [str(_MODULE_DIRECTORY)]
sys.modules[_PACKAGE_NAME] = _PACKAGE


def _translate(message: str) -> str:
	"""Return an untranslated message for standalone validation errors."""
	return message


def _initTranslation() -> None:
	"""Install the standalone translator in the importing module globals."""
	sys._getframe(1).f_globals["_"] = _translate


_ADDON_HANDLER = ModuleType("addonHandler")
_ADDON_HANDLER.initTranslation = _initTranslation
_LOG_HANDLER = ModuleType("logHandler")
_LOG_HANDLER.log = Mock()
oneDriveProtocol = loadAddonModule(
	f"{_PACKAGE_NAME}.oneDriveProtocol",
	_MODULE_DIRECTORY / "oneDriveProtocol.py",
	injectedModules={"addonHandler": _ADDON_HANDLER, "logHandler": _LOG_HANDLER},
)
storage = sys.modules[f"{_PACKAGE_NAME}.storage"]


class OneDriveProtocolTests(unittest.TestCase):
	"""Verify OneDrive data handling without loading NVDA."""

	def _makeImageItem(self) -> oneDriveProtocol.ClipboardItem:
		"""Return one valid PNG clipboard item for protocol tests."""
		png = bytearray(b"\x89PNG\r\n\x1a\n")
		for chunkType, payload in (
			(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)),
			(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00")),
			(b"IEND", b""),
		):
			png.extend(struct.pack(">I", len(payload)))
			png.extend(chunkType)
			png.extend(payload)
			png.extend(struct.pack(">I", zlib.crc32(payload, zlib.crc32(chunkType)) & 0xFFFFFFFF))
		return oneDriveProtocol.ClipboardItem(
			oneDriveProtocol.ClipboardItemType.TEXT_AND_IMAGE,
			text="mixed",
			html=b"<b>mixed</b>",
			rtf=b"{\\rtf1 mixed}",
			imageData=bytes(png),
			imageWidth=1,
			imageHeight=1,
			imageBitDepth=32,
		)

	def testItemRoundTripAndValidation(self) -> None:
		"""Round-trip an item and reject malformed or tampered payloads."""
		item = self._makeImageItem()
		payloadHash = storage.getItemPayloadHash(item)
		encoded = oneDriveProtocol._encodeItem(item)
		self.assertEqual(oneDriveProtocol._decodeItem(encoded, payloadHash), item)
		with self.assertRaises(oneDriveProtocol.OneDriveError):
			oneDriveProtocol._decodeItem(encoded, b"0" * 32)
		malformedItem = json.loads(encoded)
		malformedItem["image"] = "!"
		with self.assertRaises(oneDriveProtocol.OneDriveError):
			oneDriveProtocol._decodeItem(json.dumps(malformedItem).encode("utf-8"), payloadHash)

	def testMalformedCloudDataAlwaysRaisesOneDriveError(self) -> None:
		"""Normalize hostile JSON, Base64, and UTF-8 failures at the cloud boundary."""
		itemPayload = json.loads(oneDriveProtocol._encodeItem(self._makeImageItem()))
		version = {"clock": 1, "operationId": str(UUID(int=1))}
		categoryManifest = {
			"schemaVersion": oneDriveProtocol.MANIFEST_SCHEMA_VERSION,
			"generation": version,
			"history": [],
			"categories": [
				{
					"key": "\ud800",
					"name": "\ud800",
					"deleted": False,
					"orderVersion": version,
					"version": version,
				},
			],
			"categoryItems": [],
		}
		hugeInteger = b"9" * 5000
		deeplyNested = b"[" * 5000 + b"0" + b"]" * 5000
		expectedHash = b"\0" * 32
		malformedCases = (
			("manifest huge integer", oneDriveProtocol._decodeManifest, (hugeInteger,)),
			("item huge integer", oneDriveProtocol._decodeItem, (hugeInteger, expectedHash)),
			("manifest deeply nested", oneDriveProtocol._decodeManifest, (deeplyNested,)),
			("item deeply nested", oneDriveProtocol._decodeItem, (deeplyNested, expectedHash)),
			(
				"Base64 surrogate",
				oneDriveProtocol._decodeItem,
				(json.dumps(itemPayload | {"html": "\ud800"}).encode("utf-8"), expectedHash),
			),
			(
				"text surrogate",
				oneDriveProtocol._decodeItem,
				(json.dumps(itemPayload | {"text": "\ud800"}).encode("utf-8"), expectedHash),
			),
			(
				"category surrogate",
				oneDriveProtocol._decodeManifest,
				(json.dumps(categoryManifest).encode("utf-8"),),
			),
		)
		for name, decoder, arguments in malformedCases:
			with self.subTest(name=name):
				with self.assertRaises(oneDriveProtocol.OneDriveError):
					decoder(*arguments)

	def testManifestMergeRebaseAndCompaction(self) -> None:
		"""Preserve manifest versions while merging, rebasing, and compacting."""
		item = self._makeImageItem()
		payloadHash = storage.getItemPayloadHash(item)
		firstVersion = oneDriveProtocol.SyncVersion(1, "00000000-0000-0000-0000-000000000001")
		secondVersion = oneDriveProtocol.SyncVersion(2, "00000000-0000-0000-0000-000000000002")
		dedupKey = storage.getItemDedupKey(item)
		local = oneDriveProtocol.OneDriveSyncSnapshot(
			accountId="account",
			history=(oneDriveProtocol.OneDriveHistoryState(dedupKey, payloadHash, firstVersion, False),),
			categories=(
				oneDriveProtocol.OneDriveCategoryState("work", "Work", firstVersion, firstVersion, False),
			),
			categoryItems=(
				oneDriveProtocol.OneDriveCategoryItemState("work", payloadHash, firstVersion, False),
			),
		)
		remote = oneDriveProtocol.OneDriveSyncSnapshot(
			accountId=None,
			history=(oneDriveProtocol.OneDriveHistoryState(dedupKey, None, secondVersion, True),),
			categories=(
				oneDriveProtocol.OneDriveCategoryState("work", "WORK", secondVersion, secondVersion, False),
			),
			categoryItems=(
				oneDriveProtocol.OneDriveCategoryItemState("work", payloadHash, secondVersion, True),
			),
		)
		merged = oneDriveProtocol._mergeSnapshots(local, remote)
		self.assertTrue(merged.history[0].deleted)
		self.assertEqual(merged.categories[0].name, "WORK")
		self.assertEqual(merged.categories[0].orderVersion, firstVersion)
		self.assertTrue(merged.categoryItems[0].deleted)
		self.assertEqual(
			oneDriveProtocol._decodeManifest(oneDriveProtocol._encodeManifest(merged)),
			replace(merged, accountId=None),
		)
		unsupportedManifest = json.loads(oneDriveProtocol._encodeManifest(merged))
		unsupportedManifest["schemaVersion"] = 1
		del unsupportedManifest["generation"]
		with self.assertRaises(oneDriveProtocol.OneDriveError):
			oneDriveProtocol._decodeManifest(json.dumps(unsupportedManifest).encode("utf-8"))
		newGeneration = oneDriveProtocol.SyncVersion(3, "00000000-0000-0000-0000-000000000003")
		compacted = oneDriveProtocol._compactSnapshot(merged, newGeneration)
		self.assertEqual(
			compacted,
			replace(merged, history=(), categoryItems=(), generation=newGeneration),
		)
		dirtyLocal = replace(
			local,
			history=(replace(local.history[0], dirty=True),),
			categoryItems=(replace(local.categoryItems[0], dirty=True),),
		)
		newBaseline = oneDriveProtocol.OneDriveSyncSnapshot(
			accountId=None,
			history=(),
			categories=(),
			categoryItems=(),
			generation=newGeneration,
		)
		rebaseStates = oneDriveProtocol._generationRebaseStates(dirtyLocal, newBaseline)
		self.assertEqual(rebaseStates[0], dirtyLocal.history)
		self.assertEqual(rebaseStates[1], dirtyLocal.categories)
		self.assertEqual(rebaseStates[2], dirtyLocal.categoryItems)
		historyVersion = oneDriveProtocol.SyncVersion(4, "00000000-0000-0000-0000-000000000004")
		categoryVersion = oneDriveProtocol.SyncVersion(5, "00000000-0000-0000-0000-000000000005")
		categoryItemVersion = oneDriveProtocol.SyncVersion(
			6,
			"00000000-0000-0000-0000-000000000006",
		)
		rebased = oneDriveProtocol._rebaseLocalSnapshot(
			dirtyLocal,
			newBaseline,
			rebaseStates,
			(historyVersion, categoryVersion, categoryItemVersion),
		)
		self.assertEqual(
			rebased,
			replace(
				dirtyLocal,
				history=(replace(dirtyLocal.history[0], version=historyVersion, dirty=False),),
				categories=(
					replace(
						dirtyLocal.categories[0],
						orderVersion=categoryVersion,
						version=categoryVersion,
						dirty=False,
					),
				),
				categoryItems=(
					replace(dirtyLocal.categoryItems[0], version=categoryItemVersion, dirty=False),
				),
				generation=newGeneration,
			),
		)
		conflicting = replace(
			local,
			history=(replace(local.history[0], payloadHash=b"z" * 32),),
		)
		with self.assertRaises(oneDriveProtocol.OneDriveError):
			oneDriveProtocol._mergeSnapshots(local, conflicting)

	def testNormalizationAndMalformedManifests(self) -> None:
		"""Apply history and image limits and reject malformed manifest clocks."""
		history = tuple(
			oneDriveProtocol.OneDriveHistoryState(
				index.to_bytes(32, "big"),
				index.to_bytes(32, "big"),
				oneDriveProtocol.SyncVersion(index + 1, str(UUID(int=index + 1))),
				False,
			)
			for index in range(3)
		)
		limitSnapshot = oneDriveProtocol.OneDriveSyncSnapshot("account", history, (), ())
		with patch.object(oneDriveProtocol, "MAX_HISTORY_ITEMS", 2):
			historyKeys, categoryItemKeys = oneDriveProtocol._normalizationTargets(
				limitSnapshot,
				{state.payloadHash: 0 for state in history if state.payloadHash is not None},
				{},
			)
			self.assertEqual(historyKeys, (history[0].dedupKey,))
			self.assertEqual(categoryItemKeys, ())
			normalizationVersion = oneDriveProtocol.SyncVersion(4, str(UUID(int=4)))
			limited = oneDriveProtocol._applyNormalization(
				limitSnapshot,
				historyKeys,
				categoryItemKeys,
				(normalizationVersion,),
			)
		self.assertEqual(
			limited,
			replace(
				limitSnapshot,
				history=(
					replace(
						history[0],
						payloadHash=None,
						version=normalizationVersion,
						deleted=True,
					),
					history[1],
					history[2],
				),
			),
		)
		newestImageHash = b"n" * 32
		oldestImageHash = b"o" * 32
		imageHistory = (
			oneDriveProtocol.OneDriveHistoryState(
				b"N" * 32,
				newestImageHash,
				oneDriveProtocol.SyncVersion(2, str(UUID(int=2))),
				False,
			),
			oneDriveProtocol.OneDriveHistoryState(
				b"O" * 32,
				oldestImageHash,
				oneDriveProtocol.SyncVersion(1, str(UUID(int=1))),
				False,
			),
		)
		with patch.object(oneDriveProtocol, "MAX_TOTAL_IMAGE_BYTES", 10):
			historyKeys, categoryItemKeys = oneDriveProtocol._normalizationTargets(
				oneDriveProtocol.OneDriveSyncSnapshot("account", imageHistory, (), ()),
				{newestImageHash: 6, oldestImageHash: 6},
				{},
			)
		self.assertEqual(historyKeys, (b"O" * 32,))
		self.assertEqual(categoryItemKeys, ())
		manifest = json.loads(oneDriveProtocol._encodeManifest(limitSnapshot))
		manifest["history"][0]["version"]["operationId"] = "not-a-uuid"
		with self.assertRaises(oneDriveProtocol.OneDriveError):
			oneDriveProtocol._decodeManifest(json.dumps(manifest).encode("utf-8"))
		for invalidClock in (oneDriveProtocol._MAX_SYNC_CLOCK - 1, oneDriveProtocol._MAX_SYNC_CLOCK):
			invalidManifest = json.loads(oneDriveProtocol._encodeManifest(limitSnapshot))
			invalidManifest["history"][0]["version"]["clock"] = invalidClock
			with self.assertRaises(oneDriveProtocol.OneDriveError):
				oneDriveProtocol._decodeManifest(json.dumps(invalidManifest).encode("utf-8"))
		with self.assertRaises(oneDriveProtocol.OneDriveError):
			oneDriveProtocol._decodeManifest(b"{}")
