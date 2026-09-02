### 0.2.8

- Added a browse mode clipboard viewer: press `NVDA+C` once for a summary and twice to open the current content.
- Added rendering for clipboard HTML, Markdown, and common LaTeX, with readable fallbacks when rich content cannot be displayed and active web content removed.
- Added an option to show clipboard content as plain text in browse mode; rendering remains enabled by default.
- Updated default gestures: laptop clipboard navigation now uses `NVDA+Windows` combinations, the current-line laptop gesture and clipboard image save gesture are unassigned, and appending the last spoken text now uses `NVDA+Shift+X`.

### 0.2.6

- Added global navigation across clipboard history and user categories for stored entries. The category cycling command is unassigned by default and can be configured in NVDA's Input Gestures dialog.
- Updated the next, previous, and restore commands to operate within the selected category, and opened Clipboard Manager at the current global navigation position.
- Added indexed category summary lookups for responsive repeated navigation.
- Updated terminology, documentation, and Chinese translations.

### 0.2.5

- Improved Clipboard Manager search, file-group cleanup, and save behavior for more reliable editing and responsive operation.
- Normalized line endings when pasting NVDA's last spoken text.
- Improved OneDrive token cache writes and synchronization reliability.
- Removed redundant internal code and simplified maintenance tooling.

### 0.2.3

- Improved temporary pasting of NVDA's last spoken text by retrying brief clipboard sequence races before failing.

### 0.2.2

- Clarified Clipboard Manager save commands so they show the active save target and added a save-and-close command.
- Limited Clipboard Manager editor synchronization with clipboard navigation to the initial open, so editing no longer changes global clipboard navigation position.

### 0.2.1

Minor fixes.

### 0.2.0

- Consolidated vendored runtime dependencies under `addon/globalPlugins/nvdaClipboard/_vendor`.
- Split native runtime files into `_vendor/_native/amd64`, `_vendor/_native/arm64`, and `_vendor/_native/notices`.
- Kept the cloud and SQLite loaders thin while preserving amd64 and ARM64 loading paths.
