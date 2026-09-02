# NVDA Clipboard for NVDA

NVDA Clipboard makes the Windows clipboard easier to explore and reuse with NVDA. You can browse clipboard content, keep a history, work with images and copied files, and synchronize content between computers.

## Requirements

- NVDA 2026.3 or later, using the x64 (AMD64) build or the ARM64 build on Windows 11.
- Tiantan Cloud Clipboard also requires .NET 8 or later.

## Exploring the Clipboard

You can browse clipboard text by line, navigation unit, or character. Navigation units use Windows word boundaries by default. Under **NVDA Clipboard** in NVDA's Settings dialog, you can instead split units at Unicode punctuation and choose whether each punctuation character is a separate unit. Joined words such as `getWord` are further split based on capitalization by default in either mode; this can be turned off. When punctuation is not separate, delimiter clusters are kept with the preceding text where possible. If the clipboard contains copied files, the line commands move through their paths instead. Sound cues indicate boundaries and content that is not plain text.

Text summaries report the current one-based navigation line and character column before the total line and character statistics; a tab counts as one column.

File summaries omit names and report whether the items were copied, cut, or linked, the top-level item count, and, after a background scan, their combined size and recursive file and folder counts. File names and paths remain available through line navigation.

Commands for paging up or down through the clipboard have no default gestures. You can change the number of lines moved per page under **NVDA Clipboard** in NVDA's Settings dialog, and assign the commands under **NVDA Clipboard** in the Input Gestures dialog.

Press the current line or navigation unit command once to read it, twice to spell it, and three times to hear character descriptions. For the current character, the second press gives its description and the third gives its numeric value.

Use `NVDA+Alt+A` to append selected text to the clipboard. Press `NVDA+Shift+X` to append the last text spoken by NVDA, or ``NVDA+` `` to paste it temporarily. Temporary paste attempts to restore the previous clipboard afterward. These commands can be reassigned under **NVDA Clipboard** in NVDA's Input Gestures dialog.

Press `NVDA+C` once to report a clipboard summary and twice to open its content in browse mode. HTML is shown when available; otherwise Markdown and common LaTeX are rendered, while active web content is removed. To view the text as-is, enable **Show clipboard content as plain text in browse mode (do not render)** under **NVDA Clipboard** in NVDA Settings. This gesture replaces NVDA's standard clipboard report.

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

You can also browse stored entries without opening the manager. Assign the "Cycle through categories of stored entries" command in NVDA's Input Gestures dialog to cycle through clipboard history and user categories; the existing previous and next commands move within the selected category, and the restore command puts the selected stored entry on the system clipboard. Opening the manager starts at the same category and entry.

## Images

When the clipboard contains an image, its summary reports exact dimensions and orientation. It also reports an exact fully transparent or solid-color image, or the percentage when at least 95% of its pixels are black, white, or fully transparent. Pixel properties are analyzed only for common PNG and standard 24/32-bit DIB data where source pixels remain exact; other formats omit them rather than risking an inaccurate description. The add-on does not infer image content. The command for saving the current clipboard image has no default gesture; you can also save an image selected in the Clipboard Manager. `NVDA+PrintScreen` copies the current navigator object as an image.

## Cloud Synchronization

Cloud features are optional and are available from the Clipboard Manager's **Cloud** menu.

### OneDrive

OneDrive keeps clipboard history and user categories in sync between your NVDA installations. Sign in with your Microsoft account from **Cloud > OneDrive**. The add-on can access only its own OneDrive application folder, not your other files.

Synchronization is bidirectional and runs automatically while NVDA is running. You can also start it from the **Cloud** menu. Text, formatting, and images can be synchronized; copied file groups remain on the computer where they were created.

Deleting a synchronized entry or category also deletes it from the other synchronized installations. OneDrive synchronization is therefore not a backup.

### Tiantan Cloud Clipboard

Tiantan Cloud Clipboard can send text between this computer and the Tiantan cloud clipboard. Formatting, images, and copied files stay local. Automatic sending is enabled by default and can be turned off from the **Cloud** menu, which also provides manual send and receive commands.

`NVDA+Alt+V` receives Tiantan Cloud Clipboard text and immediately pastes it into the focused application. A manual receive leaves the text on the clipboard without pasting it.

## Privacy

Clipboard history is stored locally and is not encrypted. It can contain sensitive text, images, and file paths, and is protected only by your Windows account.

OneDrive sign-in data is encrypted for the current Windows user, but synchronized clipboard content is not end-to-end encrypted.

## Default Gestures

| Command | All layouts | Laptop-only override |
| --- | --- | --- |
| Report or view clipboard content | `NVDA+C` | — |
| First clipboard line | `Ctrl+NumPad Divide` | `NVDA+Windows+Shift+Up Arrow` |
| Last clipboard line | `Ctrl+NumPad Multiply` | `NVDA+Windows+Shift+Down Arrow` |
| Previous clipboard line | `Ctrl+NumPad 7` | `NVDA+Windows+Up Arrow` |
| Current clipboard line | `Ctrl+NumPad 8` | Unassigned |
| Next clipboard line | `Ctrl+NumPad 9` | `NVDA+Windows+Down Arrow` |
| Previous clipboard navigation unit | `Ctrl+NumPad 4` | `NVDA+Shift+Windows+Left Arrow` |
| Current clipboard navigation unit | `Ctrl+NumPad 5` | `NVDA+Windows+Shift+.` |
| Next clipboard navigation unit | `Ctrl+NumPad 6` | `NVDA+Shift+Windows+Right Arrow` |
| Previous clipboard character | `Ctrl+NumPad 1` | `NVDA+Windows+Left Arrow` |
| Current clipboard character | `Ctrl+NumPad 2` | `NVDA+Windows+.` |
| Next clipboard character | `Ctrl+NumPad 3` | `NVDA+Windows+Right Arrow` |
| Open Clipboard Manager | `NVDA+E` | — |
| Append selected text | `NVDA+Alt+A` | — |
| Append last spoken text | `NVDA+Shift+X` | — |
| Temporarily paste last spoken text | ``NVDA+` `` | — |
| Receive and paste Tiantan Cloud Clipboard text | `NVDA+Alt+V` | — |
| Copy navigator object as an image | `NVDA+PrintScreen` | — |
| Cycle through categories of stored entries | Unassigned | Unassigned |
| Next stored entry in the current category | `Ctrl+Windows+NumPad Plus` | `Ctrl+Windows+]` |
| Previous stored entry in the current category | `Ctrl+Windows+NumPad Minus` | `Ctrl+Windows+[` |
| Put the selected stored entry on the system clipboard | `Ctrl+Windows+NumPad Multiply` | `Ctrl+Windows+\` |
| Save the current clipboard image | Unassigned | Unassigned |
