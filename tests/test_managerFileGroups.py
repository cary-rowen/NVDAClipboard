"""Tests for clipboard manager file-group commands without loading NVDA."""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


_MODULE_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "manager.py"


def _loadFileGroupMethods() -> tuple[
	Callable[..., tuple[int, ...]],
	Callable[..., None],
	Callable[..., None],
]:
	"""Load file-group menu methods without importing manager GUI dependencies."""
	tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
	managerClass = next(
		node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ClipboardManagerFrame"
	)
	methods = [
		node
		for node in managerClass.body
		if isinstance(node, ast.FunctionDef)
		and node.name in {"_getSelectedFileGroupKeys", "_showItemContextMenu", "_onRemoveMissingFiles"}
	]
	namespace: dict[str, object] = {
		"ClipboardItemType": SimpleNamespace(FILES="files"),
		"ngettext": lambda singular, plural, count: singular if count == 1 else plural,
		"_": lambda message: message,
	}
	exec(compile(ast.Module(body=methods, type_ignores=[]), str(_MODULE_PATH), "exec"), namespace)
	return namespace["_getSelectedFileGroupKeys"], namespace["_showItemContextMenu"], namespace["_onRemoveMissingFiles"]


_getSelectedFileGroupKeys, _showItemContextMenu, _onRemoveMissingFiles = _loadFileGroupMethods()


