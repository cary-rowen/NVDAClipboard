"""Tests for clipboard manager category interactions without loading NVDA."""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock


_MODULE_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "manager.py"


def _loadManagerMethods(
	fakeWx: SimpleNamespace,
) -> tuple[Callable[..., bool], Callable[..., None]]:
	"""Load category methods without importing manager GUI dependencies."""
	tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
	managerClass = next(
		node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ClipboardManagerFrame"
	)
	methods = [
		node
		for node in managerClass.body
		if isinstance(node, ast.FunctionDef) and node.name in {"_selectCategory", "_onCategoryContextMenu"}
	]
	namespace: dict[str, object] = {"wx": fakeWx, "_": lambda message: message}
	exec(compile(ast.Module(body=methods, type_ignores=[]), str(_MODULE_PATH), "exec"), namespace)
	return namespace["_selectCategory"], namespace["_onCategoryContextMenu"]


class ManagerCategoryContextMenuTests(unittest.TestCase):
	"""Verify that category context commands target the clicked row."""

	def testRightClickSelectsCategoryBeforeBuildingMenu(self) -> None:
		"""Load the right-clicked category before evaluating its commands."""
		position = object()
		menu = Mock()
		menu.Append.side_effect = (Mock(), Mock(), Mock())
		fakeWx = SimpleNamespace(
			CommandEvent=object,
			ContextMenuEvent=object,
			DefaultPosition=object(),
			EVT_MENU=object(),
			ID_ANY=-1,
			Menu=Mock(return_value=menu),
			NOT_FOUND=-1,
		)
		selectCategory, showContextMenu = _loadManagerMethods(fakeWx)
		categoryList = Mock()
		categoryList.HitTest.return_value = 1
		controller = SimpleNamespace(isHistoryCategory=Mock(return_value=False))
		manager = SimpleNamespace(
			_categoryHasItems=False,
			_categoryIds=["first", "second"],
			_navigationSyncState=object(),
			_selectedCategory="first",
			_confirmDirtyChanges=Mock(return_value=True),
			_loadActiveItem=Mock(),
			_onDeleteCategory=Mock(),
			_onNewCategory=Mock(),
			_onRenameCategory=Mock(),
			_reloadItemsFromController=Mock(),
			_showError=Mock(),
			categoryList=categoryList,
			controller=controller,
		)
		manager._getSelectedCategory = lambda: manager._selectedCategory
		manager._selectCategory = MethodType(selectCategory, manager)
		event = Mock()
		event.GetPosition.return_value = position

		showContextMenu(manager, event)

		categoryList.HitTest.assert_called_once_with(categoryList.ScreenToClient.return_value)
		categoryList.SetSelection.assert_called_once_with(1)
		controller.isHistoryCategory.assert_called_once_with("second")
		categoryList.PopupMenu.assert_called_once_with(menu)
		menu.Destroy.assert_called_once_with()
