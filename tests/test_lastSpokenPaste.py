# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for temporary paste helpers without loading NVDA."""

from __future__ import annotations

import ast
from dataclasses import dataclass, replace
import unittest
from unittest.mock import Mock, patch

from tests._module_loader import addonSourcePath
from tests.test_clipboardData import clipboardData
from tests.test_clipboardMonitor import clipboardMonitor


_PASTE_PATH = addonSourcePath("paste")
_FORMATTING_PATH = addonSourcePath("summary.formatting")


def _loadTemporaryPasteHelpers() -> tuple[object, ...]:
	"""Load temporary paste helpers without importing paste or formatting dependencies."""
	namespace = {
		"dataclass": dataclass,
		"ClipboardContentType": clipboardMonitor.ClipboardContentType,
		"ClipboardSnapshot": clipboardMonitor.ClipboardSnapshot,
		"ngettext": lambda singular, plural, count: singular if count == 1 else plural,
		"normalizeNewlinesForWin32": clipboardData.normalizeNewlinesForWin32,
		"normalizeUnicodeText": clipboardData.normalizeUnicodeText,
		"replace": replace,
		"log": Mock(),
	}
	for path, classNames, functionNames in (
		(_FORMATTING_PATH, set(), {"getTextOrCharacterCount"}),
		(
			_PASTE_PATH,
			{"_TemporaryPasteState", "_TemporaryPasteRequest"},
			{
				"_isTemporarySnapshot",
				"_getPlainTextPasteRequest",
				"preparePngDib",
				"_prepareTemporaryPngPaste",
			},
		),
	):
		tree = ast.parse(path.read_text(encoding="utf-8"))
		nodes = [
			node
			for node in tree.body
			if (
				isinstance(node, ast.ClassDef)
				and node.name in classNames
				or isinstance(node, ast.FunctionDef)
				and node.name in functionNames
			)
		]
		exec(compile(ast.Module(body=nodes, type_ignores=[]), path, "exec"), namespace)
	return (
		namespace["_TemporaryPasteState"],
		namespace["_isTemporarySnapshot"],
		namespace["getTextOrCharacterCount"],
		namespace["_TemporaryPasteRequest"],
		namespace["_prepareTemporaryPngPaste"],
	)


(
	_TemporaryPasteState,
	_isTemporarySnapshot,
	_getTextOrCharacterCount,
	_TemporaryPasteRequest,
	_prepareTemporaryPngPaste,
) = _loadTemporaryPasteHelpers()


