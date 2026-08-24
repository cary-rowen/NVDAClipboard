"""Tests for clipboard manager category interactions without loading NVDA."""

from __future__ import annotations

from collections.abc import Callable
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock

from _manager_method_loader import loadManagerClassMethods


def _loadManagerMethods(
	fakeWx: SimpleNamespace,
) -> tuple[Callable[..., bool], Callable[..., None]]:
	"""Load category methods without importing manager GUI dependencies."""
	namespace = loadManagerClassMethods(
		{"_selectCategory", "_onCategoryContextMenu"},
		{"wx": fakeWx, "_": lambda message: message},
	)
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
