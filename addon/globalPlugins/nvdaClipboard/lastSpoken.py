# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Track the text from NVDA's most recent speech sequence in memory."""

from threading import Lock

from speech import CHUNK_SEPARATOR
from speech.extensions import pre_speech
from speech.types import SpeechSequence


_lock = Lock()
_isRegistered = False
_lastSpokenText: str | None = None


def initialize() -> None:
	"""Start tracking NVDA speech."""
	global _isRegistered
	with _lock:
		if _isRegistered:
			return
		pre_speech.register(_onPreSpeech)
		_isRegistered = True


def terminate() -> None:
	"""Stop tracking NVDA speech and clear the cached text."""
	global _isRegistered, _lastSpokenText
	with _lock:
		if _isRegistered:
			_isRegistered = False
			pre_speech.unregister(_onPreSpeech)
		_lastSpokenText = None


def getText() -> str | None:
	"""Return the text from NVDA's most recent speech sequence."""
	with _lock:
		return _lastSpokenText


def _onPreSpeech(speechSequence: SpeechSequence, **_kwargs: object) -> None:
	"""Cache text from a speech sequence using NVDA's last-speech semantics."""
	textItems = [item for item in speechSequence if isinstance(item, str)]
	if not textItems:
		return
	text = CHUNK_SEPARATOR.join(textItems)
	global _lastSpokenText
	with _lock:
		if _isRegistered:
			_lastSpokenText = text