class ManagerFileGroupTests(unittest.TestCase):
	"""Verify file-group context menu behavior."""

	def testFileGroupSelectionShowsRemoveMissingFiles(self) -> None:
		"""Show missing-file cleanup when selected entries include a file group."""
		menu = Mock()
		restoreItem = Mock()
		transferItem = Mock()
		removeMissingFilesItem = Mock()
		deleteItem = Mock()
		menu.Append.side_effect = [restoreItem, transferItem, removeMissingFilesItem, deleteItem]
		fakeWx = SimpleNamespace(
			DefaultPosition=object(),
			EVT_MENU=object(),
			ID_ANY=-1,
			Menu=Mock(return_value=menu),
			NOT_FOUND=-1,
		)
		_showItemContextMenu.__globals__["wx"] = fakeWx
		itemList = Mock()
		itemList.GetFirstSelected.return_value = 0
		itemList.GetNextSelected.return_value = -1
		manager = SimpleNamespace(
			_contentHasImage=False,
			_getSelectedCategory=Mock(return_value="Saved"),
			_itemKeys=(7,),
			_itemKinds=("files",),
			_onDeleteItems=Mock(),
			_onRemoveMissingFiles=Mock(),
			_onRestoreItemToClipboard=Mock(),
			_onTransferItemsToCategory=Mock(),
			controller=SimpleNamespace(selectedFileGroupsHaveMissingFiles=Mock(return_value=True)),
			itemList=itemList,
		)
		manager._getSelectedFileGroupKeys = lambda: _getSelectedFileGroupKeys(manager)

		_showItemContextMenu(manager, fakeWx.DefaultPosition, 0, 1)

		self.assertEqual(4, menu.Append.call_count)
		self.assertEqual("Remove &Missing Files", menu.Append.call_args_list[2].args[1])
		menu.Bind.assert_any_call(fakeWx.EVT_MENU, manager._onRemoveMissingFiles, removeMissingFilesItem)
		manager.controller.selectedFileGroupsHaveMissingFiles.assert_called_once_with("Saved", (7,))
		menu.Destroy.assert_called_once_with()

	def testFileGroupSelectionHidesRemoveMissingFilesWhenNothingCanBeCleaned(self) -> None:
		"""Hide missing-file cleanup when selected file groups have no missing paths."""
		menu = Mock()
		menu.Append.side_effect = [Mock(), Mock(), Mock()]
		fakeWx = SimpleNamespace(
			DefaultPosition=object(),
			EVT_MENU=object(),
			ID_ANY=-1,
			Menu=Mock(return_value=menu),
			NOT_FOUND=-1,
		)
		_showItemContextMenu.__globals__["wx"] = fakeWx
		itemList = Mock()
		itemList.GetFirstSelected.return_value = 0
		itemList.GetNextSelected.return_value = -1
		manager = SimpleNamespace(
			_contentHasImage=False,
			_getSelectedCategory=Mock(return_value="Saved"),
			_itemKeys=(7,),
			_itemKinds=("files",),
			_onDeleteItems=Mock(),
			_onRestoreItemToClipboard=Mock(),
			_onTransferItemsToCategory=Mock(),
			controller=SimpleNamespace(selectedFileGroupsHaveMissingFiles=Mock(return_value=False)),
			itemList=itemList,
		)
		manager._getSelectedFileGroupKeys = lambda: _getSelectedFileGroupKeys(manager)

		_showItemContextMenu(manager, fakeWx.DefaultPosition, 0, 1)

		self.assertEqual(3, menu.Append.call_count)
		self.assertNotIn(
			"Remove &Missing Files",
			[call.args[1] for call in menu.Append.call_args_list],
		)
		manager.controller.selectedFileGroupsHaveMissingFiles.assert_called_once_with("Saved", (7,))
		menu.Destroy.assert_called_once_with()

	def testPlainTextSelectionHidesRemoveMissingFiles(self) -> None:
		"""Hide missing-file cleanup when selected entries are not file groups."""
		menu = Mock()
		menu.Append.side_effect = [Mock(), Mock(), Mock()]
		fakeWx = SimpleNamespace(
			DefaultPosition=object(),
			EVT_MENU=object(),
			ID_ANY=-1,
			Menu=Mock(return_value=menu),
			NOT_FOUND=-1,
		)
		_showItemContextMenu.__globals__["wx"] = fakeWx
		itemList = Mock()
		itemList.GetFirstSelected.return_value = 0
		itemList.GetNextSelected.return_value = -1
		manager = SimpleNamespace(
			_contentHasImage=False,
			_itemKeys=(7,),
			_itemKinds=("plainText",),
			_onDeleteItems=Mock(),
			_onRestoreItemToClipboard=Mock(),
			_onTransferItemsToCategory=Mock(),
			itemList=itemList,
		)
		manager._getSelectedFileGroupKeys = lambda: _getSelectedFileGroupKeys(manager)

		_showItemContextMenu(manager, fakeWx.DefaultPosition, 0, 1)

		self.assertEqual(3, menu.Append.call_count)
		self.assertNotIn(
			"Remove &Missing Files",
			[call.args[1] for call in menu.Append.call_args_list],
		)
		menu.Destroy.assert_called_once_with()

	def testPartialCleanupKeepsActiveReplacementFocused(self) -> None:
		"""Focus the cleaned replacement when the active file group remains."""
		result = SimpleNamespace(
			changedCount=1,
			removedCount=1,
			replacementIds={7: 9},
		)
		manager = SimpleNamespace(
			_getSelectedCategory=Mock(return_value="Saved"),
			_getActiveItemIndex=Mock(return_value=0),
			_getActiveItemKey=Mock(return_value=7),
			_getSelectedFileGroupKeys=Mock(return_value=(7,)),
			_confirm=Mock(return_value=True),
			_showInfo=Mock(),
			_navigationSyncState=object(),
			_getSurvivingNeighborKey=Mock(return_value=11),
			_reloadItemsFromController=Mock(),
			_isContentCurrent=Mock(return_value=False),
			_hasDirtyChanges=Mock(return_value=False),
			_loadActiveItem=Mock(),
			_showError=Mock(),
			_isSearchSessionActive=False,
			controller=SimpleNamespace(removeMissingFiles=Mock(return_value=result)),
		)

		_onRemoveMissingFiles(manager, object())

		manager._reloadItemsFromController.assert_called_once_with(preferredKey=9)
		self.assertIsNotNone(manager._navigationSyncState)
		manager._getSurvivingNeighborKey.assert_called_once_with(0, (7,))


if __name__ == "__main__":
	unittest.main()
