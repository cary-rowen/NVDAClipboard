"""Load the bundled SQLite extension for the NVDA Clipboard add-on."""

from __future__ import annotations

from ._nativeDeps import loadExtensionModule


_module = loadExtensionModule("_sqlite3", "_sqlite3.pyd")
for name in dir(_module):
	if name in {
		"__builtins__",
		"__cached__",
		"__doc__",
		"__file__",
		"__loader__",
		"__name__",
		"__package__",
		"__spec__",
	}:
		continue
	globals()[name] = getattr(_module, name)