class TemporaryPasteTests(unittest.TestCase):
	"""Verify bounded feedback and temporary clipboard matching."""

	def testPngPreparationRetainsTextButNeverInventsImageText(self) -> None:
		"""Keep rich data on success and degrade failed mixed images only when actual text exists."""
		for text, fails in (("full\ntext", False), ("full\ntext", True), ("", True)):
			with self.subTest(text=text, fails=fails):
				snapshot = clipboardMonitor.ClipboardSnapshot(
					clipboardMonitor.ClipboardContentType.TEXT_AND_IMAGE
					if text
					else clipboardMonitor.ClipboardContentType.IMAGE,
					text=text,
					imageFormat="PNG",
					imageData=b"png",
					canIncludeInHistory=False,
					canUpload=False,
				)
				request = _TemporaryPasteRequest(
					snapshot,
					"image summary",
					frozenset({1}),
					3.0,
					expectedFocus=object(),
				)
				with patch.dict(
					_prepareTemporaryPngPaste.__globals__,
					pngToPackedDib=Mock(
						return_value=b"dib",
						side_effect=ValueError("decode failed") if fails else None,
					),
				):
					if not text:
						with self.assertRaises(ValueError):
							_prepareTemporaryPngPaste(request)
						continue
					prepared = _prepareTemporaryPngPaste(request)
				self.assertEqual(text, prepared.snapshot.text)
				self.assertIs(request.expectedFocus, prepared.expectedFocus)
				self.assertEqual(request.triggerKeyCodes, prepared.triggerKeyCodes)
				self.assertEqual(request.keyReleaseDeadline, prepared.keyReleaseDeadline)
				self.assertFalse(prepared.snapshot.canIncludeInHistory)
				self.assertFalse(prepared.snapshot.canUpload)
				self.assertEqual(None if fails else b"png", prepared.snapshot.imageData)
				self.assertEqual(None if fails else b"dib", prepared.preparedPngDib)
				self.assertEqual(fails, prepared.usedPlainTextFallback)

	def testTextFeedbackUsesConfiguredCharacterLimit(self) -> None:
		"""Replace lengthy selection and paste feedback with a character count."""
		for maxLength in (512, 1024):
			with self.subTest(maxLength=maxLength):
				shortText = "x" * (maxLength - 1)
				self.assertEqual(shortText, _getTextOrCharacterCount(shortText, maxLength))
				self.assertEqual(
					f"{maxLength} characters",
					_getTextOrCharacterCount("x" * maxLength, maxLength),
				)

	def testTemporarySnapshotsMatchEveryPasteableContentType(self) -> None:
		"""Accept retained payloads while tolerating Win32-derived fields."""
		contentType = clipboardMonitor.ClipboardContentType
		snapshots = (
			clipboardMonitor.ClipboardSnapshot(contentType.TEXT, text="first\nsecond"),
			clipboardMonitor.ClipboardSnapshot(
				contentType.FORMATTED_TEXT,
				text="formatted\ntext",
				html=b"<b>formatted</b>",
				rtf=b"{\\rtf1 formatted}",
			),
			clipboardMonitor.ClipboardSnapshot(
				contentType.IMAGE,
				imageFormat="PNG",
				imageData=b"png payload",
				imageWidth=10,
				imageHeight=20,
				imageBitDepth=32,
			),
			clipboardMonitor.ClipboardSnapshot(
				contentType.TEXT_AND_IMAGE,
				text="mixed\ntext",
				html=b"<i>mixed</i>",
				imageFormat="PNG",
				imageData=b"mixed png payload",
				imageWidth=30,
				imageHeight=40,
				imageBitDepth=24,
			),
			clipboardMonitor.ClipboardSnapshot(
				contentType.FILES,
				files=(r"C:\one.txt", r"C:\two.txt"),
			),
		)
		for sequenceNumber, expected in enumerate(snapshots, start=1):
			with self.subTest(contentType=expected.contentType):
				expected = replace(expected, canIncludeInHistory=False, canUpload=False)
				actual = replace(
					expected,
					sequenceNumber=sequenceNumber,
					text=expected.text.replace("\n", "\r\n"),
					preferredDropEffect=2 if expected.files else None,
				)
				state = _TemporaryPasteState(None, sequenceNumber, expected)
				self.assertTrue(_isTemporarySnapshot(actual, state))

	def testTemporarySnapshotRejectsChangedPayloadOrPolicy(self) -> None:
		"""Reject any changed format payload and snapshots lacking restrictive policy flags."""
		contentType = clipboardMonitor.ClipboardContentType
		expected = clipboardMonitor.ClipboardSnapshot(
			contentType.TEXT_AND_IMAGE,
			text="text",
			html=b"html",
			rtf=b"rtf",
			imageFormat="PNG",
			imageData=b"png",
			imageWidth=10,
			imageHeight=20,
			imageBitDepth=32,
			canIncludeInHistory=False,
			canUpload=False,
		)
		state = _TemporaryPasteState(None, 1, expected)
		changes = {
			"contentType": contentType.FORMATTED_TEXT,
			"text": "other",
			"html": b"other html",
			"rtf": b"other rtf",
			"imageFormat": "DIB",
			"imageData": b"other image",
			"imageWidth": 11,
			"imageHeight": 21,
			"imageBitDepth": 24,
			"files": (r"C:\other.txt",),
			"canIncludeInHistory": True,
			"canUpload": True,
			"sequenceNumber": 0,
		}
		actual = replace(expected, sequenceNumber=1)
		for fieldName, changedValue in changes.items():
			with self.subTest(fieldName=fieldName):
				self.assertFalse(_isTemporarySnapshot(replace(actual, **{fieldName: changedValue}), state))


if __name__ == "__main__":
	unittest.main()
