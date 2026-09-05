# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for clipboard manager file-group commands without loading NVDA."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock

from _manager_method_loader import loadManagerClassMethods


def _loadFileGroupMethods() -> tuple[
	Callable[..., tuple[int, ...]],
	Callable[..., None],
	Callable[..., None],
	Callable[..., None],
]:
	"""Load file-group menu methods without importing manager GUI dependencies."""
	namespace = loadManagerClassMethods(
		{
			"_getSelectedFileGroupKeys",
			"_showItemContextMenu",
			"_onRemoveMissingFiles",
			"_finishRemoveMissingFiles",
		},
		{
			"ClipboardItemType": SimpleNamespace(FILES="files"),
			"ngettext": lambda singular, plural, count: singular if count == 1 else plural,
			"partial": partial,
			"_": lambda message: message,
		},
	)
	return (
		namespace["_getSelectedFileGroupKeys"],
		namespace["_showItemContextMenu"],
		namespace["_onRemoveMissingFiles"],
		namespace["_finishRemoveMissingFiles"],
	)


(
	_getSelectedFileGroupKeys,
	_showItemContextMenu,
	_onRemoveMissingFiles,
	_finishRemoveMissingFiles,
) = _loadFileGroupMethods()


def _menuLabels(menu: Mock) -> list[str]:
	"""Return labels appended to a fake wx menu."""
	return [call.args[1] for call in menu.Append.call_args_list]


class ManagerFileGroupTests(unittest.TestCase):
	"""Verify file-group context menu behavior."""

	def testFileGroupSelectionShowsRemoveMissingFiles(self) -> None:
		"""Show missing-file cleanup when selected entries include a file group."""
		menu = Mock()
		putItem = Mock()
		transferItem = Mock()
		removeMissingFilesItem = Mock()
		deleteItem = Mock()
		menu.Append.side_effect = [putItem, transferItem, removeMissingFilesItem, deleteItem]
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
			_itemKinds=("files",),
			_onDeleteItems=Mock(),
			_onRemoveMissingFiles=Mock(),
			_onPutItemOnClipboard=Mock(),
			_onTransferItemsToCategory=Mock(),
			controller=SimpleNamespace(),
			itemList=itemList,
		)
		manager._getSelectedFileGroupKeys = lambda: _getSelectedFileGroupKeys(manager)

		_showItemContextMenu(manager, fakeWx.DefaultPosition, 0, 1)

		self.assertIn("Remove &Missing Files", _menuLabels(menu))
		menu.Bind.assert_any_call(fakeWx.EVT_MENU, manager._onRemoveMissingFiles, removeMissingFilesItem)
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
			_onPutItemOnClipboard=Mock(),
			_onTransferItemsToCategory=Mock(),
			itemList=itemList,
		)
		manager._getSelectedFileGroupKeys = lambda: _getSelectedFileGroupKeys(manager)

		_showItemContextMenu(manager, fakeWx.DefaultPosition, 0, 1)

		self.assertNotIn("Remove &Missing Files", _menuLabels(menu))
		menu.Destroy.assert_called_once_with()

	def testPartialCleanupKeepsActiveReplacementFocused(self) -> None:
		"""Focus a cleaned replacement after the background result arrives."""
		result = SimpleNamespace(
			changedCount=1,
			removedCount=1,
			replacementIds={7: 9},
		)
		manager = SimpleNamespace(
			_isBeingDestroyed=False,
			_getSelectedCategory=Mock(return_value="Saved"),
			_getActiveItemIndex=Mock(return_value=0),
			_getActiveItemKey=Mock(return_value=7),
			_getSelectedItemKeys=Mock(return_value=(7,)),
			_getSelectedFileGroupKeys=Mock(return_value=(7,)),
			_confirm=Mock(return_value=True),
			_showInfo=Mock(),
			_getSurvivingNeighborKey=Mock(return_value=11),
			_reloadItemsFromController=Mock(),
			_isContentCurrent=Mock(return_value=False),
			_hasDirtyChanges=Mock(return_value=False),
			_loadActiveItem=Mock(),
			_showError=Mock(),
			IsShown=Mock(return_value=True),
			refreshFromController=Mock(),
			_isSearchSessionActive=False,
			controller=SimpleNamespace(startMissingFileCleanup=Mock()),
		)
		manager._finishRemoveMissingFiles = MethodType(_finishRemoveMissingFiles, manager)

		_onRemoveMissingFiles(manager, object())
		callback = manager.controller.startMissingFileCleanup.call_args.args[2]
		callback(result)

		manager.controller.startMissingFileCleanup.assert_called_once()
		manager._reloadItemsFromController.assert_called_once_with(preferredKey=9)
		manager._getSurvivingNeighborKey.assert_called_once_with(0, (7,))

	def testClosedManagerIgnoresCleanupCompletion(self) -> None:
		"""Ignore a cleanup result after the manager has been hidden."""
		manager = SimpleNamespace(
			_isBeingDestroyed=False,
			IsShown=Mock(return_value=False),
			_showInfo=Mock(),
			_showError=Mock(),
		)

		_finishRemoveMissingFiles(
			manager,
			SimpleNamespace(changedCount=1, removedCount=1, replacementIds={}),
			category="Saved",
			index=0,
			activeKey=7,
			selectedKeys=(7,),
			fileGroupKeys=(7,),
		)

		manager._showInfo.assert_not_called()
		manager._showError.assert_not_called()


if __name__ == "__main__":
	unittest.main()
