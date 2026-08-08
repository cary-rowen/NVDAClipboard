# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Play the small set of sound cues used by NVDA Clipboard."""

from pathlib import Path

from logHandler import log
from nvwave import playWaveFile


_SOUNDS_DIRECTORY = Path(__file__).parents[2] / "Sounds"
_reportedFailures: set[str] = set()


def playCopy() -> None:
	"""Play the cue for a new local clipboard value."""
	_play("Copy.wav")


def playNonPlainText() -> None:
	"""Play the cue for non-plain-text clipboard content."""
	_play("NonPlainText.wav")


def playBoundary() -> None:
	"""Play the cue for the beginning or end of available content."""
	_play("StartOrEnd.wav")


def playLineBoundary() -> None:
	"""Play the cue for navigation that crosses a line boundary."""
	_play("LineBoundary.wav")


def _play(fileName: str) -> None:
	try:
		playWaveFile(str(_SOUNDS_DIRECTORY / fileName))
	except Exception:
		if fileName not in _reportedFailures:
			_reportedFailures.add(fileName)
			log.debugWarning(f"Could not play NVDA Clipboard sound: {fileName}", exc_info=True)
