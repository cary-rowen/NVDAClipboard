### 0.4.0

- The **Append selected text** command now supports navigator object text selected with the review cursor, such as buttons and list items.
- Improved the Clipboard Manager layout and saved and restored its window position, size, maximized state, and navigation pane width.
- Added a **Clean Terminal Output** editing command to remove leading and trailing whitespace and excess blank lines.
- Optimized clipboard reads by skipping unnecessary rich-format and image reads, improving responsiveness.
- Improved cancellation handling, temporary pasting, and clipboard file-list size validation.
- Improved storage index migration, image quota handling, and history maintenance.
- Skipped formula scanning for HTML text without mathematical content.
- Updated the English and Chinese README files for consistent content and terminology.

### 0.3.3

- Allow `NVDA+PrintScreen` screenshots with Screen Curtain enabled on supported systems.
- Simplify internal code and remove redundant logic.

### 0.3.1

- Add all-layout shortcuts for clipboard selection marking: `Ctrl+Windows+NumPad 4` and `Ctrl+Windows+NumPad 6`.
- Add the all-layout `Ctrl+Windows+NumPad Divide` shortcut for cycling through stored-entry categories.
- Simplify the documentation so command descriptions appear in the introduction while concrete shortcuts are maintained in the default gesture table.

### 0.3.0

- Press `NVDA+C` once to report a clipboard summary and twice in quick succession to view its content in browse mode. This replaces the add-on's previous summary shortcuts and NVDA's standard clipboard report.
- View HTML, Markdown, and common LaTeX content with scripts and external resource loading removed. An option in settings lets you show the text as-is instead.
- Browse stored entries across clipboard history and user categories without opening the manager. Press `Ctrl+Windows+=` to cycle through categories; opening the manager starts at the current category and entry.
- Use `NVDA+Windows+[` and `NVDA+Windows+]` to mark the current clipboard navigation position as the selection start and end, respectively. Select in either direction, including the characters at both endpoints.
- Use `NVDA+Windows+V` to paste the selection, or the current stored entry if there is no complete selection. Pasting a stored entry keeps it selected for repeated pasting.
- Pasting stored entries preserves the original formatting when possible, using plain text when necessary.
- Laptop clipboard text navigation now uses `NVDA+Windows` instead of `NVDA+Alt` combinations. The laptop-specific current-line gesture is removed; `Ctrl+NumPad8` remains available in both layouts.
- Appending the last spoken text now uses `NVDA+Windows+A`. Saving the clipboard image and receiving and pasting from Tiantan Cloud Clipboard are unassigned by default.
- Unified English and Chinese command descriptions and messages for text selection, stored entries, and putting content on the clipboard. Gesture tables now list all commands for both keyboard layouts, including commands unassigned by default.

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
