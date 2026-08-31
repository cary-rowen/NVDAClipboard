"""Tests for clipboard manager search session guards without loading NVDA."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call

from _manager_method_loader import loadManagerClassMethods


_beginSearchSession = loadManagerClassMethods({"_beginSearchSession"})["_beginSearchSession"]
_onSearchTextChanged = loadManagerClassMethods(
	{"_onSearchTextChanged"},
	{
		"normalizeSearchText": lambda value: value,
		"splitSearchKeywords": lambda value: (value,) if value else (),
	},
)["_onSearchTextChanged"]


class ManagerSearchTests(unittest.TestCase):
	"""Verify search does not replace content that is still a draft."""

	def testSearchRejectsActiveDraft(self) -> None:
		"""Leave a draft untouched instead of letting search previews replace it."""
		manager = SimpleNamespace(
			_isSearchSessionActive=False,
			_contentEditable=True,
			_contentItemKey=None,
			_confirmDirtyChanges=Mock(),
		)

		self.assertFalse(_beginSearchSession(manager))
		manager._confirmDirtyChanges.assert_not_called()

	def testSearchRejectsDirtyStoredContent(self) -> None:
		"""Leave stored edits for an explicit save or discard before searching."""
		manager = SimpleNamespace(
			_isSearchSessionActive=False,
			_contentEditable=True,
			_contentItemKey=7,
			_hasDirtyChanges=Mock(return_value=True),
			_confirmDirtyChanges=Mock(),
		)

		self.assertFalse(_beginSearchSession(manager))
		manager._confirmDirtyChanges.assert_not_called()

	def testExitSearchRestoresEditorEditability(self) -> None:
		"""Restore editing when the pre-search stored item becomes active again."""
		exitSearch = loadManagerClassMethods({"_exitSearch"})["_exitSearch"]
		editor = Mock()
		manager = SimpleNamespace(
			_isSearchSessionActive=True,
			_searchRestoreListState=(1, 0, (1,)),
			_contentEditable=True,
			_getSelectedCategory=Mock(return_value="history"),
			_cancelPendingSearch=Mock(),
			_cancelSearchScan=Mock(),
			_resetSearchState=Mock(side_effect=lambda: editor.SetEditable(False)),
			_refreshItems=Mock(),
			_isContentCurrent=Mock(return_value=True),
			editor=editor,
			itemList=Mock(),
		)

		self.assertTrue(exitSearch(manager))
		editor.SetEditable.assert_has_calls([call(False), call(True)])

	def testSearchClearsQueryWhileContentIsNotReady(self) -> None:
		"""Clear a query until the current draft or edit is explicitly resolved."""
		searchCtrl = Mock()
		searchCtrl.GetValue.return_value = "needle"
		manager = SimpleNamespace(
			_isSearchSessionActive=False,
			_beginSearchSession=Mock(return_value=False),
			_cancelPendingSearch=Mock(),
			clearSearchButton=Mock(),
			searchCtrl=searchCtrl,
		)
		event = Mock()

		_onSearchTextChanged(manager, event)

		manager._cancelPendingSearch.assert_called_once_with()
		searchCtrl.ChangeValue.assert_called_once_with("")
		manager.clearSearchButton.Disable.assert_called_once_with()
		event.Skip.assert_called_once_with()


if __name__ == "__main__":
	unittest.main()
