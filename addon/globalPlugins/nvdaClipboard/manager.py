# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Accessible non-modal clipboard history manager.

``ClipboardManagerFrame`` displays stored clipboard items only. The current
system clipboard is deliberately not used as fallback content, so the category,
entry list, and content control always describe the same stored item.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import locale
from functools import partial
from pathlib import Path
import re
from threading import Event
from collections.abc import Callable
from typing import TYPE_CHECKING

import addonHandler
from gui import nvdaControls
from gui.message import DefaultButton, DialogType, MessageDialog, ReturnCode, displayDialogAsModal
from logHandler import log
import textUtils
import ui
import winUser
import wx

from .cues import playNonPlainText
from .clipboardData import MAX_TEXT_BYTES
from . import textTransforms
from .managerEditor import _ManagerEditorCommands
from .configuration import getConfirmOnClose
from .search import normalizeSearchText, splitSearchKeywords
from .storage import ItemNotFoundError
from .storageModels import ClipboardItemType

if TYPE_CHECKING:
	from .controller import CategoryId, ClipboardController, MissingFileCleanupResult

__all__ = ["ClipboardManagerFrame"]


addonHandler.initTranslation()


_ITEM_LOAD_DELAY_MS = 80
_SEARCH_DELAY_MS = 100


def _sourceToEditorOffset(text: str, offset: int) -> int:
	"""Map a source-text code-point offset to wx's normalized editor text."""
	offset = min(max(offset, 0), len(text))
	if 0 < offset < len(text) and text[offset] == "\n" and text[offset - 1] == "\r":
		offset -= 1
	return offset - text.count("\r\n", 0, offset)


class _SearchTextCtrl(wx.TextCtrl):
	"""Allow explicit search focus while omitting the control from keyboard traversal."""

	def AcceptsFocusFromKeyboard(self) -> bool:
		"""Exclude search from the dialog's Tab order."""
		return False


class _ItemListAccessible(wx.Accessible):
	"""Add a description to the list without replacing native child accessibility."""

	def GetDescription(self, childId: int) -> tuple[int, str]:
		"""Describe the list itself and delegate list-item descriptions."""
		if childId == winUser.CHILDID_SELF:
			# Translators: Accessible description for the clipboard manager entry list.
			return (wx.ACC_OK, _("Press Alt+S to search the current category"))
		return super().GetDescription(childId)


