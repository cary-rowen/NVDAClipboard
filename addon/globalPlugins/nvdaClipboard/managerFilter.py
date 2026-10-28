# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Run the clipboard manager entry search session and its background result scan."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
from typing import TYPE_CHECKING

from logHandler import log
import wx

from .search import normalizeSearchText, splitSearchKeywords

if TYPE_CHECKING:
	from .storageModels import ClipboardItemType


_SEARCH_DELAY_MS = 100

ListState = tuple[int | None, int | None, tuple[int, ...]]
"""Active key, active index, and selected keys of the manager entry list."""


class ManagerSearchFilter[CategoryT]:
	"""Own the search box state, debounce, and cancellable result scan of the manager list.

	The owning window supplies the list, content, and session UI through callbacks,
	so this class only decides when to filter, which results are current, and which
	list state to restore. ``CategoryT`` is the window's opaque category identifier.
	"""

	def __init__(
		self,
		searchCtrl: wx.TextCtrl,
		clearSearchButton: wx.Button,
		*,
		isClosed: Callable[[], bool],
		getCategory: Callable[[], CategoryT | None],
		getEntries: Callable[[], tuple[tuple[int, str, ClipboardItemType], ...]],
		getSearchResultKeys: Callable[
			[CategoryT, tuple[tuple[int, str, ClipboardItemType], ...], tuple[str, ...], Event],
			tuple[int, ...],
		],
		getCurrentListState: Callable[[], ListState],
		canBeginSession: Callable[[], bool],
		onSessionChanged: Callable[[], None],
		refreshItems: Callable[..., int | None],
		exitSearch: Callable[[], bool],
		moveIntoResults: Callable[[], None],
		loadActiveResult: Callable[[], None],
		showError: Callable[[Exception], None],
	) -> None:
		"""Create idle search state for one manager window and bind its search box.

		:param searchCtrl: The search query control.
		:param clearSearchButton: The button that exits search.
		:param isClosed: Return whether the window is being destroyed.
		:param getCategory: Return the selected category.
		:param getEntries: Return all summaries of the selected category.
		:param getSearchResultKeys: Scan summaries for keywords outside the wx thread.
		:param getCurrentListState: Return the visible list state.
		:param canBeginSession: Return whether the current content allows searching.
		:param onSessionChanged: Update window UI after a session starts or is reset.
		:param refreshItems: Refilter the entry list while restoring the given list state.
		:param exitSearch: Exit search and restore the list state captured before searching.
		:param moveIntoResults: Focus and load the preferred result from completed results.
		:param loadActiveResult: Load the active result once completed results are shown.
		:param showError: Report a failure to the user.
		"""
		super().__init__()
		self._searchCtrl = searchCtrl
		self._clearSearchButton = clearSearchButton
		self._isClosed = isClosed
		self._getCategory = getCategory
		self._getEntries = getEntries
		self._getSearchResultKeys = getSearchResultKeys
		self._getCurrentListState = getCurrentListState
		self._canBeginSession = canBeginSession
		self._onSessionChanged = onSessionChanged
		self._refreshItems = refreshItems
		self._exitSearch = exitSearch
		self._moveIntoResults = moveIntoResults
		self._loadActiveResult = loadActiveResult
		self._showError = showError
		self._searchTimer: wx.CallLater | None = None
		self._searchKeywords: tuple[str, ...] = ()
		self._matchingSearchKeys: frozenset[int] | None = None
		self._searchFuture: Future[tuple[int, ...]] | None = None
		self._searchCancelEvent: Event | None = None
		self._searchExecutor = ThreadPoolExecutor(
			max_workers=1,
			thread_name_prefix="nvdaClipboard.managerSearch",
		)
		self._pendingSearchListState: ListState | None = None
		self._enterSearchResultsWhenReady = False
		self._isSearchSessionActive = False
		self._searchRestoreListState: ListState = (None, None, ())
		searchCtrl.Bind(wx.EVT_TEXT, self.onSearchTextChanged)

	@property
	def isSessionActive(self) -> bool:
		"""Whether a search session currently filters the list."""
		return self._isSearchSessionActive

	@property
	def hasKeywords(self) -> bool:
		"""Whether an applied query currently filters the list."""
		return bool(self._searchKeywords)

	@property
	def hasPendingQuery(self) -> bool:
		"""Whether a debounced query change has not been applied yet."""
		return self._searchTimer is not None

	@property
	def isUpdatingResults(self) -> bool:
		"""Whether a result scan for the applied query has not been shown yet."""
		return self._pendingSearchListState is not None

	@property
	def areResultsReady(self) -> bool:
		"""Whether completed results for the applied query are shown and no scan is running."""
		return self._searchFuture is None and self._matchingSearchKeys is not None

	@property
	def restoreListState(self) -> ListState:
		"""The list state captured when the search session started."""
		return self._searchRestoreListState

	def matches(self, itemKey: int) -> bool:
		"""Return whether one entry passes the applied keyword filter."""
		return not self._searchKeywords or (
			self._matchingSearchKeys is not None and itemKey in self._matchingSearchKeys
		)

	def getListState(self) -> ListState:
		"""Return stable list state, including state hidden by an in-progress search."""
		if self._isSearchSessionActive:
			if self._pendingSearchListState is not None:
				return self._pendingSearchListState
			if self._matchingSearchKeys is None:
				return self._searchRestoreListState
		return self._getCurrentListState()

	def cancelScan(self) -> None:
		"""Cancel background search work and invalidate its queued completion."""
		cancelEvent = self._searchCancelEvent
		self._searchCancelEvent = None
		if cancelEvent is not None:
			cancelEvent.set()
		future = self._searchFuture
		self._searchFuture = None
		if future is not None:
			future.cancel()

	def startScan(
		self,
		*,
		preferredKey: int | None,
		preferredIndex: int | None,
		selectedKeys: tuple[int, ...],
		clearResults: bool,
	) -> None:
		"""Start one cancellable payload-free search outside the wx thread."""
		category = self._getCategory()
		if category is None or not self._searchKeywords:
			return
		self.cancelScan()
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
			self._getSearchResultKeys,
			category,
			self._getEntries(),
			self._searchKeywords,
			cancelEvent,
		)
		self._searchFuture = future
		future.add_done_callback(self._queueScanCompletion)

	def _queueScanCompletion(
		self,
		future: Future[tuple[int, ...]],
	) -> None:
		"""Queue one live worker completion for the wx thread."""
		if self._isClosed() or future is not self._searchFuture:
			return
		try:
			wx.CallAfter(self._finishScan, future)
		except RuntimeError:
			if self._isClosed() or future is not self._searchFuture:
				return
			self._searchFuture = None
			self._searchCancelEvent = None
			self._pendingSearchListState = None
			self._enterSearchResultsWhenReady = False
			log.debugWarning("Could not schedule a clipboard manager search result.", exc_info=True)

	def _finishScan(
		self,
		future: Future[tuple[int, ...]],
	) -> None:
		"""Apply one current background result on the wx thread."""
		if self._isClosed() or future is not self._searchFuture:
			return
		self._searchFuture = None
		self._searchCancelEvent = None
		if self._searchTimer is not None:
			currentKeywords = splitSearchKeywords(normalizeSearchText(self._searchCtrl.GetValue()))
			if currentKeywords != self._searchKeywords:
				return
		try:
			matchingKeys = future.result()
		except Exception as error:
			self._matchingSearchKeys = None
			self._enterSearchResultsWhenReady = False
			self._refreshItems()
			self._showError(error)
			self._searchCtrl.SetFocus()
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
			self._moveIntoResults()
		else:
			self._loadActiveResult()

	def cancelPendingQuery(self) -> None:
		"""Cancel a pending debounced search update."""
		timer = self._searchTimer
		self._searchTimer = None
		if timer is not None:
			timer.Stop()

	def reset(self) -> None:
		"""Clear all query, worker, timer, and restoration state without moving focus."""
		self.cancelPendingQuery()
		self.cancelScan()
		self._searchCtrl.ChangeValue("")
		self._searchKeywords = ()
		self._matchingSearchKeys = None
		self._pendingSearchListState = None
		self._enterSearchResultsWhenReady = False
		self._isSearchSessionActive = False
		self._searchRestoreListState = (None, None, ())
		self._clearSearchButton.Disable()
		self._onSessionChanged()

	def beginSession(self) -> bool:
		"""Reject drafts or edits and capture state before filtering."""
		if self._isSearchSessionActive:
			return True
		if not self._canBeginSession():
			return False
		self._searchRestoreListState = self._getCurrentListState()
		self._isSearchSessionActive = True
		self._onSessionChanged()
		return True

	def applyQuery(self) -> bool:
		"""Start filtering the current query without moving keyboard focus."""
		self.cancelPendingQuery()
		queryValue = self._searchCtrl.GetValue()
		keywords = splitSearchKeywords(normalizeSearchText(queryValue))
		if not keywords:
			return self._exitSearch()
		if not self.beginSession():
			self._searchCtrl.ChangeValue("")
			self._clearSearchButton.Disable()
			return False
		keywordsChanged = keywords != self._searchKeywords
		previousKey, previousIndex, selectedKeys = self.getListState()
		if not keywordsChanged:
			if self._searchFuture is None and (
				self._matchingSearchKeys is None or self._pendingSearchListState is not None
			):
				self.startScan(
					preferredKey=previousKey,
					preferredIndex=previousIndex,
					selectedKeys=selectedKeys,
					clearResults=self._matchingSearchKeys is None,
				)
			self._clearSearchButton.Enable()
			return True
		self._searchKeywords = keywords
		self._clearSearchButton.Enable()
		self.startScan(
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
			self.applyQuery()
		except Exception as error:
			self._showError(error)
			self._searchCtrl.SetFocus()

	def endSession(self) -> ListState:
		"""Stop searching, reset all search state, and return the list state to restore."""
		self._enterSearchResultsWhenReady = False
		self.cancelPendingQuery()
		self.cancelScan()
		if self._isSearchSessionActive:
			listState = self._searchRestoreListState
		else:
			listState = self._getCurrentListState()
		self.reset()
		return listState

	def enterResults(self) -> None:
		"""Move from search to the best matching result and preview it."""
		if not self.applyQuery():
			return
		if not self._searchKeywords:
			return
		if not self.areResultsReady:
			self._enterSearchResultsWhenReady = True
			self._searchCtrl.SetFocus()
			return
		self._moveIntoResults()

	def onSearchTextChanged(self, event: wx.CommandEvent) -> None:
		"""Debounce live search after the query text changes."""
		self._enterSearchResultsWhenReady = False
		queryValue = self._searchCtrl.GetValue()
		if queryValue and not self._isSearchSessionActive:
			keywords = splitSearchKeywords(normalizeSearchText(queryValue))
			if keywords and not self.beginSession():
				self.cancelPendingQuery()
				self._searchCtrl.ChangeValue("")
				self._clearSearchButton.Disable()
				event.Skip()
				return
		self._clearSearchButton.Enable(bool(queryValue) or self._isSearchSessionActive)
		if self._searchTimer is None:
			self._searchTimer = wx.CallLater(_SEARCH_DELAY_MS, self._runScheduledSearch)
		else:
			self._searchTimer.Restart(_SEARCH_DELAY_MS)
		event.Skip()

	def prepareListInteraction(self, *, enterWhenReady: bool = False) -> bool:
		"""Flush debounce and reject list actions until current results are ready."""
		try:
			if self._searchTimer is not None and not self.applyQuery():
				return False
			if (
				self._isSearchSessionActive
				and self._searchFuture is None
				and self._matchingSearchKeys is None
				and not self.applyQuery()
			):
				return False
		except Exception as error:
			self._showError(error)
			self._searchCtrl.SetFocus()
			return False
		if not self._isSearchSessionActive:
			return True
		if not self.areResultsReady:
			if enterWhenReady:
				self._enterSearchResultsWhenReady = True
			self._searchCtrl.SetFocus()
			return False
		return True

	def shutdown(self, *, wait: bool) -> None:
		"""Stop pending search work and release the search worker thread."""
		self.cancelPendingQuery()
		self.cancelScan()
		self._searchKeywords = ()
		self._searchExecutor.shutdown(wait=wait, cancel_futures=True)

	def discard(self) -> None:
		"""Drop all search state and release the worker without waiting, for window destruction."""
		self.cancelPendingQuery()
		self.cancelScan()
		self._searchKeywords = ()
		self._matchingSearchKeys = None
		self._isSearchSessionActive = False
		self._pendingSearchListState = None
		self._searchExecutor.shutdown(wait=False, cancel_futures=True)
