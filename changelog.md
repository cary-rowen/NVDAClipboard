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