class ClipboardManagerFrame(wx.Frame):
	"""Show clipboard categories, stored items, and accessible content."""

	def __init__(self, parent: wx.Window, controller: ClipboardController) -> None:
		super().__init__(
			parent,
			# Translators: Title of the clipboard manager window.
			title=_("Clipboard Manager"),
			size=(900, 620),
		)
		self.controller = controller
		self._contentEditable = False
		self._contentKind: ClipboardItemType | None = None
		self._contentHasImage = False
		self._contentCanUpload = True
		self._contentSourceText = ""
		self._baselineText = ""
		self._isDirty = False
		self._dirtyStateNeedsCheck = False
		self._isSettingContent = False
		self._isRefreshingList = False
		self._categoryIds: list[CategoryId] = []
		self._allEntries: tuple[tuple[int, str, ClipboardItemType], ...] = ()
		self._itemLabels: tuple[str, ...] = ()
		self._itemKeys: tuple[int, ...] = ()
		self._itemKinds: tuple[ClipboardItemType, ...] = ()
		self._categoryHasItems = False
		self._itemTypeFilter: ClipboardItemType | None = None
		self._viewFilterItems: dict[ClipboardItemType | None, wx.MenuItem] = {}
		self._newItem: wx.MenuItem | None = None
		self._openItem: wx.MenuItem | None = None
		self._segmentChineseWordsItem: wx.MenuItem | None = None
		self._textCleanupMenuItem: wx.MenuItem | None = None
		self._lineOperationsMenuItem: wx.MenuItem | None = None
		self._selectedCategory: CategoryId | None = None
		self._activeItemIndex: int | None = None
		self._activeItemKey: int | None = None
		self._contentCategory: CategoryId | None = None
		self._contentItemKey: int | None = None
		self._contentActiveKey: int | None = None
		self._itemLoadTimer: wx.CallLater | None = None
		self._searchTimer: wx.CallLater | None = None
		self._searchKeywords: tuple[str, ...] = ()
		self._matchingSearchKeys: frozenset[int] | None = None
		self._searchFuture: Future[tuple[int, ...]] | None = None
		self._searchCancelEvent: Event | None = None
		self._searchExecutor = ThreadPoolExecutor(
			max_workers=1,
			thread_name_prefix="nvdaClipboard.managerSearch",
		)
		self._pendingSearchListState: tuple[int | None, int | None, tuple[int, ...]] | None = None
		self._enterSearchResultsWhenReady = False
		self._isSearchSessionActive = False
		self._searchRestoreListState: tuple[int | None, int | None, tuple[int, ...]] = (None, None, ())
		self._isBeingDestroyed = False
		self._makeUi()
		self._editorCommands = _ManagerEditorCommands(
			self,
			self.editor,
			showInfo=self._showInfo,
			showError=self._showError,
		)
		self._makeMenus()
		self.Bind(wx.EVT_CLOSE, self._onClose)
		self.Bind(wx.EVT_WINDOW_DESTROY, self._onWindowDestroy)
		self.Bind(wx.EVT_CHAR_HOOK, self._onCharHook)
		self.SetMinSize((700, 480))
		self.CentreOnScreen()

	def _makeUi(self) -> None:
		panel = wx.Panel(self)
		mainSizer = wx.BoxSizer(wx.VERTICAL)

		categorySizer = wx.BoxSizer(wx.VERTICAL)
		categoryLabel = wx.StaticText(
			panel,
			# Translators: Label for the clipboard category list.
			label=_("&Category:"),
		)
		self.categoryList = wx.ListBox(panel)
		categorySizer.Add(categoryLabel, flag=wx.BOTTOM, border=5)
		categorySizer.Add(self.categoryList, proportion=1, flag=wx.EXPAND)
		mainSizer.Add(categorySizer, flag=wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, border=10)

		searchSizer = wx.BoxSizer(wx.HORIZONTAL)
		searchLabel = wx.StaticText(
			panel,
			# Translators: Label for searching entries in the selected clipboard category.
			label=_("&Search:"),
		)
		self.searchCtrl = _SearchTextCtrl(panel)
		self.clearSearchButton = wx.Button(
			panel,
			# Translators: Button that exits clipboard manager entry search.
			label=_("C&lear Search"),
		)
		self.clearSearchButton.Disable()
		searchSizer.Add(searchLabel, flag=wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, border=8)
		searchSizer.Add(self.searchCtrl, proportion=1, flag=wx.RIGHT | wx.EXPAND, border=8)
		searchSizer.Add(self.clearSearchButton)
		mainSizer.Add(searchSizer, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)

		contentSizer = wx.BoxSizer(wx.HORIZONTAL)
		itemsSizer = wx.BoxSizer(wx.VERTICAL)
		self.itemsLabel = wx.StaticText(
			panel,
			# Translators: Label for the stored clipboard entry list.
			label=_("En&tries:"),
		)
		self.itemList = nvdaControls.AutoWidthColumnListCtrl(
			panel,
			autoSizeColumn=1,
			itemTextCallable=self._getItemText,
			style=wx.LC_REPORT | wx.LC_NO_HEADER | wx.LC_VIRTUAL,
		)
		self.itemList.InsertColumn(0, self.itemsLabel.GetLabel())
		self.itemList.SetAccessible(_ItemListAccessible(self.itemList))
		itemsSizer.Add(self.itemsLabel, flag=wx.BOTTOM, border=5)
		itemsSizer.Add(self.itemList, proportion=1, flag=wx.EXPAND)

		textSizer = wx.BoxSizer(wx.VERTICAL)
		self.contentLabel = wx.StaticText(
			panel,
			# Translators: Label for the selected clipboard entry content.
			label=_("C&ontent:"),
		)
		self.editor = wx.TextCtrl(
			panel,
			style=wx.TE_MULTILINE | wx.TE_RICH2 | wx.HSCROLL,
		)
		textSizer.Add(self.contentLabel, flag=wx.BOTTOM | wx.EXPAND, border=5)
		textSizer.Add(self.editor, proportion=1, flag=wx.EXPAND)

		contentSizer.Add(itemsSizer, proportion=1, flag=wx.RIGHT | wx.EXPAND, border=10)
		contentSizer.Add(textSizer, proportion=2, flag=wx.EXPAND)
		mainSizer.Add(contentSizer, proportion=1, flag=wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, border=10)
		panel.SetSizer(mainSizer)

		self.categoryList.Bind(wx.EVT_LISTBOX, self._onCategoryChanged)
		self.categoryList.Bind(wx.EVT_CONTEXT_MENU, self._onCategoryContextMenu)
		self.searchCtrl.Bind(wx.EVT_TEXT, self._onSearchTextChanged)
		self.clearSearchButton.Bind(wx.EVT_BUTTON, self._onClearSearch)
		self.itemList.Bind(wx.EVT_SET_FOCUS, self._onItemListSetFocus)
		self.itemList.Bind(wx.EVT_LIST_ITEM_FOCUSED, self._onItemFocused)
		self.itemList.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._onPutItemOnClipboard)
		self.itemList.Bind(wx.EVT_CONTEXT_MENU, self._onItemContextMenu)
		self.editor.Bind(wx.EVT_TEXT, self._onContentChanged)

	def _makeMenus(self) -> None:
		menuBar = wx.MenuBar()
		fileMenu = wx.Menu()
		self._newItem = fileMenu.Append(
			wx.ID_NEW,
			# Translators: File menu command to start a blank clipboard text entry.
			_("&New\tCtrl+N"),
		)
		self._openItem = fileMenu.Append(
			wx.ID_OPEN,
			# Translators: File menu command to open a text file as a draft.
			_("&Open Text File...\tCtrl+O"),
		)
		self.saveAsItem = fileMenu.Append(
			wx.ID_SAVEAS,
			# Translators: File menu command to save visible text to a file.
			_("Save &Text As...\tCtrl+Shift+S"),
		)
		self.replaceClipboardItem = fileMenu.Append(
			wx.ID_ANY,
			# Translators: File menu command to save the visible text to the active target.
			_("Save to Target\tCtrl+S"),
		)
		self.savePlainTextToCategoryItem = fileMenu.Append(
			wx.ID_ANY,
			# Translators: File menu command to save the visible text and close the window.
			_("Save to Target and Close Window\tCtrl+Shift+X"),
		)
		fileMenu.AppendSeparator()
		exitItem = fileMenu.Append(
			wx.ID_EXIT,
			# Translators: File menu command to hide the clipboard manager.
			_("E&xit\tCtrl+E"),
		)
		menuBar.Append(
			fileMenu,
			# Translators: Name of the File menu.
			_("&File"),
		)

		editMenu = wx.Menu()
		self.findItem = editMenu.Append(
			wx.ID_FIND,
			# Translators: Edit menu command to find text in selected content.
			_("&Find...\tCtrl+F"),
		)
		self.continueFindItem = editMenu.Append(
			wx.ID_ANY,
			# Translators: Edit menu command to find the next occurrence.
			_("Find &Next\tF3"),
		)
		self.previousFindItem = editMenu.Append(
			wx.ID_ANY,
			# Translators: Edit menu command to find the previous occurrence.
			_("Find &Previous\tShift+F3"),
		)
		self.replaceItem = editMenu.Append(
			wx.ID_ANY,
			# Translators: Edit menu command to replace editable plain text.
			_("&Replace...\tCtrl+H"),
		)
		self.gotoLineItem = editMenu.Append(
			wx.ID_ANY,
			# Translators: Edit menu command to move to a line in content.
			_("&Go to Line...\tCtrl+G"),
		)
		self._segmentChineseWordsItem = editMenu.Append(
			wx.ID_ANY,
			# Translators: Edit menu command to insert spaces at Chinese word boundaries.
			_("Segment Chinese &Words"),
		)
		editMenu.AppendSeparator()
		textCleanupMenu = wx.Menu()
		trailingSpacesItem = textCleanupMenu.Append(
			wx.ID_ANY,
			# Translators: Edit menu command to trim trailing spaces from each line.
			_("Trim Trailing &Spaces"),
		)
		leadingSpacesItem = textCleanupMenu.Append(
			wx.ID_ANY,
			# Translators: Edit menu command to trim leading spaces from each line.
			_("Trim Leading S&paces"),
		)
		leadingAndTrailingSpacesItem = textCleanupMenu.Append(
			wx.ID_ANY,
			# Translators: Edit menu command to trim leading and trailing spaces from each line.
			_("Trim Leading and Trailing S&paces"),
		)
		lineBreaksToSpacesItem = textCleanupMenu.Append(
			wx.ID_ANY,
			# Translators: Edit menu command to replace line breaks with spaces.
			_("Replace &Line Breaks with Spaces"),
		)
		removeBlankLinesItem = textCleanupMenu.Append(
			wx.ID_ANY,
			# Translators: Edit menu command to remove blank lines.
			_("Remove &Blank Lines"),
		)
		removeConsecutiveBlankLinesItem = textCleanupMenu.Append(
			wx.ID_ANY,
			# Translators: Edit menu command to collapse consecutive blank lines.
			_("Remove Consecutive B&lank Lines"),
		)
		self._textCleanupMenuItem = editMenu.AppendSubMenu(
			textCleanupMenu,
			# Translators: Submenu containing text cleanup commands.
			_("Text &Cleanup"),
		)
		lineOperationsMenu = wx.Menu()
		removeConsecutiveDuplicatesItem = lineOperationsMenu.Append(
			wx.ID_ANY,
			# Translators: Edit menu command to remove consecutive duplicate lines.
			_("Remove Consecutive &Duplicate Lines"),
		)
		removeDuplicatesItem = lineOperationsMenu.Append(
			wx.ID_ANY,
			# Translators: Edit menu command to remove duplicate lines.
			_("Remove &Duplicate Lines"),
		)
		sortLinesDescendingItem = lineOperationsMenu.Append(
			wx.ID_ANY,
			# Translators: Edit menu command to sort lines by length from longest to shortest.
			_("Sort Lines by Length, &Descending"),
		)
		sortLinesAscendingItem = lineOperationsMenu.Append(
			wx.ID_ANY,
			# Translators: Edit menu command to sort lines by length from shortest to longest.
			_("Sort Lines by Length, &Ascending"),
		)
		self._lineOperationsMenuItem = editMenu.AppendSubMenu(
			lineOperationsMenu,
			# Translators: Submenu containing line-based clipboard text operations.
			_("&Line Operations"),
		)
		self.Bind(
			wx.EVT_MENU,
			partial(self._onApplyTextTransform, transform=textTransforms.trimTrailingSpaces),
			trailingSpacesItem,
		)
		self.Bind(
			wx.EVT_MENU,
			partial(self._onApplyTextTransform, transform=textTransforms.trimLeadingSpaces),
			leadingSpacesItem,
		)
		self.Bind(
			wx.EVT_MENU,
			partial(self._onApplyTextTransform, transform=textTransforms.trimLeadingAndTrailingSpaces),
			leadingAndTrailingSpacesItem,
		)
		self.Bind(
			wx.EVT_MENU,
			partial(self._onApplyTextTransform, transform=textTransforms.replaceLineBreaksWithSpaces),
			lineBreaksToSpacesItem,
		)
		self.Bind(
			wx.EVT_MENU,
			partial(self._onApplyTextTransform, transform=textTransforms.segmentChineseWords),
			self._segmentChineseWordsItem,
		)
		self.Bind(
			wx.EVT_MENU,
			partial(self._onApplyTextTransform, transform=textTransforms.removeBlankLines),
			removeBlankLinesItem,
		)
		self.Bind(
			wx.EVT_MENU,
			partial(self._onApplyTextTransform, transform=textTransforms.removeConsecutiveBlankLines),
			removeConsecutiveBlankLinesItem,
		)
		self.Bind(
			wx.EVT_MENU,
			partial(self._onApplyTextTransform, transform=textTransforms.removeConsecutiveDuplicateLines),
			removeConsecutiveDuplicatesItem,
		)
		self.Bind(
			wx.EVT_MENU,
			partial(self._onApplyTextTransform, transform=textTransforms.removeDuplicateLines),
			removeDuplicatesItem,
		)
		self.Bind(
			wx.EVT_MENU,
			partial(self._onApplyTextTransform, transform=textTransforms.sortLinesByLengthDescending),
			sortLinesDescendingItem,
		)
		self.Bind(
			wx.EVT_MENU,
			partial(self._onApplyTextTransform, transform=textTransforms.sortLinesByLengthAscending),
			sortLinesAscendingItem,
		)
		menuBar.Append(
			editMenu,
			# Translators: Name of the Edit menu.
			_("&Edit"),
		)

		viewMenu = wx.Menu()
		viewFilters: tuple[tuple[ClipboardItemType | None, str], ...] = (
			# Translators: View filter showing all clipboard entry types.
			(None, _("&All Types")),
			# Translators: View filter showing plain-text clipboard entries.
			(ClipboardItemType.PLAIN_TEXT, _("&Plain Text")),
			# Translators: View filter showing formatted-text clipboard entries.
			(ClipboardItemType.FORMATTED_TEXT, _("&Formatted Text")),
			# Translators: View filter showing image-only clipboard entries.
			(ClipboardItemType.IMAGE, _("&Image")),
			# Translators: View filter showing text-and-image clipboard entries.
			(ClipboardItemType.TEXT_AND_IMAGE, _("Text and I&mage")),
			# Translators: View filter showing file-group clipboard entries.
			(ClipboardItemType.FILES, _("File &Groups")),
		)
		for itemType, label in viewFilters:
			item = viewMenu.AppendRadioItem(wx.ID_ANY, label)
			self._viewFilterItems[itemType] = item
			self.Bind(wx.EVT_MENU, self._onViewFilterChanged, item)
		self._viewFilterItems[None].Check(True)
		menuBar.Append(
			viewMenu,
			# Translators: Name of the View menu.
			_("&View"),
		)

		cloudMenu = wx.Menu()
		self.sendToTiantanItem: wx.MenuItem | None = None
		self.receiveFromTiantanItem: wx.MenuItem | None = None
		self.autoSyncTiantanItem: wx.MenuItem | None = None
		cloudSync = self.controller.cloudSync
		if cloudSync is not None:
			cloudItem = cloudMenu.Append(
				wx.ID_ANY,
				# Translators: Menu command to open the Tiantan Cloud Clipboard account dialog.
				_("&Tiantan Cloud Clipboard..."),
			)
			self.Bind(wx.EVT_MENU, self._onCloudSync, cloudItem)
			self.autoSyncTiantanItem = cloudMenu.AppendCheckItem(
				wx.ID_ANY,
				# Translators: Checkable menu command to automatically send clipboard text to Tiantan Cloud Clipboard.
				_("&Auto-sync Tiantan Cloud Clipboard"),
			)
			self.sendToTiantanItem = cloudMenu.Append(
				wx.ID_ANY,
				# Translators: Menu command to send current clipboard text to Tiantan Cloud Clipboard.
				_("&Send to Tiantan Cloud Clipboard"),
			)
			self.receiveFromTiantanItem = cloudMenu.Append(
				wx.ID_ANY,
				# Translators: Menu command to receive Tiantan Cloud Clipboard text into the system clipboard.
				_("&Receive from Tiantan Cloud Clipboard"),
			)
			cloudMenu.AppendSeparator()
			self.Bind(wx.EVT_MENU, self._onAutoSyncTiantan, self.autoSyncTiantanItem)
			self.Bind(wx.EVT_MENU, self._onSendToTiantan, self.sendToTiantanItem)
			self.Bind(wx.EVT_MENU, self._onReceiveFromTiantan, self.receiveFromTiantanItem)
		oneDriveItem = cloudMenu.Append(
			wx.ID_ANY,
			# Translators: Menu command to open the OneDrive account dialog.
			_("&OneDrive..."),
		)
		self.syncOneDriveItem = cloudMenu.Append(
			wx.ID_ANY,
			# Translators: Menu command to synchronize with OneDrive immediately.
			_("S&ynchronize OneDrive"),
		)
		menuBar.Append(
			cloudMenu,
			# Translators: Name of the Cloud menu.
			_("Clo&ud"),
		)
		self.Bind(wx.EVT_MENU, self._onOneDriveSync, oneDriveItem)
		self.Bind(wx.EVT_MENU, self._onSyncOneDrive, self.syncOneDriveItem)
		self.refreshCloudMenuState()

		self.SetMenuBar(menuBar)
		self.Bind(wx.EVT_MENU, self._onNewEntry, self._newItem)
		self.Bind(wx.EVT_MENU, self._onOpenFile, self._openItem)
		self.Bind(wx.EVT_MENU, self._onSaveAs, self.saveAsItem)
		self.Bind(wx.EVT_MENU, self._onReplaceClipboardWithText, self.replaceClipboardItem)
		self.Bind(wx.EVT_MENU, self._onSavePlainTextToCategory, self.savePlainTextToCategoryItem)
		self.Bind(wx.EVT_MENU, self._onExit, exitItem)
		self.Bind(wx.EVT_MENU, self._onFind, self.findItem)
		self.Bind(wx.EVT_MENU, self._onContinueFind, self.continueFindItem)
		self.Bind(wx.EVT_MENU, self._onPreviousFind, self.previousFindItem)
		self.Bind(wx.EVT_MENU, self._onReplace, self.replaceItem)
		self.Bind(wx.EVT_MENU, self._onGotoLine, self.gotoLineItem)

	def showManager(self, preferredCategory: CategoryId, preferredIndex: int) -> None:
		"""Show the manager at the global navigation position with focus on content."""
		if not self.IsShown():
			self._resetSearchState(clearEntries=True)
			try:
				self._selectedCategory = preferredCategory
				self._activeItemKey = None
				self._isDirty = False
				self._dirtyStateNeedsCheck = False
				self._itemTypeFilter = None
				self._viewFilterItems[None].Check(True)
				self._refreshCategories(preferredCategory)
				self._reloadItemsFromController(preferredIndex=preferredIndex, selectedKeys=())
				self._loadActiveItem(confirmDirty=False)
				self._restoreEditorOffsetFromClipboardNavigation()
			except Exception as error:
				self._showError(error)
		if self.IsIconized():
			self.Iconize(False)
		self.Show()
		self.Raise()
		self.editor.SetFocus()

	def terminate(self) -> None:
		"""Stop background search work before destroying the manager."""
		destroyWindow = not self._isBeingDestroyed
		self._isBeingDestroyed = True
		self._cancelPendingItemLoad()
		self._cancelPendingSearch()
		self._cancelSearchScan()
		self._searchKeywords = ()
		self._searchExecutor.shutdown(wait=True, cancel_futures=True)
		if destroyWindow:
			self.Destroy()

	def refreshFromController(
		self,
		preferredHistoryItemId: int | None = None,
		expectedText: str | None = None,
	) -> None:
		"""Refresh stored summaries while preserving the visible selection."""
		try:
			previousCategory = self._selectedCategory
			previousActiveKey, previousActiveIndex, previousSelectedKeys = self._getSearchListState()
			wasDirty = self._hasDirtyChanges()
			wasDraft = self._contentEditable and self._contentItemKey is None
			isPendingHistorySave = (
				wasDraft
				and not wasDirty
				and expectedText is not None
				and self._contentSourceText == expectedText
				and self._baselineText == expectedText
				and self.editor.GetValue() == expectedText
			)
			preservedEditorOffset: int | None = None
			preservedEditorText: str | None = None
			if self._contentEditable and self._contentItemKey is not None and not wasDirty:
				preservedEditorText = self._contentSourceText
				preservedEditorOffset = self._getEditorCodePointOffset()
			self._refreshCategories(previousCategory)
			category = self._getSelectedCategory()
			if self._isSearchSessionActive and category != previousCategory:
				self._resetSearchState()
			self._reloadItemsFromController(
				preferredKey=previousActiveKey if category == previousCategory else None,
				preferredIndex=previousActiveIndex if category == previousCategory else None,
				selectedKeys=previousSelectedKeys if category == previousCategory else (),
			)
			if (
				isPendingHistorySave
				and category == previousCategory
				and self.controller.isHistoryCategory(category)
				and preferredHistoryItemId is not None
				and preferredHistoryItemId in self._itemKeys
			):
				self._reloadItemsFromController(preferredKey=preferredHistoryItemId)
				self._loadActiveItem(confirmDirty=False)
			elif wasDraft and category == previousCategory:
				self._contentActiveKey = self._getActiveItemKey()
				self._updateUiState()
			elif not wasDirty:
				if self._isSearchSessionActive:
					self._loadActiveSearchResult()
				else:
					self._loadActiveItem(confirmDirty=False)
					if (
						preservedEditorOffset is not None
						and preservedEditorText is not None
						and self._contentSourceText == preservedEditorText
					):
						self._setEditorCodePointOffset(preservedEditorOffset)
			else:
				self._updateUiState()
		except Exception as error:
			self._showError(error)

	def refreshCloudMenuState(self) -> None:
		"""Enable Cloud menu actions supported by the current account states."""
		cloudSync = self.controller.cloudSync
		if cloudSync is not None:
			cloudState = cloudSync.getState()
			if self.autoSyncTiantanItem is not None:
				self.autoSyncTiantanItem.Check(cloudState.isAutoSyncEnabled)
				self.autoSyncTiantanItem.Enable(cloudState.isAvailable)
			if self.sendToTiantanItem is not None:
				self.sendToTiantanItem.Enable(
					cloudState.isAvailable
					and cloudState.isLoggedIn
					and not cloudState.isManualSendInProgress,
				)
			if self.receiveFromTiantanItem is not None:
				self.receiveFromTiantanItem.Enable(cloudState.isAvailable and cloudState.isLoggedIn)
		oneDriveState = self.controller.oneDriveSync.getState()
		self.syncOneDriveItem.Enable(
			oneDriveState.isAvailable
			and oneDriveState.isLoggedIn
			and not oneDriveState.isBusy
			and not oneDriveState.isSigningOut,
		)

	def _getEditorCodePointOffset(self, text: str | None = None) -> int:
		"""Return the wx insertion point as a Python string offset."""
		if text is None:
			text = self.editor.GetValue()
		offset = self.editor.GetInsertionPoint()
		return textUtils.WideStringOffsetConverter(text).encodedToStrOffsets(offset, offset)[0]

	def _setEditorCodePointOffset(self, offset: int) -> None:
		"""Set and reveal the wx insertion point from a Python string offset."""
		text = self.editor.GetValue()
		encodedOffset = textUtils.WideStringOffsetConverter(text).strToEncodedOffsets(offset, offset)[0]
		self.editor.SetInsertionPoint(encodedOffset)
		self.editor.ShowPosition(encodedOffset)

	def _restoreEditorOffsetFromClipboardNavigation(self) -> None:
		"""Use the current clipboard navigation offset as the initial editor position."""
		offset = self.controller.getCurrentNavigationOffsetForText(self._contentSourceText)
		if offset is not None:
			self._setEditorCodePointOffset(_sourceToEditorOffset(self._contentSourceText, offset))

	def _refreshCategories(self, preferredCategory: CategoryId | None = None) -> None:
		"""Reload categories and select a stable preferred category."""
		categories = list(self.controller.getCategories())
		self._categoryIds = categories
		self.categoryList.Set([self.controller.getCategoryLabel(category) for category in categories])
		self.categoryList.GetParent().Layout()
		self.categoryList.Enable(bool(categories) and not self._isSearchSessionActive)
		if not categories:
			self.categoryList.SetSelection(wx.NOT_FOUND)
			self._selectedCategory = None
			return
		if preferredCategory in categories:
			selection = categories.index(preferredCategory)
		elif self._selectedCategory in categories:
			selection = categories.index(self._selectedCategory)
		else:
			selection = next(
				(
					index
					for index, category in enumerate(categories)
					if self.controller.isHistoryCategory(category)
				),
				0,
			)
		self.categoryList.SetSelection(selection)
		self._selectedCategory = categories[selection]

	def _reloadItemsFromController(
		self,
		preferredKey: int | None = None,
		preferredIndex: int | None = None,
		selectedKeys: tuple[int, ...] = (),
	) -> int | None:
		"""Reload category summaries and restart an affected active search."""
		category = self._getSelectedCategory()
		entries = self.controller.getItems(category) if category is not None else ()
		entriesChanged = entries != self._allEntries
		self._allEntries = entries
		self._categoryHasItems = bool(self._allEntries)
		activeIndex = self._refreshItems(
			preferredKey=preferredKey,
			preferredIndex=preferredIndex,
			selectedKeys=selectedKeys,
		)
		if entriesChanged and self._searchKeywords:
			self._startSearchScan(
				preferredKey=preferredKey,
				preferredIndex=preferredIndex,
				selectedKeys=selectedKeys,
				clearResults=False,
			)
		return activeIndex

	def _getItemText(self, itemIndex: int, columnIndex: int) -> str:
		"""Return text for one visible row in the virtual entry list."""
		return (
			self._itemLabels[itemIndex] if columnIndex == 0 and 0 <= itemIndex < len(self._itemLabels) else ""
		)

	def _refreshItems(
		self,
		preferredKey: int | None = None,
		preferredIndex: int | None = None,
		selectedKeys: tuple[int, ...] = (),
	) -> int | None:
		"""Apply local type and keyword filters while restoring stable list state."""
		self._cancelPendingItemLoad()
		category = self._getSelectedCategory()
		entries = tuple(
			entry
			for entry in self._allEntries
			if (self._itemTypeFilter is None or entry[2] == self._itemTypeFilter)
			and (
				not self._searchKeywords
				or (self._matchingSearchKeys is not None and entry[0] in self._matchingSearchKeys)
			)
		)
		itemLabels = tuple(label for _itemId, label, _kind in entries)
		itemKeys = tuple(itemId for itemId, _label, _kind in entries)
		itemKinds = tuple(kind for _itemId, _label, kind in entries)
		self._isRefreshingList = True
		try:
			self._itemLabels = itemLabels
			self._itemKeys = itemKeys
			self._itemKinds = itemKinds
			self.itemList.SetItemCount(len(itemLabels))
			self.itemList.SetItemState(-1, 0, wx.LIST_STATE_SELECTED)
			self.itemList.SetItemState(-1, 0, wx.LIST_STATE_FOCUSED)
			selectedKeySet = set(selectedKeys)
			selectedIndices = [index for index, key in enumerate(itemKeys) if key in selectedKeySet]
			activeIndex = self._chooseActiveItemIndex(
				itemKeys,
				selectedIndices,
				preferredKey,
				preferredIndex,
				preferFirstResult=bool(self._searchKeywords),
			)
			for index in selectedIndices:
				self.itemList.Select(index)
			if activeIndex is not None:
				if not selectedIndices:
					self.itemList.Select(activeIndex)
				self.itemList.Focus(activeIndex)
			self.itemList.Refresh()
		finally:
			self._isRefreshingList = False
		if activeIndex is None:
			self._activeItemIndex = None
			self._activeItemKey = None
			if not self._isSearchSessionActive and not self._hasDirtyChanges():
				self._clearContent(category)
			else:
				self._updateUiState()
			self._updateItemsLabel(len(entries))
			return None
		self._activeItemIndex = activeIndex
		self._activeItemKey = itemKeys[activeIndex]
		self._updateItemsLabel(len(entries))
		self._updateUiState()
		return activeIndex

	@staticmethod
	def _chooseActiveItemIndex(
		itemKeys: tuple[int, ...],
		selectedIndices: list[int],
		preferredKey: int | None,
		preferredIndex: int | None,
		*,
		preferFirstResult: bool,
	) -> int | None:
		"""Choose the stable active row used by list refresh and search exit."""
		if preferredKey is not None and preferredKey in itemKeys:
			return itemKeys.index(preferredKey)
		if preferFirstResult and itemKeys:
			return 0
		if preferredKey is not None and selectedIndices:
			return selectedIndices[0]
		if preferredIndex is not None and itemKeys:
			return min(max(preferredIndex, 0), len(itemKeys) - 1)
		return 0 if itemKeys else None

	def _getCurrentListState(self) -> tuple[int | None, int | None, tuple[int, ...]]:
		"""Return the active key, active index, and selected keys."""
		return (self._getActiveItemKey(), self._getActiveItemIndex(), self._getSelectedItemKeys())

	def _getSearchListState(self) -> tuple[int | None, int | None, tuple[int, ...]]:
		"""Return stable list state, including state hidden by an in-progress search."""
		if self._isSearchSessionActive:
			if self._pendingSearchListState is not None:
				return self._pendingSearchListState
			if self._matchingSearchKeys is None:
				return self._searchRestoreListState
		return self._getCurrentListState()

	def _cancelSearchScan(self) -> None:
		"""Cancel background search work and invalidate its queued completion."""
		cancelEvent = self._searchCancelEvent
		self._searchCancelEvent = None
		if cancelEvent is not None:
			cancelEvent.set()
		future = self._searchFuture
		self._searchFuture = None
		if future is not None:
			future.cancel()

	def _startSearchScan(
		self,
		*,
		preferredKey: int | None,
		preferredIndex: int | None,
		selectedKeys: tuple[int, ...],
		clearResults: bool,
	) -> None:
		"""Start one cancellable payload-free search outside the wx thread."""
		category = self._getSelectedCategory()
		if category is None or not self._searchKeywords:
			return
		self._cancelSearchScan()
		self._pendingSearchListState = (preferredKey, preferredIndex, selectedKeys)
		if clearResults:
			self._matchingSearchKeys = None
			self._refreshItems(
				preferredKey=preferredKey,
				preferredIndex=preferredIndex,
				selectedKeys=selectedKeys,
			)
		cancelEvent = Event()
		self._searchCancelEvent = cancelEvent
		future = self._searchExecutor.submit(
			self.controller.getSearchResultKeys,
			category,
			self._allEntries,
			self._searchKeywords,
			cancelEvent,
		)
		self._searchFuture = future
		future.add_done_callback(self._queueSearchScanCompletion)

	def _queueSearchScanCompletion(
		self,
		future: Future[tuple[int, ...]],
	) -> None:
		"""Queue one live worker completion for the wx thread."""
		if self._isBeingDestroyed or future is not self._searchFuture:
			return
		try:
			wx.CallAfter(self._finishSearchScan, future)
		except RuntimeError:
			if self._isBeingDestroyed or future is not self._searchFuture:
				return
			self._searchFuture = None
			self._searchCancelEvent = None
			self._pendingSearchListState = None
			self._enterSearchResultsWhenReady = False
			log.debugWarning("Could not schedule a clipboard manager search result.", exc_info=True)

	def _finishSearchScan(
		self,
		future: Future[tuple[int, ...]],
	) -> None:
		"""Apply one current background result on the wx thread."""
		if self._isBeingDestroyed or future is not self._searchFuture:
			return
		self._searchFuture = None
		self._searchCancelEvent = None
		if self._searchTimer is not None:
			currentKeywords = splitSearchKeywords(normalizeSearchText(self.searchCtrl.GetValue()))
			if currentKeywords != self._searchKeywords:
				return
		try:
			matchingKeys = future.result()
		except Exception as error:
			self._matchingSearchKeys = None
			self._enterSearchResultsWhenReady = False
			self._refreshItems()
			self._showError(error)
			self.searchCtrl.SetFocus()
			return
		pendingState = self._pendingSearchListState
		assert pendingState is not None
		self._matchingSearchKeys = frozenset(matchingKeys)
		preferredKey, preferredIndex, selectedKeys = pendingState
		self._pendingSearchListState = None
		self._refreshItems(
			preferredKey=preferredKey,
			preferredIndex=preferredIndex,
			selectedKeys=selectedKeys,
		)
		if self._enterSearchResultsWhenReady:
			self._enterSearchResultsWhenReady = False
			self._moveIntoSearchResults()
		else:
			self._loadActiveSearchResult()

	def _updateItemsLabel(self, resultCount: int) -> None:
		"""Show passive search status and the completed result count."""
		if self._searchKeywords and self._pendingSearchListState is not None:
			# Translators: Label shown while clipboard manager search results are being updated.
			label = _("Search resul&ts:")
		elif self._searchKeywords:
			label = ngettext(
				# Translators: Label for one or multiple clipboard manager search results.
				"Search resul&t ({count}):",
				"Search resul&ts ({count}):",
				resultCount,
			).format(count=resultCount)
		else:
			# Translators: Label for the stored clipboard entry list.
			label = _("En&tries:")
		if label != self.itemsLabel.GetLabel():
			self.itemsLabel.SetLabel(label)
			self.itemsLabel.GetParent().Layout()

	def _cancelPendingSearch(self) -> None:
		"""Cancel a pending debounced search update."""
		timer = self._searchTimer
		self._searchTimer = None
		if timer is not None:
			timer.Stop()

	def _resetSearchState(self, *, clearEntries: bool = False) -> None:
		"""Clear all query, worker, timer, and restoration state without moving focus."""
		self._cancelPendingSearch()
		self._cancelSearchScan()
		self.searchCtrl.ChangeValue("")
		self._searchKeywords = ()
		self._matchingSearchKeys = None
		self._pendingSearchListState = None
		self._enterSearchResultsWhenReady = False
		self._isSearchSessionActive = False
		self._searchRestoreListState = (None, None, ())
		self.editor.SetEditable(self._contentEditable and self._isContentCurrent())
		self.clearSearchButton.Disable()
		self.categoryList.Enable(bool(self._categoryIds))
		self._updateItemsLabel(0)
		if clearEntries:
			self._allEntries = ()

	def _beginSearchSession(self) -> bool:
		"""Reject drafts or edits and capture state before filtering."""
		if self._isSearchSessionActive:
			return True
		if self._contentEditable and (self._contentItemKey is None or self._hasDirtyChanges()):
			return False
		self._searchRestoreListState = self._getCurrentListState()
		self._isSearchSessionActive = True
		self.categoryList.Disable()
		self.editor.SetEditable(False)
		self._updateUiState()
		return True

	def _applySearchQuery(self) -> bool:
		"""Start filtering the current query without moving keyboard focus."""
		self._cancelPendingSearch()
		queryValue = self.searchCtrl.GetValue()
		keywords = splitSearchKeywords(normalizeSearchText(queryValue))
		if not keywords:
			return self._exitSearch()
		if not self._beginSearchSession():
			self.searchCtrl.ChangeValue("")
			self.clearSearchButton.Disable()
			return False
		keywordsChanged = keywords != self._searchKeywords
		previousKey, previousIndex, selectedKeys = self._getSearchListState()
		if not keywordsChanged:
			if self._searchFuture is None and (
				self._matchingSearchKeys is None or self._pendingSearchListState is not None
			):
				self._startSearchScan(
					preferredKey=previousKey,
					preferredIndex=previousIndex,
					selectedKeys=selectedKeys,
					clearResults=self._matchingSearchKeys is None,
				)
			self.clearSearchButton.Enable()
			return True
		self._searchKeywords = keywords
		self.clearSearchButton.Enable()
		self._startSearchScan(
			preferredKey=previousKey,
			preferredIndex=previousIndex,
			selectedKeys=selectedKeys,
			clearResults=True,
		)
		return True

	def _runScheduledSearch(self) -> None:
		"""Run one debounced search update and report storage failures."""
		self._searchTimer = None
		try:
			self._applySearchQuery()
		except Exception as error:
			self._showError(error)
			self.searchCtrl.SetFocus()

	def _exitSearch(self) -> bool:
		"""Exit search and restore the list state captured before searching."""
		self._enterSearchResultsWhenReady = False
		self._cancelPendingSearch()
		self._cancelSearchScan()
		if self._isSearchSessionActive:
			preferredKey, preferredIndex, selectedKeys = self._searchRestoreListState
		else:
			preferredKey, preferredIndex, selectedKeys = self._getCurrentListState()
		category = self._getSelectedCategory()
		self._resetSearchState()
		self._refreshItems(
			preferredKey=preferredKey,
			preferredIndex=preferredIndex,
			selectedKeys=selectedKeys,
		)
		self.editor.SetEditable(self._contentEditable and self._isContentCurrent())
		if not self._isContentCurrent():
			try:
				self._loadActiveItem(confirmDirty=False)
			except Exception:
				self._clearContent(category)
				self.itemList.SetFocus()
				raise
		self.itemList.SetFocus()
		return True

	def _enterSearchResults(self) -> None:
		"""Move from search to the best matching result and preview it."""
		if not self._applySearchQuery():
			return
		if not self._searchKeywords:
			return
		if self._searchFuture is not None or self._matchingSearchKeys is None:
			self._enterSearchResultsWhenReady = True
			self.searchCtrl.SetFocus()
			return
		self._moveIntoSearchResults()

	def _moveIntoSearchResults(self) -> None:
		"""Focus and load the preferred result from a completed search."""
		if not self._itemKeys:
			# Translators: Message reported when a clipboard manager search has no results.
			ui.message(_("No matching entries."))
			self.searchCtrl.SetFocus()
			return
		preferredKey = self._getActiveItemKey()
		if preferredKey is None:
			preferredKey = self._searchRestoreListState[0]
		targetKey = preferredKey if preferredKey in self._itemKeys else self._itemKeys[0]
		targetIndex = self._itemKeys.index(targetKey)
		self._isRefreshingList = True
		try:
			self.itemList.Select(targetIndex)
			self.itemList.Focus(targetIndex)
		finally:
			self._isRefreshingList = False
		self._activeItemIndex = targetIndex
		self._activeItemKey = targetKey
		if not self._isStoredContentCurrent() and not self._loadActiveItem(confirmDirty=False):
			self.searchCtrl.SetFocus()
			return
		self.itemList.SetFocus()
		self._updateUiState()

	def _onSearchTextChanged(self, event: wx.CommandEvent) -> None:
		"""Debounce live search after the query text changes."""
		self._enterSearchResultsWhenReady = False
		queryValue = self.searchCtrl.GetValue()
		if queryValue and not self._isSearchSessionActive:
			keywords = splitSearchKeywords(normalizeSearchText(queryValue))
			if keywords and not self._beginSearchSession():
				self._cancelPendingSearch()
				self.searchCtrl.ChangeValue("")
				self.clearSearchButton.Disable()
				event.Skip()
				return
		self.clearSearchButton.Enable(bool(queryValue) or self._isSearchSessionActive)
		if self._searchTimer is None:
			self._searchTimer = wx.CallLater(_SEARCH_DELAY_MS, self._runScheduledSearch)
		else:
			self._searchTimer.Restart(_SEARCH_DELAY_MS)
		event.Skip()

	def _onClearSearch(self, event: wx.CommandEvent) -> None:
		"""Exit search from the clear button."""
		try:
			self._exitSearch()
		except Exception as error:
			self._showError(error)

	def _prepareSearchListInteraction(self, *, enterWhenReady: bool = False) -> bool:
		"""Flush debounce and reject list actions until current results are ready."""
		try:
			if self._searchTimer is not None and not self._applySearchQuery():
				return False
			if (
				self._isSearchSessionActive
				and self._searchFuture is None
				and self._matchingSearchKeys is None
				and not self._applySearchQuery()
			):
				return False
		except Exception as error:
			self._showError(error)
			self.searchCtrl.SetFocus()
			return False
		if not self._isSearchSessionActive:
			return True
		if self._searchFuture is not None or self._matchingSearchKeys is None:
			if enterWhenReady:
				self._enterSearchResultsWhenReady = True
			self.searchCtrl.SetFocus()
			return False
		return True

	def _onItemListSetFocus(self, event: wx.FocusEvent) -> None:
		"""Load the focused search result even when its row focus did not change."""
		event.Skip()
		if self._isSearchSessionActive and self._prepareSearchListInteraction(enterWhenReady=True):
			wx.CallAfter(self._loadActiveSearchResult, requireListFocus=True)

	def _loadActiveSearchResult(self, *, requireListFocus: bool = False) -> None:
		"""Load the active search result when completed results are available."""
		if (
			self._isBeingDestroyed
			or not self._isSearchSessionActive
			or self._searchFuture is not None
			or self._matchingSearchKeys is None
			or (requireListFocus and self.FindFocus() is not self.itemList)
		):
			return
		category = self._getSelectedCategory()
		index = self._getActiveItemIndex()
		key = self._getActiveItemKey()
		if category is None:
			return
		if index is None or key is None:
			self._clearContent(category)
			return
		try:
			if not self._isStoredContentCurrent() and not self._loadActiveItem(confirmDirty=False):
				self.searchCtrl.SetFocus()
				return
		except Exception as error:
			self._showError(error)
			self.searchCtrl.SetFocus()
			return
		self._activeItemIndex = self._getActiveItemIndex()
		self._activeItemKey = self._getActiveItemKey()
		self._updateUiState()

	def _loadActiveItem(self, *, confirmDirty: bool = True) -> bool:
		"""Load the active stored item into the content control."""
		self._cancelPendingItemLoad()
		category = self._getSelectedCategory()
		index = self._getActiveItemIndex()
		key = self._getActiveItemKey()
		if category is None or index is None or key is None:
			self._clearContent(category)
			return True
		previousIndex = self._activeItemIndex
		previousKey = self._contentActiveKey
		selectedKeys = self._getSelectedItemKeys()
		wasDirty = self._hasDirtyChanges() if confirmDirty else False
		if confirmDirty:
			if not self._confirmDirtyChanges():
				self._restoreActiveItem(previousIndex, previousKey)
				return False
			if wasDirty:
				self._reloadItemsFromController(
					preferredKey=key,
					preferredIndex=index,
					selectedKeys=selectedKeys,
				)
				index = self._getActiveItemIndex()
				key = self._getActiveItemKey()
				if index is None or key is None:
					self._clearContent(category)
					return True
		try:
			details = self.controller.getItemDetails(category, key)
		except ItemNotFoundError:
			self._reloadItemsFromController(preferredIndex=index, selectedKeys=selectedKeys)
			category = self._getSelectedCategory()
			index = self._getActiveItemIndex()
			key = self._getActiveItemKey()
			if category is None or index is None or key is None:
				self._clearContent(category)
				return True
			details = self.controller.getItemDetails(category, key)
		if details.key != key:
			# Translators: Error shown when a selected clipboard item changed unexpectedly.
			raise RuntimeError(_("The selected clipboard entry changed before it could be loaded."))
		self._selectedCategory = category
		self._activeItemIndex = index
		self._activeItemKey = key
		self._setContent(
			details.content,
			isEditable=details.isEditable,
			category=category,
			itemKey=key,
			kind=details.kind,
			hasImage=details.hasImage,
			canUpload=details.canUpload,
		)
		return True

	def _setContent(
		self,
		text: str,
		*,
		isEditable: bool,
		category: CategoryId | None,
		itemKey: int | None,
		kind: ClipboardItemType | None,
		hasImage: bool,
		canUpload: bool,
		isDraft: bool = False,
	) -> None:
		"""Replace content silently and record its source and capabilities."""
		self._contentEditable = isEditable
		self._contentCategory = category
		self._contentItemKey = itemKey
		self._contentActiveKey = self._getActiveItemKey()
		self._contentKind = kind
		self._contentHasImage = hasImage
		self._contentCanUpload = canUpload
		contentKind = self._getContentKindLabel(kind, isDraft)
		self._isSettingContent = True
		try:
			self.editor.SetEditable(True)
			self.editor.ChangeValue(text)
			self.editor.SetEditable(isEditable and not self._isSearchSessionActive)
			self.editor.SetName(contentKind)
			self.contentLabel.SetLabel(
				# Translators: Dynamic label for manager content. ``kind`` describes the selected content type.
				_("C&ontent ({kind}):").format(kind=contentKind),
			)
		finally:
			self._isSettingContent = False
		self._contentSourceText = text
		self._baselineText = self.editor.GetValue()
		self._isDirty = False
		self._dirtyStateNeedsCheck = False
		self.editor.SetInsertionPoint(0)
		self._updateUiState()

	def _startPlainTextDraft(self, text: str = "") -> None:
		"""Start an editable plain-text draft in the selected category."""
		self._setContent(
			text,
			isEditable=True,
			category=self._getSelectedCategory(),
			itemKey=None,
			kind=ClipboardItemType.PLAIN_TEXT,
			hasImage=False,
			canUpload=True,
			isDraft=True,
		)
		self._baselineText = ""
		self._isDirty = bool(text)
		self._dirtyStateNeedsCheck = False
		self._updateUiState()

	def _getContentKindLabel(
		self,
		kind: ClipboardItemType | None,
		isDraft: bool,
	) -> str:
		"""Return the concise visible and accessible kind of manager content."""
		if isDraft:
			# Translators: Kind of a plain-text draft in the manager.
			return _("Plain-text draft")
		if kind == ClipboardItemType.PLAIN_TEXT:
			# Translators: Kind of stored plain text in the manager.
			return _("Plain text")
		if kind == ClipboardItemType.FORMATTED_TEXT:
			# Translators: Kind of stored formatted text in the manager.
			return _("Formatted text")
		if kind == ClipboardItemType.TEXT_AND_IMAGE:
			# Translators: Kind of stored text-and-image content in the manager.
			return _("Text and image")
		if kind == ClipboardItemType.IMAGE:
			# Translators: Kind of a stored image summary in the manager.
			return _("Image summary")
		if kind == ClipboardItemType.FILES:
			# Translators: Kind of a stored file group in the manager.
			return _("File group")
		# Translators: Kind of an empty manager content control.
		return _("Empty")

	def _clearContent(self, category: CategoryId | None) -> None:
		"""Show an empty read-only content control for an empty category."""
		self._setContent(
			"",
			isEditable=False,
			category=category,
			itemKey=None,
			kind=None,
			hasImage=False,
			canUpload=True,
		)

	def _restoreActiveItem(self, index: int | None, key: int | None) -> None:
		"""Restore the active item as the sole selection after cancelled navigation."""
		self._cancelPendingItemLoad()
		if key is not None and key in self._itemKeys:
			index = self._itemKeys.index(key)
		elif index is None or index >= len(self._itemKeys):
			index = None
		self._isRefreshingList = True
		try:
			self.itemList.SetItemState(-1, 0, wx.LIST_STATE_SELECTED)
			self.itemList.SetItemState(-1, 0, wx.LIST_STATE_FOCUSED)
			if index is not None:
				self.itemList.Select(index)
				self.itemList.Focus(index)
		finally:
			self._isRefreshingList = False
		self._activeItemIndex = index
		self._activeItemKey = None if index is None else self._itemKeys[index]
		self._updateUiState()

	def _scheduleActiveItemLoad(self) -> None:
		"""Debounce expensive content loading while the active item is moving."""
		if self._itemLoadTimer is None:
			self._itemLoadTimer = wx.CallLater(_ITEM_LOAD_DELAY_MS, self._loadScheduledActiveItem)
		else:
			self._itemLoadTimer.Restart(_ITEM_LOAD_DELAY_MS)

	def _cancelPendingItemLoad(self) -> None:
		timer = self._itemLoadTimer
		self._itemLoadTimer = None
		if timer is not None:
			timer.Stop()

	def _loadScheduledActiveItem(self) -> None:
		"""Load the final active item after a burst of list navigation."""
		self._itemLoadTimer = None
		isSearchResult = self._isSearchSessionActive and self.FindFocus() is self.itemList
		try:
			if self._loadActiveItem(confirmDirty=not self._isSearchSessionActive) and isSearchResult:
				self._updateUiState()
		except Exception as error:
			self._showError(error)
			if isSearchResult:
				self.searchCtrl.SetFocus()

	def _hasDirtyChanges(self) -> bool:
		"""Return whether pending editor changes differ from the baseline text."""
		if self._dirtyStateNeedsCheck:
			self._isDirty = self.editor.GetValue() != self._baselineText
			self._dirtyStateNeedsCheck = False
		return self._isDirty

	def _onContentChanged(self, event: wx.CommandEvent) -> None:
		"""Mark editable content for a deferred baseline comparison."""
		if not self._isSettingContent and self._contentEditable and not self._isSearchSessionActive:
			self._isDirty = True
			self._dirtyStateNeedsCheck = True
			self._updateUiState()
		event.Skip()

	def _markContentSaved(self, text: str) -> None:
		"""Record already-read editor text as the saved baseline."""
		self._baselineText = text
		self._isDirty = False
		self._dirtyStateNeedsCheck = False
		self._updateUiState()

	def _markSystemClipboardTextSaved(self, text: str) -> None:
		"""Keep saved clipboard text visible until the asynchronous history refresh catches up."""
		self._contentSourceText = text
		self._contentItemKey = None

	def _getSaveTargetLabel(self) -> str:
		"""Return the current save target label for menu commands."""
		category = self._getSelectedCategory()
		if category is None or self.controller.isHistoryCategory(category):
			# Translators: Label for the system clipboard target in save commands.
			return _("System Clipboard")
		return self.controller.getCategoryLabel(category)

	def _formatMenuLabel(self, text: str) -> str:
		"""Normalize user-facing menu text so control characters cannot break it."""
		text = re.sub(r"[\x00-\x1F\x7F]+", " ", text).strip()
		return text.replace("&", "&&")

	def _updateSaveMenuLabels(self) -> None:
		"""Update save command labels to match the selected category."""
		targetLabel = self._formatMenuLabel(self._getSaveTargetLabel())
		self.replaceClipboardItem.SetItemLabel(
			_("Save to {target}\tCtrl+S").format(target=targetLabel),
		)
		self.savePlainTextToCategoryItem.SetItemLabel(
			_("Save to {target} and Close Window\tCtrl+Shift+X").format(target=targetLabel),
		)

	def _saveVisibleContent(self, *, closeAfterSave: bool) -> bool:
		"""Save the current editor text, optionally closing the manager."""
		if self._isSearchSessionActive or not self._isContentCurrent() or not self._contentEditable:
			return False
		category = self._getSelectedCategory()
		if category is None:
			return False
		try:
			text = self.editor.GetValue()
			if self.controller.isHistoryCategory(category):
				if self.controller.replaceClipboardWithText(text, canUpload=self._contentCanUpload) is False:
					return False
				self._markSystemClipboardTextSaved(text)
			else:
				self.controller.savePlainTextToCategory(
					category,
					text,
					canUpload=self._contentCanUpload,
					notifyManager=False,
				)
			self._markContentSaved(text)
			if closeAfterSave:
				self.Close()
				return True
			isHistory = self.controller.isHistoryCategory(category)
			if not isHistory:
				self._reloadItemsFromController(preferredIndex=0, selectedKeys=())
				self._loadActiveItem(confirmDirty=False)
		except Exception as error:
			self._showError(error)
			return False
		return True

	def _confirmDirtyChanges(self, category: CategoryId | None = None) -> bool:
		"""Save, discard, or retain pending plain-text changes."""
		if not self._hasDirtyChanges():
			return True
		category = category if category is not None else self._contentCategory
		isHistory = category is None or self.controller.isHistoryCategory(category)
		if isHistory:
			# Translators: Button that puts modified text on the system clipboard.
			saveLabel = _("Put on Clipboard")
		else:
			# Translators: Button that saves modified text in the selected user category.
			saveLabel = _("Save to Current Category")
		dialog = wx.MessageDialog(
			self,
			# Translators: Prompt shown before leaving modified plain text.
			_("The content has unsaved text changes. Choose an action before continuing."),
			self.GetTitle(),
			wx.YES_NO | wx.CANCEL | wx.CANCEL_DEFAULT | wx.ICON_QUESTION,
		)
		dialog.SetYesNoCancelLabels(
			saveLabel,
			# Translators: Button that discards modified clipboard manager text.
			_("Discard"),
			# Translators: Button that cancels leaving modified clipboard manager text.
			_("Cancel"),
		)
		try:
			result = displayDialogAsModal(dialog)
		finally:
			dialog.Destroy()
		if result == wx.ID_CANCEL:
			return False
		if result == wx.ID_NO:
			self._isSettingContent = True
			try:
				self.editor.ChangeValue(self._baselineText)
			finally:
				self._isSettingContent = False
			self._isDirty = False
			self._dirtyStateNeedsCheck = False
			self.editor.SetInsertionPoint(0)
			self._updateUiState()
			return True
		return self._saveDirtyChanges(category)

	def _saveDirtyChanges(self, category: CategoryId | None) -> bool:
		"""Save modified plain text according to its category context."""
		text = self.editor.GetValue()
		try:
			if category is None or self.controller.isHistoryCategory(category):
				if self.controller.replaceClipboardWithText(text, canUpload=self._contentCanUpload) is False:
					return False
				self._markSystemClipboardTextSaved(text)
			else:
				# A later dialog or operation may be cancelled or fail, so this save must refresh independently.
				self.controller.savePlainTextToCategory(
					category,
					text,
					canUpload=self._contentCanUpload,
					notifyManager=True,
				)
		except Exception as error:
			self._showError(error)
			return False
		self._markContentSaved(text)
		return True

	def _getSelectedCategory(self) -> CategoryId | None:
		return self._selectedCategory

	def _getActiveItemIndex(self) -> int | None:
		"""Return the focused list row used for preview and single-item actions."""
		index = self.itemList.GetFocusedItem()
		return None if index == wx.NOT_FOUND else index

	def _getActiveItemKey(self) -> int | None:
		"""Return the stable key of the active list row."""
		index = self._getActiveItemIndex()
		if index is None or index >= len(self._itemKeys):
			return None
		return self._itemKeys[index]

	def _getSelectedItemKeys(self) -> tuple[int, ...]:
		"""Return selected stable keys in visible list order."""
		keys: list[int] = []
		index = self.itemList.GetFirstSelected()
		while index != wx.NOT_FOUND:
			if index < len(self._itemKeys):
				keys.append(self._itemKeys[index])
			index = self.itemList.GetNextSelected(index)
		return tuple(keys)

	def _getSelectedFileGroupKeys(self) -> tuple[int, ...]:
		"""Return selected file-group keys in visible list order."""
		keys: list[int] = []
		index = self.itemList.GetFirstSelected()
		while index != wx.NOT_FOUND:
			if index < len(self._itemKeys) and self._itemKinds[index] == ClipboardItemType.FILES:
				keys.append(self._itemKeys[index])
			index = self.itemList.GetNextSelected(index)
		return tuple(keys)

	def _getSurvivingNeighborKey(
		self,
		activeIndex: int,
		removedKeys: tuple[int, ...],
	) -> int | None:
		"""Return the next, or otherwise previous, visible key that survives removal."""
		removedKeySet = set(removedKeys)
		for key in self._itemKeys[activeIndex + 1 :]:
			if key not in removedKeySet:
				return key
		for key in reversed(self._itemKeys[:activeIndex]):
			if key not in removedKeySet:
				return key
		return None

	def _isContentCurrent(self) -> bool:
		return (
			self._contentCategory == self._getSelectedCategory()
			and self._contentActiveKey == self._getActiveItemKey()
		)

	def _isStoredContentCurrent(self) -> bool:
		"""Return whether the editor contains the active stored entry rather than a draft."""
		return (
			self._contentCategory == self._getSelectedCategory()
			and self._contentItemKey is not None
			and self._contentItemKey == self._getActiveItemKey()
		)

	def _updateUiState(self) -> None:
		"""Enable commands valid for the visible content and category."""
		category = self._getSelectedCategory()
		hasCategory = category is not None
		isCurrentContent = self._isContentCurrent()
		hasContent = isCurrentContent and self.editor.GetLastPosition() != 0
		hasTextContent = hasContent and self._contentKind != ClipboardItemType.IMAGE
		canEditContent = isCurrentContent and self._contentEditable and not self._isSearchSessionActive
		self._updateSaveMenuLabels()
		if self._newItem is not None:
			self._newItem.Enable(not self._isSearchSessionActive)
		if self._openItem is not None:
			self._openItem.Enable(not self._isSearchSessionActive)
		self.saveAsItem.Enable(hasTextContent)
		self.replaceClipboardItem.Enable(canEditContent)
		self.savePlainTextToCategoryItem.Enable(hasContent and canEditContent and hasCategory)
		self.findItem.Enable(hasTextContent)
		self.continueFindItem.Enable(hasTextContent)
		self.previousFindItem.Enable(hasTextContent)
		self.replaceItem.Enable(canEditContent)
		self.gotoLineItem.Enable(hasTextContent)
		if self._segmentChineseWordsItem is not None:
			self._segmentChineseWordsItem.Enable(canEditContent)
		if self._textCleanupMenuItem is not None:
			self._textCleanupMenuItem.Enable(canEditContent)
		if self._lineOperationsMenuItem is not None:
			self._lineOperationsMenuItem.Enable(canEditContent)

	def _showError(self, error: Exception) -> None:
		if not isinstance(error, (ValueError, re.error)):
			log.exception("Clipboard manager operation failed.", exc_info=error)
		message = str(error)
		if not message:
			# Translators: Generic clipboard manager error.
			message = _("The operation failed.")
		MessageDialog(
			parent=self,
			message=message,
			title=self.GetTitle(),
			dialogType=DialogType.ERROR,
		).ShowModal()

	def _showInfo(self, message: str) -> None:
		MessageDialog.alert(message, self.GetTitle(), parent=self)

	def _confirm(self, message: str) -> bool:
		dialog = MessageDialog(
			parent=self,
			message=message,
			title=self.GetTitle(),
			dialogType=DialogType.WARNING,
			buttons=(
				DefaultButton.YES,
				DefaultButton.NO.value._replace(defaultFocus=True, fallbackAction=True),
			),
		)
		return dialog.ShowModal() == ReturnCode.YES

	def _promptText(self, message: str, value: str = "") -> str | None:
		"""Prompt for one text value and return ``None`` when cancelled."""
		dialog = wx.TextEntryDialog(self, message, self.GetTitle(), value=value)
		try:
			if displayDialogAsModal(dialog) != wx.ID_OK:
				return None
			return dialog.GetValue().strip()
		finally:
			dialog.Destroy()

	def _onCategoryContextMenu(self, event: wx.ContextMenuEvent) -> None:
		"""Select the clicked category and show commands for it."""
		if getattr(self, "_isSearchSessionActive", False):
			return
		eventPosition = event.GetPosition()
		if eventPosition != wx.DefaultPosition:
			selection = self.categoryList.HitTest(self.categoryList.ScreenToClient(eventPosition))
			if selection != wx.NOT_FOUND and not self._selectCategory(selection):
				return
		menu = wx.Menu()
		newItem = menu.Append(
			wx.ID_ANY,
			# Translators: Context menu command to create a clipboard category.
			_("&New..."),
		)
		renameItem = menu.Append(
			wx.ID_ANY,
			# Translators: Context menu command to rename a clipboard category.
			_("&Rename..."),
		)
		deleteItem = menu.Append(
			wx.ID_ANY,
			# Translators: Context menu command to delete a clipboard category.
			_("&Delete"),
		)
		category = self._getSelectedCategory()
		canModifyCategory = category is not None and not self.controller.isHistoryCategory(category)
		renameItem.Enable(canModifyCategory)
		deleteItem.Enable(canModifyCategory and not self._categoryHasItems)
		menu.Bind(wx.EVT_MENU, self._onNewCategory, newItem)
		menu.Bind(wx.EVT_MENU, self._onRenameCategory, renameItem)
		menu.Bind(wx.EVT_MENU, self._onDeleteCategory, deleteItem)
		try:
			if eventPosition == wx.DefaultPosition:
				self.categoryList.PopupMenu(menu, pos=self.categoryList.GetClientRect().GetBottomLeft())
			else:
				self.categoryList.PopupMenu(menu)
		finally:
			menu.Destroy()

	def _onItemContextMenu(self, event: wx.ContextMenuEvent) -> None:
		"""Prepare the targeted entries and show their context menu."""
		if self._isSearchSessionActive and not self._prepareSearchListInteraction():
			return
		eventPosition = event.GetPosition()
		previousActiveKey = self._activeItemKey
		if not self._focusItemContextMenuTarget(eventPosition):
			return
		category = self._getSelectedCategory()
		index = self._getActiveItemIndex()
		key = self._getActiveItemKey()
		if category is None or index is None or key is None:
			return
		activeChanged = key != previousActiveKey
		if activeChanged:
			self._playActiveItemCue()
		try:
			needsLoad = activeChanged or (self._contentCategory, self._contentItemKey) != (category, key)
			if needsLoad:
				if not self._loadActiveItem(confirmDirty=not self._isSearchSessionActive):
					if self._isSearchSessionActive:
						self.searchCtrl.SetFocus()
					return
			else:
				self._cancelPendingItemLoad()
				self._updateUiState()
		except Exception as error:
			self._showError(error)
			return
		itemCount = self.itemList.GetSelectedItemCount()
		self._showItemContextMenu(eventPosition, index, itemCount)

	def _focusItemContextMenuTarget(self, eventPosition: wx.Point) -> bool:
		"""Focus the context-menu target while preserving an existing multi-selection."""
		activeIndex = self._getActiveItemIndex()
		if eventPosition == wx.DefaultPosition:
			itemIndex = activeIndex
		else:
			itemIndex, _flags = self.itemList.HitTest(self.itemList.ScreenToClient(eventPosition))
		if itemIndex is None or itemIndex == wx.NOT_FOUND:
			return False
		isSelected = bool(self.itemList.GetItemState(itemIndex, wx.LIST_STATE_SELECTED))
		collapseSelection = not isSelected
		focusChanged = itemIndex != activeIndex
		if collapseSelection or focusChanged:
			self._isRefreshingList = True
			try:
				if collapseSelection:
					self.itemList.SetItemState(-1, 0, wx.LIST_STATE_SELECTED)
					self.itemList.Select(itemIndex)
				self.itemList.Focus(itemIndex)
			finally:
				self._isRefreshingList = False
		return True

	def _showItemContextMenu(self, eventPosition: wx.Point, itemIndex: int, itemCount: int) -> None:
		"""Show commands for the selected entries at the requested position."""
		if not itemCount:
			return
		fileGroupKeys = self._getSelectedFileGroupKeys()
		menu = wx.Menu()
		if itemCount == 1:
			putItem = menu.Append(
				wx.ID_ANY,
				# Translators: Context menu command to put an entry on the clipboard.
				_("Put on Clipboa&rd"),
			)
			menu.Bind(wx.EVT_MENU, self._onPutItemOnClipboard, putItem)
		transferItem = menu.Append(
			wx.ID_ANY,
			# Translators: Context menu command to move or collect selected entries.
			_("&Move or Collect..."),
		)
		if fileGroupKeys:
			removeMissingFilesItem = menu.Append(
				wx.ID_ANY,
				# Translators: Context menu command to remove missing paths from selected file groups.
				_("Remove &Missing Files"),
			)
			menu.Bind(wx.EVT_MENU, self._onRemoveMissingFiles, removeMissingFilesItem)
		deleteItem = menu.Append(
			wx.ID_ANY,
			ngettext(
				# Translators: Context menu command to delete one or multiple clipboard entries.
				"De&lete Entry",
				"De&lete {count} Entries",
				itemCount,
			).format(count=itemCount),
		)
		menu.Bind(wx.EVT_MENU, self._onTransferItemsToCategory, transferItem)
		menu.Bind(wx.EVT_MENU, self._onDeleteItems, deleteItem)
		if itemCount == 1 and self._contentHasImage:
			menu.AppendSeparator()
			saveImageItem = menu.Append(
				wx.ID_ANY,
				# Translators: Context command to save the image from the selected entry.
				_("Save Selected &Image..."),
			)
			menu.Bind(wx.EVT_MENU, self._onSaveSelectedImage, saveImageItem)
		try:
			if eventPosition == wx.DefaultPosition:
				self.itemList.PopupMenu(menu, pos=self.itemList.GetItemRect(itemIndex).GetBottomLeft())
			else:
				self.itemList.PopupMenu(menu)
		finally:
			menu.Destroy()

	def _onViewFilterChanged(self, event: wx.CommandEvent) -> None:
		"""Apply one clipboard entry type filter without disturbing visible content."""
		itemType = next(
			(candidate for candidate, item in self._viewFilterItems.items() if item.GetId() == event.GetId()),
			self._itemTypeFilter,
		)
		if itemType == self._itemTypeFilter:
			return
		previousFilter = self._itemTypeFilter
		if self._isSearchSessionActive and self._searchTimer is not None:
			try:
				if not self._applySearchQuery():
					self._viewFilterItems[previousFilter].Check(True)
					return
			except Exception as error:
				self._viewFilterItems[previousFilter].Check(True)
				self._showError(error)
				self.searchCtrl.SetFocus()
				return
		previousKey, previousIndex, selectedKeys = self._getSearchListState()
		if self._isSearchSessionActive:
			self._itemTypeFilter = itemType
			self._refreshItems(
				preferredKey=previousKey,
				preferredIndex=previousIndex,
				selectedKeys=selectedKeys,
			)
			self._loadActiveSearchResult()
			return
		selectedKind = self._itemKinds[previousIndex] if previousIndex is not None else None
		selectedEntrySurvives = previousKey is not None and (itemType is None or selectedKind == itemType)
		wasContentCurrent = self._isContentCurrent()
		wasDirty = self._hasDirtyChanges()
		if (
			wasDirty
			and (not wasContentCurrent or not selectedEntrySurvives)
			and not self._confirmDirtyChanges()
		):
			self._viewFilterItems[previousFilter].Check(True)
			return
		try:
			self._itemTypeFilter = itemType
			refresh = self._reloadItemsFromController if wasDirty else self._refreshItems
			refresh(
				preferredKey=previousKey,
				preferredIndex=previousIndex,
				selectedKeys=selectedKeys,
			)
			if (
				self._getActiveItemKey() != previousKey or not wasContentCurrent
			) and not self._hasDirtyChanges():
				self._loadActiveItem(confirmDirty=False)
		except Exception as error:
			self._showError(error)

	def _onCategoryChanged(self, event: wx.CommandEvent) -> None:
		"""Apply a category selected through the list."""
		self._selectCategory(self.categoryList.GetSelection())

	def _selectCategory(self, selection: int) -> bool:
		"""Select and load one category, restoring the previous selection when cancelled."""
		if (
			getattr(self, "_isSearchSessionActive", False)
			or selection == wx.NOT_FOUND
			or selection >= len(self._categoryIds)
		):
			return False
		self.categoryList.SetSelection(selection)
		category = self._categoryIds[selection]
		previousCategory = self._selectedCategory
		if category == previousCategory:
			return True
		if not self._confirmDirtyChanges(previousCategory):
			if previousCategory in self._categoryIds:
				self.categoryList.SetSelection(self._categoryIds.index(previousCategory))
			return False
		try:
			self._selectedCategory = category
			self._reloadItemsFromController(preferredIndex=0, selectedKeys=())
			self._loadActiveItem(confirmDirty=False)
		except Exception as error:
			self._showError(error)
			return False
		return True

	def _onItemFocused(self, event: wx.ListEvent) -> None:
		"""Preview only the active row, independently of the selected set."""
		event.Skip()
		if self._isRefreshingList:
			return
		if wx.GetMouseState().RightIsDown():
			return
		if self._isSearchSessionActive and not self._prepareSearchListInteraction(enterWhenReady=True):
			return
		activeChanged = self._getActiveItemKey() != self._activeItemKey
		if not activeChanged:
			if self._isSearchSessionActive:
				self._loadActiveSearchResult(requireListFocus=True)
			return
		self._playActiveItemCue()
		if self._isSearchSessionActive:
			self._activeItemIndex = self._getActiveItemIndex()
			self._activeItemKey = self._getActiveItemKey()
			self._updateUiState()
			self._scheduleActiveItemLoad()
			return
		if self._hasDirtyChanges():
			try:
				self._loadActiveItem()
			except Exception as error:
				self._showError(error)
		else:
			self._activeItemIndex = self._getActiveItemIndex()
			self._activeItemKey = self._getActiveItemKey()
			self._updateUiState()
			self._scheduleActiveItemLoad()

	def _playActiveItemCue(self) -> None:
		"""Identify non-plain content when the active row changes."""
		index = self.itemList.GetFocusedItem()
		if 0 <= index < len(self._itemKinds) and self._itemKinds[index] != ClipboardItemType.PLAIN_TEXT:
			playNonPlainText()

	def _onNewCategory(self, event: wx.CommandEvent) -> None:
		if not self._confirmDirtyChanges():
			return
		# Translators: Prompt for the name of a new clipboard category.
		name = self._promptText(_("Enter the new category name:"))
		if name is None:
			return
		if not name:
			# Translators: Error shown when a clipboard category name is empty.
			self._showInfo(_("The category name cannot be empty."))
			return
		try:
			actualName = self.controller.createCategory(name)
			self._selectedCategory = actualName
			self._refreshCategories(actualName)
			self._reloadItemsFromController(selectedKeys=())
			self._clearContent(actualName)
		except Exception as error:
			self._showError(error)

	def _onRenameCategory(self, event: wx.CommandEvent) -> None:
		category = self._getSelectedCategory()
		if category is None or self.controller.isHistoryCategory(category):
			return
		assert isinstance(category, str)
		# Translators: Prompt for a new name for a clipboard category.
		newName = self._promptText(_("Enter the new category name:"), category)
		if newName is None:
			return
		if not newName:
			# Translators: Error shown when a clipboard category name is empty.
			self._showInfo(_("The category name cannot be empty."))
			return
		try:
			itemKey = self._activeItemKey
			selectedKeys = self._getSelectedItemKeys()
			actualName = self.controller.renameCategory(category, newName)
			if self._contentCategory == category:
				self._contentCategory = actualName
			self._selectedCategory = actualName
			self._refreshCategories(actualName)
			self._reloadItemsFromController(preferredKey=itemKey, selectedKeys=selectedKeys)
			if not self._isContentCurrent() and not self._hasDirtyChanges():
				self._loadActiveItem(confirmDirty=False)
		except Exception as error:
			self._showError(error)

	def _onDeleteCategory(self, event: wx.CommandEvent) -> None:
		category = self._getSelectedCategory()
		if category is None or self.controller.isHistoryCategory(category):
			return
		assert isinstance(category, str)
		if not self._confirmDirtyChanges():
			return
		# Translators: Confirmation before deleting an empty clipboard category.
		if not self._confirm(_("Delete the selected category?")):
			return
		try:
			self.controller.deleteCategory(category)
			self._refreshCategories()
			self._reloadItemsFromController(preferredIndex=0, selectedKeys=())
			self._loadActiveItem(confirmDirty=False)
		except Exception as error:
			self._showError(error)

	def _onPutItemOnClipboard(self, event: wx.Event) -> None:
		"""Put the active stored entry on the clipboard without pasting it."""
		if self._isSearchSessionActive and not self._prepareSearchListInteraction():
			return
		category = self._getSelectedCategory()
		index = self._getActiveItemIndex()
		key = self._getActiveItemKey()
		if category is None or index is None or key is None:
			return
		try:
			self.controller.putItemOnClipboard(category, key)
		except Exception as error:
			self._showError(error)

	def _onTransferItemsToCategory(self, event: wx.CommandEvent) -> None:
		"""Move or collect the selected entries in one operation."""
		if self._isSearchSessionActive and not self._prepareSearchListInteraction():
			return
		sourceCategory = self._getSelectedCategory()
		index = self._getActiveItemIndex()
		activeKey = self._getActiveItemKey()
		selectedKeys = self._getSelectedItemKeys()
		if sourceCategory is None or index is None or activeKey is None or not selectedKeys:
			return
		survivingNeighborKey = self._getSurvivingNeighborKey(index, selectedKeys)
		try:
			targetCategories = [
				category
				for category in self.controller.getCategories()
				if category != sourceCategory and not self.controller.isHistoryCategory(category)
			]
		except Exception as error:
			self._showError(error)
			return
		if not targetCategories:
			# Translators: Message shown when no user category can receive an entry.
			self._showInfo(_("Create another user category first."))
			return
		dialog = wx.SingleChoiceDialog(
			self,
			# Translators: Prompt for the destination category of selected entries.
			_("Choose the destination category:"),
			self.GetTitle(),
			[self.controller.getCategoryLabel(category) for category in targetCategories],
		)
		try:
			if displayDialogAsModal(dialog) != wx.ID_OK:
				return
			targetSelection = dialog.GetSelection()
			if targetSelection == wx.NOT_FOUND:
				return
			targetCategory = targetCategories[targetSelection]
		finally:
			dialog.Destroy()
		isHistory = self.controller.isHistoryCategory(sourceCategory)
		activeWillRemain = isHistory or activeKey not in selectedKeys
		if not activeWillRemain and not self._confirmDirtyChanges():
			return
		try:
			self.controller.transferItemsToCategory(
				sourceCategory,
				selectedKeys,
				targetCategory,
			)
			self._reloadItemsFromController(
				preferredKey=activeKey if activeWillRemain else survivingNeighborKey,
				selectedKeys=selectedKeys if isHistory else (),
			)
			if not self._isContentCurrent() and not self._hasDirtyChanges():
				self._loadActiveItem(confirmDirty=False)
		except Exception as error:
			self._showError(error)

	def _onDeleteItems(self, event: wx.Event) -> None:
		"""Delete the selected entries in one operation."""
		if self._isSearchSessionActive and not self._prepareSearchListInteraction():
			return
		category = self._getSelectedCategory()
		index = self._getActiveItemIndex()
		activeKey = self._getActiveItemKey()
		selectedKeys = self._getSelectedItemKeys()
		itemCount = len(selectedKeys)
		if category is None or index is None or activeKey is None or not selectedKeys:
			return
		survivingNeighborKey = self._getSurvivingNeighborKey(index, selectedKeys)
		confirmation = ngettext(
			# Translators: Confirmation before deleting one or multiple clipboard entries.
			"Delete the selected entry?",
			"Delete the {count} selected entries?",
			itemCount,
		).format(count=itemCount)
		if not self._confirm(confirmation):
			return
		activeWillRemain = activeKey not in selectedKeys
		if not activeWillRemain and not self._confirmDirtyChanges():
			return
		try:
			self.controller.deleteItems(category, selectedKeys)
			self._reloadItemsFromController(
				preferredKey=activeKey if activeWillRemain else survivingNeighborKey,
			)
			if not self._isContentCurrent() and not self._hasDirtyChanges():
				self._loadActiveItem(confirmDirty=False)
		except Exception as error:
			self._showError(error)

	def _onRemoveMissingFiles(self, event: wx.Event) -> None:
		"""Remove missing paths from the selected file groups."""
		if self._isSearchSessionActive and not self._prepareSearchListInteraction():
			return
		category = self._getSelectedCategory()
		index = self._getActiveItemIndex()
		activeKey = self._getActiveItemKey()
		selectedKeys = self._getSelectedItemKeys()
		fileGroupKeys = self._getSelectedFileGroupKeys()
		if category is None or index is None or activeKey is None or not fileGroupKeys:
			return
		if not self._confirm(
			_(
				"Remove missing files from the selected file groups? "
				"File groups with no remaining files will be deleted.",
			),
		):
			return
		try:
			self.controller.startMissingFileCleanup(
				category,
				fileGroupKeys,
				partial(
					self._finishRemoveMissingFiles,
					category=category,
					index=index,
					activeKey=activeKey,
					selectedKeys=selectedKeys,
					fileGroupKeys=fileGroupKeys,
				),
			)
		except Exception as error:
			self._showError(error)

	def _finishRemoveMissingFiles(
		self,
		result: MissingFileCleanupResult | Exception,
		*,
		category: CategoryId,
		index: int,
		activeKey: int,
		selectedKeys: tuple[int, ...],
		fileGroupKeys: tuple[int, ...],
	) -> None:
		"""Apply one missing-file cleanup result to the visible manager."""
		if self._isBeingDestroyed or not self.IsShown():
			return
		if isinstance(result, Exception):
			self._showError(result)
			return
		if not result.changedCount:
			# Translators: Message shown when selected file groups have no missing paths.
			self._showInfo(_("No missing files"))
			return
		self._showInfo(
			ngettext(
				# Translators: Message shown after removing one or multiple missing paths from file groups.
				"Removed {count} missing file",
				"Removed {count} missing files",
				result.removedCount,
			).format(count=result.removedCount),
		)
		if self._getSelectedCategory() != category:
			return
		if self._getActiveItemKey() != activeKey or self._getSelectedItemKeys() != selectedKeys:
			self.refreshFromController()
			return
		preferredKey = result.replacementIds.get(activeKey, activeKey)
		activeWillRemain = activeKey not in fileGroupKeys or activeKey in result.replacementIds
		survivingNeighborKey = self._getSurvivingNeighborKey(index, fileGroupKeys)
		self._reloadItemsFromController(
			preferredKey=preferredKey if activeWillRemain else survivingNeighborKey,
		)
		if not self._isContentCurrent() and not self._hasDirtyChanges():
			self._loadActiveItem(confirmDirty=False)

	def _onSaveSelectedImage(self, event: wx.CommandEvent) -> None:
		if self._isSearchSessionActive and not self._prepareSearchListInteraction():
			return
		if not self._isContentCurrent():
			return
		category = self._getSelectedCategory()
		index = self._getActiveItemIndex()
		key = self._getActiveItemKey()
		if category is None or index is None or key is None:
			return
		try:
			self.controller.saveStoredItemImage(category, key, self)
		except Exception as error:
			self._showError(error)

	def _onNewEntry(self, event: wx.CommandEvent) -> None:
		"""Start a blank plain-text entry in the selected category."""
		if getattr(self, "_isSearchSessionActive", False):
			return
		if not self._confirmDirtyChanges():
			return
		self._startPlainTextDraft()
		self.editor.SetFocus()

	def _onOpenFile(self, event: wx.CommandEvent) -> None:
		if getattr(self, "_isSearchSessionActive", False):
			return
		if not self._confirmDirtyChanges():
			return
		dialog = wx.FileDialog(
			self,
			# Translators: Title of the file dialog for opening text.
			message=_("Open Text File"),
			# Translators: File type filter for clipboard manager text files.
			wildcard=_("Text files (*.txt)|*.txt|All files (*.*)|*.*"),
			style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
		)
		try:
			if displayDialogAsModal(dialog) != wx.ID_OK:
				return
			path = Path(dialog.GetPath())
			# Translators: Error shown when a selected text file exceeds the manager's size limit.
			fileSizeError = _("The text file exceeds the {limit} MB size limit.").format(
				limit=MAX_TEXT_BYTES // (1024 * 1024),
			)
			if path.stat().st_size > MAX_TEXT_BYTES:
				raise ValueError(fileSizeError)
			with path.open("rb") as textFile:
				data = textFile.read(MAX_TEXT_BYTES + 1)
			if len(data) > MAX_TEXT_BYTES:
				raise ValueError(fileSizeError)
			try:
				text = data.decode("utf-8-sig")
			except UnicodeDecodeError:
				text = data.decode(locale.getencoding())
			text = text.replace("\r\n", "\n").replace("\r", "\n")
			self._startPlainTextDraft(text)
		except Exception as error:
			self._showError(error)
		finally:
			dialog.Destroy()

	def _onSaveAs(self, event: wx.CommandEvent) -> None:
		if (
			not self._isContentCurrent()
			or self.editor.GetLastPosition() == 0
			or self._contentKind == ClipboardItemType.IMAGE
		):
			return
		dialog = wx.FileDialog(
			self,
			# Translators: Title of the file dialog for saving visible text.
			message=_("Save Text File"),
			# Translators: File type filter for clipboard manager text files.
			wildcard=_("Text files (*.txt)|*.txt|All files (*.*)|*.*"),
			style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
		)
		try:
			if displayDialogAsModal(dialog) != wx.ID_OK:
				return
			Path(dialog.GetPath()).write_text(self.editor.GetValue(), encoding="utf-8", newline="")
			# Translators: Message shown after visible content is saved to a file.
			self._showInfo(_("The text file was saved."))
		except Exception as error:
			self._showError(error)
		finally:
			dialog.Destroy()

	def _onReplaceClipboardWithText(self, event: wx.CommandEvent) -> None:
		self._saveVisibleContent(closeAfterSave=False)

	def _onSavePlainTextToCategory(self, event: wx.CommandEvent) -> None:
		self._saveVisibleContent(closeAfterSave=True)

	def _onFind(self, event: wx.CommandEvent, backwards: bool = False) -> None:
		"""Open the editor find dialog when visible content can be searched."""
		if (
			not self._isContentCurrent()
			or self.editor.GetLastPosition() == 0
			or self._contentKind == ClipboardItemType.IMAGE
		):
			return
		self._editorCommands.showFind(backwards=backwards)

	def _onContinueFind(self, event: wx.CommandEvent) -> None:
		"""Repeat the previous forward editor search."""
		if not self._isContentCurrent() or self._contentKind == ClipboardItemType.IMAGE:
			return
		self._editorCommands.findNext()

	def _onPreviousFind(self, event: wx.CommandEvent) -> None:
		"""Repeat the previous backward editor search."""
		if not self._isContentCurrent() or self._contentKind == ClipboardItemType.IMAGE:
			return
		self._editorCommands.findNext(backwards=True)

	def _onReplace(self, event: wx.CommandEvent) -> None:
		"""Open the editor replacement dialog for editable content."""
		if (
			getattr(self, "_isSearchSessionActive", False)
			or not self._isContentCurrent()
			or not self._contentEditable
		):
			return
		self._editorCommands.showReplace()

	def _onGotoLine(self, event: wx.CommandEvent) -> None:
		"""Open line navigation for visible text content."""
		if (
			not self._isContentCurrent()
			or self.editor.GetLastPosition() == 0
			or self._contentKind == ClipboardItemType.IMAGE
		):
			return
		self._editorCommands.goToLine()

	def _onApplyTextTransform(
		self,
		event: wx.CommandEvent,
		*,
		transform: Callable[[str], str],
	) -> None:
		"""Apply one text transform to the current editable content."""
		if (
			getattr(self, "_isSearchSessionActive", False)
			or not self._isContentCurrent()
			or not self._contentEditable
		):
			return
		textTransforms.applyEditorTextTransform(self.editor, transform, lineWise=True)

	def _onCloudSync(self, event: wx.CommandEvent) -> None:
		"""Open the Tiantan Cloud Clipboard account dialog."""
		try:
			self.controller.showCloudDialog(self)
		except Exception as error:
			self._showError(error)

	def _onOneDriveSync(self, event: wx.CommandEvent) -> None:
		"""Open the OneDrive account dialog."""
		try:
			self.controller.showOneDriveDialog(self)
		except Exception as error:
			self._showError(error)

	def _onSendToTiantan(self, event: wx.CommandEvent) -> None:
		"""Send current clipboard text to Tiantan Cloud Clipboard."""
		try:
			self.controller.sendToTiantan()
		except Exception as error:
			self._showError(error)

	def _onAutoSyncTiantan(self, event: wx.CommandEvent) -> None:
		"""Toggle automatic Tiantan Cloud Clipboard synchronization."""
		cloudSync = self.controller.cloudSync
		if self.autoSyncTiantanItem is not None and cloudSync is not None:
			cloudSync.setAutoSyncEnabled(self.autoSyncTiantanItem.IsChecked())

	def _onReceiveFromTiantan(self, event: wx.CommandEvent) -> None:
		"""Receive Tiantan Cloud Clipboard text into the system clipboard."""
		try:
			self.controller.receiveFromTiantan()
		except Exception as error:
			self._showError(error)

	def _onSyncOneDrive(self, event: wx.CommandEvent) -> None:
		"""Synchronize with OneDrive immediately."""
		try:
			self.controller.syncOneDrive()
		except Exception as error:
			self._showError(error)

	def _onCharHook(self, event: wx.KeyEvent) -> None:
		"""Handle manager-wide keys and list batch commands."""
		keyCode = event.GetKeyCode()
		modifiers = event.GetModifiers()
		if keyCode == ord("S") and modifiers == wx.MOD_ALT:
			self.searchCtrl.SetFocus()
			if self.searchCtrl.GetValue():
				self.searchCtrl.SelectAll()
			return
		if (
			self.FindFocus() is self.searchCtrl
			and keyCode in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER, wx.WXK_DOWN)
			and modifiers == wx.MOD_NONE
		):
			try:
				self._enterSearchResults()
			except Exception as error:
				self._showError(error)
				self.searchCtrl.SetFocus()
			return
		if keyCode == wx.WXK_ESCAPE:
			if (
				self.FindFocus() is self.searchCtrl
				or self._isSearchSessionActive
				or self.searchCtrl.GetValue()
			):
				try:
					self._exitSearch()
				except Exception as error:
					self._showError(error)
				return
			self.Close()
			return
		if self.FindFocus() is self.itemList and keyCode == ord("A") and modifiers == wx.MOD_CONTROL:
			if self._isSearchSessionActive and not self._prepareSearchListInteraction():
				return
			self.itemList.SetItemState(-1, wx.LIST_STATE_SELECTED, wx.LIST_STATE_SELECTED)
			return
		if self.FindFocus() is self.itemList and keyCode == wx.WXK_DELETE and modifiers == wx.MOD_NONE:
			self._onDeleteItems(event)
			return
		if (
			self.FindFocus() is self.itemList
			and keyCode in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
			and modifiers == wx.MOD_NONE
		):
			self._onPutItemOnClipboard(event)
			return
		event.Skip()

	def _onExit(self, event: wx.CommandEvent) -> None:
		self.Close()

	def _onClose(self, event: wx.CloseEvent) -> None:
		self._cancelPendingItemLoad()
		if event.CanVeto():
			if getConfirmOnClose() and not self._confirmDirtyChanges():
				event.Veto()
				return
			self.Hide()
			self._resetSearchState(clearEntries=True)
			event.Veto()
		else:
			event.Skip()

	def _onWindowDestroy(self, event: wx.WindowDestroyEvent) -> None:
		if event.GetEventObject() is self:
			self._isBeingDestroyed = True
			self._cancelPendingItemLoad()
			self._itemLoadTimer = None
			self._cancelPendingSearch()
			self._cancelSearchScan()
			self._searchKeywords = ()
			self._matchingSearchKeys = None
			self._isSearchSessionActive = False
			self._pendingSearchListState = None
			self._searchExecutor.shutdown(wait=False, cancel_futures=True)
			self._allEntries = ()
		event.Skip()
