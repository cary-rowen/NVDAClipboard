# NVDA Clipboard for NVDA

NVDA Clipboard makes the Windows clipboard easier to explore and reuse with NVDA. You can browse clipboard content, keep a history, work with images and copied files, and synchronize content between computers.

## Requirements

- NVDA 2026.3 or later, using the x64 (AMD64) build. Native ARM64 NVDA is not currently supported.
- Tiantan Clipboard also requires .NET 8 or later.

## Native ARM64 Support (TODO)

Native ARM64 support requires new binary packages, not only a compatibility metadata change. The add-on currently ships x64 builds of `_sqlite3.pyd`, `sqlite3.dll`, and `ClipDataCloud.SDK.dll`. `_sqlite3.pyd` is imported when the add-on starts, so native ARM64 NVDA cannot load the add-on. The Tiantan SDK is optional, but Tiantan features are also unavailable until a compatible SDK is provided.

Before native ARM64 can be declared supported:

- Package ARM64 builds of `_sqlite3.pyd` and `sqlite3.dll` that match the Python version bundled with NVDA.
- Package an ARM64 `ClipDataCloud.SDK.dll`, or keep Tiantan features cleanly disabled on ARM64 when no compatible SDK is available.
- Make the add-on package select binaries matching the running NVDA architecture and reject mismatched binaries.
- Verify add-on startup, SQLite history operations, upgrades, and Tiantan availability in native ARM64 NVDA.

## Exploring the Clipboard

You can browse clipboard text by line, word, or character. If the clipboard contains copied files, the line commands move through their paths instead. Sound cues indicate boundaries and content that is not plain text.

Press the current line or word command once to read it, twice to spell it, and three times to hear character descriptions. For the current character, the second press gives its description and the third gives its numeric value.

Use `NVDA+Alt+A` to append selected text to the clipboard. You can also copy, append, or temporarily paste the last text spoken by NVDA. Temporary paste attempts to restore the previous clipboard afterward. These three commands have no default gestures; you can assign them under **NVDA Clipboard** in NVDA's Input Gestures dialog.

## Clipboard History

Copies are added automatically to a history that supports:

- Plain and formatted text, including HTML and RTF.
- Images and content containing both text and an image.
- One or more copied files as a single file group.

Press `NVDA+E` to open the Clipboard Manager. From there you can:

- Browse and delete history entries.
- Press Enter or choose **Put on Clipboard** to restore an entry. This does not paste it into an application.
- Edit text, open or save text files, find and replace text (including with regular expressions), and go to a line.
- Create your own categories and collect or move entries into them.
- Save an image from the selected entry.

Formatted text keeps its original formatting when restored. Editing its text in the manager creates plain text and does not alter the original entry. File groups contain paths rather than copies of the files themselves, so an entry may stop working if its files are moved or deleted.

Older history entries are removed automatically, while entries in your own categories are retained. Older uncollected images may be removed sooner because image storage is limited.

You can also use the global history commands to move through recent entries without opening the manager and then put the selected entry back on the clipboard.

## Images

When the clipboard contains an image, the add-on can report its dimensions and color depth. Use `NVDA+Alt+PrintScreen` to save the current clipboard image, or save an image selected in the Clipboard Manager. `NVDA+PrintScreen` copies the current navigator object as an image.

## Cloud Synchronization

Cloud features are optional and are available from the Clipboard Manager's **Cloud** menu.

### OneDrive

OneDrive keeps clipboard history and user categories in sync between your NVDA installations. Sign in with your Microsoft account from **Cloud > OneDrive**. The add-on can access only its own OneDrive application folder, not your other files.

Synchronization is bidirectional and runs automatically while NVDA is running. You can also start it from the **Cloud** menu. Text, formatting, and images can be synchronized; copied file groups remain on the computer where they were created.

Deleting a synchronized entry or category also deletes it from the other synchronized installations. OneDrive synchronization is therefore not a backup.

### Tiantan Clipboard

Tiantan Clipboard can send text between this computer and the Tiantan cloud clipboard. Formatting, images, and copied files stay local. Automatic sending is enabled by default and can be turned off from the **Cloud** menu, which also provides manual send and receive commands.

`NVDA+Alt+V` receives Tiantan text and immediately pastes it into the focused application. A manual receive leaves the text on the clipboard without pasting it.

## Privacy

Clipboard history is stored locally and is not encrypted. It can contain sensitive text, images, and file paths, and is protected only by your Windows account.

OneDrive sign-in data is encrypted for the current Windows user, but synchronized clipboard content is not end-to-end encrypted.

## Default Gestures

| Command | Desktop layout | Laptop layout |
| --- | --- | --- |
| Report clipboard summary | `Ctrl+NumPad Delete` | `NVDA+Alt+'` |
| First clipboard line | `Ctrl+NumPad Divide` | `NVDA+Alt+Shift+Up Arrow` |
| Last clipboard line | `Ctrl+NumPad Multiply` | `NVDA+Alt+Shift+Down Arrow` |
| Previous clipboard line | `Ctrl+NumPad 7` | `NVDA+Alt+Up Arrow` |
| Current clipboard line | `Ctrl+NumPad 8` | `NVDA+Alt+L` |
| Next clipboard line | `Ctrl+NumPad 9` | `NVDA+Alt+Down Arrow` |
| Previous clipboard word | `Ctrl+NumPad 4` | `NVDA+Alt+Shift+Left Arrow` |
| Current clipboard word | `Ctrl+NumPad 5` | `NVDA+Alt+Shift+.` |
| Next clipboard word | `Ctrl+NumPad 6` | `NVDA+Alt+Shift+Right Arrow` |
| Previous clipboard character | `Ctrl+NumPad 1` | `NVDA+Alt+Left Arrow` |
| Current clipboard character | `Ctrl+NumPad 2` | `NVDA+Alt+.` |
| Next clipboard character | `Ctrl+NumPad 3` | `NVDA+Alt+Right Arrow` |
| Open Clipboard Manager | `NVDA+E` | `NVDA+E` |
| Append selected text | `NVDA+Alt+A` | `NVDA+Alt+A` |
| Copy last spoken text | Not assigned | Not assigned |
| Append last spoken text | Not assigned | Not assigned |
| Temporarily paste last spoken text | Not assigned | Not assigned |
| Receive and paste Tiantan text | `NVDA+Alt+V` | `NVDA+Alt+V` |
| Copy navigator object as an image | `NVDA+PrintScreen` | `NVDA+PrintScreen` |
| Next clipboard history entry | `Ctrl+Windows+NumPad Plus` | `Ctrl+Windows+]` |
| Previous clipboard history entry | `Ctrl+Windows+NumPad Minus` | `Ctrl+Windows+[` |
| Put the selected history entry on the clipboard | `Ctrl+Windows+NumPad Multiply` | `Ctrl+Windows+\` |
| Save the current clipboard image | `NVDA+Alt+PrintScreen` | `NVDA+Alt+PrintScreen` |
