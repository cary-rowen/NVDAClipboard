"""Tests for clipboard manager draft creation without loading NVDA."""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock


_MODULE_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "manager.py"


def _loadDraftMethods() -> tuple[Callable[..., None], Callable[..., None], Callable[..., None]]:
	"""Load draft methods without importing manager GUI dependencies."""
	tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
	managerClass = next(
		node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ClipboardManagerFrame"
	)
	methods = [
		node
		for node in managerClass.body
		if isinstance(node, ast.FunctionDef)
		and node.name in {"refreshFromController", "_startPlainTextDraft", "_onNewEntry"}
	]
	namespace: dict[str, object] = {
		"ClipboardItemType": SimpleNamespace(PLAIN_TEXT="plainText"),
		"wx": SimpleNamespace(CommandEvent=object),
	}
	exec(compile(ast.Module(body=methods, type_ignores=[]), str(_MODULE_PATH), "exec"), namespace)
	return namespace["refreshFromController"], namespace["_startPlainTextDraft"], namespace["_onNewEntry"]


_refreshFromController, _startPlainTextDraft, _onNewEntry = _loadDraftMethods()


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
			_navigationSyncState=object(),
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
		self.assertIsNone(manager._navigationSyncState)
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
			_navigationSyncState=None,
			_selectedCategory="Saved",
			_getActiveItemKey=Mock(return_value=2),
			_getSearchListState=Mock(return_value=(1, 0, (1,))),
			_getSelectedCategory=Mock(return_value="Saved"),
			_hasDirtyChanges=Mock(return_value=False),
			_loadActiveItem=Mock(),
			_refreshCategories=Mock(),
			_reloadItemsFromController=Mock(),
			_resetSearchState=Mock(),
			_restoreNavigationSyncOffsetInEditor=Mock(),
			_showError=Mock(),
			_syncEnteredSearchResult=Mock(),
			_updateUiState=Mock(),
		)

		_refreshFromController(manager)

		self.assertEqual(2, manager._contentActiveKey)
		manager._loadActiveItem.assert_not_called()
		manager._updateUiState.assert_called_once_with()
