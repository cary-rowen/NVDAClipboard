### 0.3.0

- Press `NVDA+C` once to report a clipboard summary and twice in quick succession to view its content in browse mode. This replaces the add-on's previous summary shortcuts and NVDA's standard clipboard report.
- View HTML, Markdown, and common LaTeX content with scripts and external resource loading removed. An option in settings lets you show the text as-is instead.
- Browse stored entries across clipboard history and user categories without opening the manager. Assign the category cycling command in Input Gestures; opening the manager starts at the current category and entry.
- Use `NVDA+Windows+[` and `NVDA+Windows+]` to mark a clipboard text selection in either direction.
- Use `NVDA+Windows+V` to paste the selection, or the current stored entry if there is no complete selection. The current entry remains selected after pasting, so you can paste it again.
- Pasting stored entries preserves the original formatting when possible, using plain text when necessary.
- Updated default gestures: laptop clipboard text navigation uses `NVDA+Windows` instead of `NVDA+Alt` combinations; the laptop-specific current-line gesture and the save clipboard image gesture are no longer assigned; appending the last spoken text uses `NVDA+Shift+X`.
- Clarified English and Chinese documentation and messages for text selection, stored-entry navigation, and pasting.

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
