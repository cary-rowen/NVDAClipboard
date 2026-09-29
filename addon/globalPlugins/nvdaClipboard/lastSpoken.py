# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Read text from NVDA's most recent speech sequence."""

from logHandler import log
from speech import CHUNK_SEPARATOR, speech


def getText() -> str | None:
	"""Return the text from NVDA's most recent speech sequence."""
	# NVDA 2026.3 private record: (speechSequence, symbolLevel).
	try:
		lastSpeech = getattr(speech, "_lastSpeech")
	except AttributeError:
		log.debugWarning("NVDA's last speech record is unavailable.", exc_info=True)
		return None
	if lastSpeech is None:
		return None
	return CHUNK_SEPARATOR.join(item for item in lastSpeech[0] if isinstance(item, str))
