# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for clipboard manager source-to-editor offset conversion."""

from __future__ import annotations

from itertools import product
import unittest

from tests._manager_loader import loadManagerModule


_sourceToEditorOffset = loadManagerModule("nvdaClipboardManagerOffsetTests")._sourceToEditorOffset


class ManagerOffsetTests(unittest.TestCase):
	"""Verify source offsets map to wx's normalized editor offsets."""

	def testSourceOffsetsMatchDirectNormalization(self) -> None:
		"""Preserve clamping and CRLF boundary behavior for mixed line endings."""
		for length in range(7):
			for characters in product("a\r\n", repeat=length):
				text = "".join(characters)
				for offset in range(-2, length * 2 + 3):
					sourceOffset = min(max(offset, 0), len(text))
					if (
						0 < sourceOffset < len(text)
						and text[sourceOffset] == "\n"
						and text[sourceOffset - 1] == "\r"
					):
						sourceOffset -= 1
					expectedEditorOffset = len(
						text[:sourceOffset].replace("\r\n", "\n").replace("\r", "\n"),
					)
					self.assertEqual(expectedEditorOffset, _sourceToEditorOffset(text, offset))


if __name__ == "__main__":
	unittest.main()
