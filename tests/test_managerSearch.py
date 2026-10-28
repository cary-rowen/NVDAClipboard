# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Tests for clipboard manager search session guards without loading NVDA."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from tests._manager_loader import loadManagerModule


_MANAGER_MODULE = loadManagerModule("nvdaClipboardManagerSearchTests")
ClipboardManagerFrame = _MANAGER_MODULE.ClipboardManagerFrame
ManagerSearchFilter = _MANAGER_MODULE.ManagerSearchFilter
_FILTER_MODULE = sys.modules[ManagerSearchFilter.__module__]
_canBeginSearchSession = ClipboardManagerFrame._canBeginSearchSession
_exitSearch = ClipboardManagerFrame._exitSearch
_onItemFocused = ClipboardManagerFrame._onItemFocused


class _ImmediateExecutor:
	"""Run submitted search scans synchronously so completions are deterministic."""

	def submit(self, function: Callable[..., tuple[int, ...]], *args: object) -> Future[tuple[int, ...]]:
		"""Return an already completed future for one scan."""
		future: Future[tuple[int, ...]] = Future()
		future.set_result(function(*args))
		return future

	def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
		"""Accept shutdown requests without owning any thread."""


class ManagerSearchTests(unittest.TestCase):
	"""Verify search does not replace content that is still a draft."""

	def _createFilter(self, **callbacks: object) -> SimpleNamespace:
		"""Create a real search filter with fake widgets and window callbacks."""
		searchCtrl = Mock()
		searchCtrl.GetValue.return_value = "needle"
		clearSearchButton = Mock()
		arguments: dict[str, object] = {
			"isClosed": Mock(return_value=False),
			"getCategory": Mock(return_value="history"),
			"getEntries": Mock(return_value=((7, "needle", "plainText"),)),
			"getSearchResultKeys": Mock(return_value=(7,)),
			"getCurrentListState": Mock(return_value=(7, 0, (7,))),
			"canBeginSession": Mock(return_value=True),
			"onSessionChanged": Mock(),
			"refreshItems": Mock(),
			"exitSearch": Mock(return_value=True),
			"moveIntoResults": Mock(),
			"loadActiveResult": Mock(),
			"showError": Mock(),
		}
		arguments.update(callbacks)
		searchFilter = ManagerSearchFilter(searchCtrl, clearSearchButton, **arguments)
		searchFilter._searchExecutor.shutdown(wait=True)
		searchFilter._searchExecutor = _ImmediateExecutor()
		return SimpleNamespace(
			filter=searchFilter,
			searchCtrl=searchCtrl,
			clearSearchButton=clearSearchButton,
			**arguments,
		)

	def testSearchRejectsActiveDraft(self) -> None:
		"""Leave a draft untouched instead of letting search previews replace it."""
		manager = SimpleNamespace(
			_contentEditable=True,
			_contentItemKey=None,
			_confirmDirtyChanges=Mock(),
		)

		self.assertFalse(_canBeginSearchSession(manager))
		manager._confirmDirtyChanges.assert_not_called()

	def testSearchRejectsDirtyStoredContent(self) -> None:
		"""Leave stored edits for an explicit save or discard before searching."""
		manager = SimpleNamespace(
			_contentEditable=True,
			_contentItemKey=7,
			_hasDirtyChanges=Mock(return_value=True),
			_confirmDirtyChanges=Mock(),
		)

		self.assertFalse(_canBeginSearchSession(manager))
		manager._confirmDirtyChanges.assert_not_called()

	def testRejectedSessionKeepsFilterIdle(self) -> None:
		"""Do not capture list state or lock the window when search is rejected."""
		parts = self._createFilter(canBeginSession=Mock(return_value=False))

		self.assertFalse(parts.filter.beginSession())
		self.assertFalse(parts.filter.isSessionActive)
		parts.getCurrentListState.assert_not_called()
		parts.onSessionChanged.assert_not_called()

	def testExitSearchRestoresEditorEditability(self) -> None:
		"""Restore editing when the pre-search stored item becomes active again."""
		editor = Mock()

		def endSession() -> tuple[int | None, int | None, tuple[int, ...]]:
			editor.SetEditable(False)
			return (1, 0, (1,))

		manager = SimpleNamespace(
			_contentEditable=True,
			_getSelectedCategory=Mock(return_value="history"),
			_searchFilter=SimpleNamespace(endSession=Mock(side_effect=endSession)),
			_refreshItems=Mock(),
			_isContentCurrent=Mock(return_value=True),
			editor=editor,
			itemList=Mock(),
		)

		self.assertTrue(_exitSearch(manager))
		manager._refreshItems.assert_called_once_with(preferredKey=1, preferredIndex=0, selectedKeys=(1,))
		editor.SetEditable.assert_has_calls([call(False), call(True)])

	def testSearchClearsQueryWhileContentIsNotReady(self) -> None:
		"""Clear a query until the current draft or edit is explicitly resolved."""
		parts = self._createFilter(canBeginSession=Mock(return_value=False))
		event = Mock()

		parts.filter.onSearchTextChanged(event)

		self.assertFalse(parts.filter.hasPendingQuery)
		parts.searchCtrl.ChangeValue.assert_called_once_with("")
		parts.clearSearchButton.Disable.assert_called_once_with()
		event.Skip.assert_called_once_with()

	def testListInteractionWaitsForResultsAndEntersWhenReady(self) -> None:
		"""Reject list actions until the scan completes, then enter the results."""
		parts = self._createFilter()
		wxModule = Mock()
		patcher = patch.object(_FILTER_MODULE, "wx", wxModule)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.assertTrue(parts.filter.beginSession())
		self.assertFalse(parts.filter.areResultsReady)

		self.assertFalse(parts.filter.prepareListInteraction(enterWhenReady=True))

		self.assertFalse(parts.filter.areResultsReady)
		parts.searchCtrl.SetFocus.assert_called_once_with()
		finishScan, future = wxModule.CallAfter.call_args.args
		finishScan(future)
		self.assertTrue(parts.filter.areResultsReady)
		self.assertTrue(parts.filter.matches(7))
		self.assertFalse(parts.filter.matches(8))
		parts.moveIntoResults.assert_called_once_with()
		parts.loadActiveResult.assert_not_called()
		self.assertTrue(parts.filter.prepareListInteraction())

		parts.filter.startScan(preferredKey=7, preferredIndex=0, selectedKeys=(7,), clearResults=False)
		self.assertTrue(parts.filter.matches(7))
		self.assertFalse(parts.filter.areResultsReady)
		self.assertFalse(parts.filter.prepareListInteraction())

	def testFocusedItemSyncsOrLoadsActiveItem(self) -> None:
		"""Sync a moved focus during search or for clean content, and load dirty content now."""
		wxModule = Mock()
		wxModule.GetMouseState.return_value.RightIsDown.return_value = False
		patcher = patch.object(_MANAGER_MODULE, "wx", wxModule)
		patcher.start()
		self.addCleanup(patcher.stop)
		for isSearchSessionActive, isDirty, expectsSync in (
			(True, True, True),
			(False, False, True),
			(False, True, False),
		):
			with self.subTest(isSearchSessionActive=isSearchSessionActive, isDirty=isDirty):
				manager = SimpleNamespace(
					_activeItemKey=1,
					_isRefreshingList=False,
					_isSearchSessionActive=isSearchSessionActive,
					_searchFilter=SimpleNamespace(prepareListInteraction=Mock(return_value=True)),
					_getActiveItemKey=Mock(return_value=2),
					_hasDirtyChanges=Mock(return_value=isDirty),
					_loadActiveItem=Mock(),
					_playActiveItemCue=Mock(),
					_scheduleActiveItemLoad=Mock(),
					_showError=Mock(),
					_syncActiveItemFromList=Mock(),
				)

				_onItemFocused(manager, Mock())

				manager._playActiveItemCue.assert_called_once_with()
				if expectsSync:
					manager._syncActiveItemFromList.assert_called_once_with()
					manager._scheduleActiveItemLoad.assert_called_once_with()
					manager._loadActiveItem.assert_not_called()
				else:
					manager._syncActiveItemFromList.assert_not_called()
					manager._scheduleActiveItemLoad.assert_not_called()
					manager._loadActiveItem.assert_called_once_with()


if __name__ == "__main__":
	unittest.main()
