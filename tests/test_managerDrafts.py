"""Tests for clipboard manager draft creation without loading NVDA."""

from __future__ import annotations

from collections.abc import Callable
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock

from _manager_method_loader import loadManagerClassMethods, loadManagerTopLevelFunctions


def _loadDraftMethods() -> tuple[
	Callable[..., None],
	Callable[..., None],
	Callable[..., None],
	Callable[..., None],
	Callable[..., None],
]:
	"""Load draft methods without importing manager GUI dependencies."""
	helpers = loadManagerTopLevelFunctions({"_sourceToEditorOffset"})
	namespace = loadManagerClassMethods(
		{
			"refreshFromController",
			"_restoreEditorOffsetFromClipboardNavigation",
			"_startPlainTextDraft",
			"_markSystemClipboardTextSaved",
			"_onNewEntry",
		},
		{
			"_sourceToEditorOffset": helpers["_sourceToEditorOffset"],
			"ClipboardItemType": SimpleNamespace(PLAIN_TEXT="plainText"),
			"wx": SimpleNamespace(CommandEvent=object),
		},
	)
	return (
		namespace["refreshFromController"],
		namespace["_restoreEditorOffsetFromClipboardNavigation"],
		namespace["_startPlainTextDraft"],
		namespace["_markSystemClipboardTextSaved"],
		namespace["_onNewEntry"],
	)


(
	_refreshFromController,
	_restoreEditorOffsetFromClipboardNavigation,
	_startPlainTextDraft,
	_markSystemClipboardTextSaved,
	_onNewEntry,
) = _loadDraftMethods()


class ManagerDraftTests(unittest.TestCase):
	"""Verify creation of blank manager text drafts."""

	def testNewEntryStartsBlankDraftInSelectedCategory(self) -> None:
		"""Replace saved content with a focused blank draft after confirmation."""
		editor = Mock()
		manager = SimpleNamespace(
			_baselineText="old",
			_confirmDirtyChanges=Mock(return_value=True),
			_dirtyStateNeedsCheck=True,
			_getSelectedCategory=Mock(return_value="Saved"),
			_isDirty=True,
			_setContent=Mock(),
			_updateUiState=Mock(),
			editor=editor,
		)
		manager._startPlainTextDraft = MethodType(_startPlainTextDraft, manager)

		_onNewEntry(manager, object())

		manager._setContent.assert_called_once_with(
			"",
			isEditable=True,
			category="Saved",
			itemKey=None,
			kind="plainText",
			hasImage=False,
			canUpload=True,
			isDraft=True,
		)
		self.assertEqual("", manager._baselineText)
		self.assertFalse(manager._isDirty)
		self.assertFalse(manager._dirtyStateNeedsCheck)
		editor.SetFocus.assert_called_once_with()

	def testNewEntryKeepsContentWhenDirtyConfirmationIsCancelled(self) -> None:
		"""Leave the existing content untouched when the user cancels."""
		manager = SimpleNamespace(
			_confirmDirtyChanges=Mock(return_value=False),
			_startPlainTextDraft=Mock(),
			editor=Mock(),
		)

		_onNewEntry(manager, object())

		manager._startPlainTextDraft.assert_not_called()
		manager.editor.SetFocus.assert_not_called()

	def testRefreshKeepsCleanDraftAfterSavedContentChanges(self) -> None:
		"""Keep a newly started draft when the preceding save queues a refresh."""
		manager = SimpleNamespace(
			_contentActiveKey=1,
			_contentEditable=True,
			_contentItemKey=None,
			_isSearchSessionActive=False,
			_selectedCategory="Saved",
			_getActiveItemKey=Mock(return_value=2),
			_getSearchListState=Mock(return_value=(1, 0, (1,))),
			_getSelectedCategory=Mock(return_value="Saved"),
			_hasDirtyChanges=Mock(return_value=False),
			_loadActiveItem=Mock(),
			_refreshCategories=Mock(),
			_reloadItemsFromController=Mock(),
			_resetSearchState=Mock(),
			_showError=Mock(),
			_syncEnteredSearchResult=Mock(),
			_updateUiState=Mock(),
		)

		_refreshFromController(manager)

		self.assertEqual(2, manager._contentActiveKey)
		manager._loadActiveItem.assert_not_called()
		manager._updateUiState.assert_called_once_with()

	def testSystemClipboardSaveSurvivesHistoryRefresh(self) -> None:
		"""Keep saved clipboard text visible when its history write refreshes the list."""
		editor = Mock()
		editor.GetValue.return_value = "first\nsecond"
		manager = SimpleNamespace(
			_baselineText="first",
			_contentActiveKey=1,
			_contentEditable=True,
			_contentItemKey=1,
			_contentSourceText="first",
			_dirtyStateNeedsCheck=False,
			_isDirty=False,
			_isSearchSessionActive=False,
			_selectedCategory="history",
			_getActiveItemKey=Mock(return_value=2),
			_getSearchListState=Mock(return_value=(1, 0, (1,))),
			_getSelectedCategory=Mock(return_value="history"),
			_hasDirtyChanges=Mock(return_value=False),
			_loadActiveItem=Mock(),
			_refreshCategories=Mock(),
			_reloadItemsFromController=Mock(),
			_resetSearchState=Mock(),
			_showError=Mock(),
			_syncEnteredSearchResult=Mock(),
			_updateUiState=Mock(),
			editor=editor,
		)

		_markSystemClipboardTextSaved(manager, "first\nsecond")
		_refreshFromController(manager)

		self.assertIsNone(manager._contentItemKey)
		self.assertEqual("first\nsecond", manager._contentSourceText)
		self.assertEqual(2, manager._contentActiveKey)
		manager._loadActiveItem.assert_not_called()

	def testRefreshPreservesEditorInsertionPointForCurrentStoredContent(self) -> None:
		"""Keep the editor cursor where it was when a copy triggers a refresh."""
		manager = SimpleNamespace(
			_contentActiveKey=1,
			_contentEditable=True,
			_contentItemKey=7,
			_contentSourceText="example text",
			_isSearchSessionActive=False,
			_selectedCategory="Saved",
			_getEditorCodePointOffset=Mock(return_value=5),
			_getActiveItemKey=Mock(return_value=7),
			_getSearchListState=Mock(return_value=(1, 0, (1,))),
			_getSelectedCategory=Mock(return_value="Saved"),
			_hasDirtyChanges=Mock(return_value=False),
			_loadActiveItem=Mock(),
			_refreshCategories=Mock(),
			_reloadItemsFromController=Mock(),
			_setEditorCodePointOffset=Mock(),
			_showError=Mock(),
			_syncEnteredSearchResult=Mock(),
			_updateUiState=Mock(),
		)

		_refreshFromController(manager)

		manager._loadActiveItem.assert_called_once_with(confirmDirty=False)
		manager._setEditorCodePointOffset.assert_called_once_with(5)

	def testOpeningCanUseCurrentClipboardNavigationOffset(self) -> None:
		"""Use clipboard navigation only to choose the initial editor cursor."""
		manager = SimpleNamespace(
			_contentSourceText="first\r\nsecond",
			_setEditorCodePointOffset=Mock(),
			controller=SimpleNamespace(getCurrentNavigationOffsetForText=Mock(return_value=8)),
		)

		_restoreEditorOffsetFromClipboardNavigation(manager)

		manager.controller.getCurrentNavigationOffsetForText.assert_called_once_with("first\r\nsecond")
		manager._setEditorCodePointOffset.assert_called_once_with(7)
