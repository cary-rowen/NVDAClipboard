# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Build localized summary and feedback text for clipboard content and stored entries."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import addonHandler
import colors

from ..clipboardMonitor import ClipboardContentType
from ..storageModels import TEXT_ITEM_TYPES, ClipboardItemType

if TYPE_CHECKING:
	from ..clipboardMonitor import ClipboardSnapshot
	from ..fileSize import FileSizeProgress
	from ..storageModels import ClipboardItem, ClipboardItemContent, ClipboardItemSummary
	from ..textStats import TextStatistics, TextStatisticsProgress


addonHandler.initTranslation()

_CANONICAL_SOLID_COLOR_NAMES = {
	# Translators: Exact name of the RGB color (255, 0, 0).
	(255, 0, 0): _("red"),
	# Translators: Exact name of the RGB color (0, 255, 0).
	(0, 255, 0): _("green"),
	# Translators: Exact name of the RGB color (0, 0, 255).
	(0, 0, 255): _("blue"),
	# Translators: Exact name of the RGB color (255, 255, 0).
	(255, 255, 0): _("yellow"),
	# Translators: Exact name of the RGB color (0, 255, 255).
	(0, 255, 255): _("cyan"),
	# Translators: Exact name of the RGB color (255, 0, 255).
	(255, 0, 255): _("magenta"),
}


def getTextOrCharacterCount(text: str, maxLength: int = 1024) -> str:
	"""Return text shorter than ``maxLength``, or its localized character count."""
	textLength = len(text)
	if textLength < maxLength:
		return text
	# Translators: Presented instead of lengthy text in clipboard feedback.
	return ngettext("%d character", "%d characters", textLength) % textLength


def formatItemSummary(item: ClipboardItemSummary) -> str:
	"""Return a concise content summary without a type prefix."""
	if item.contentType in TEXT_ITEM_TYPES:
		if item.textPreview:
			return item.textPreview
		# Translators: Clipboard list summary when text contains no visible characters.
		return _("No visible text")
	if item.contentType == ClipboardItemType.IMAGE:
		return _formatImageSummary(item)
	names = ", ".join(item.filesPreview)
	if item.fileCount > len(item.filesPreview):
		# Translators: Clipboard file summary when additional file names are omitted.
		return _("{names}, …").format(names=names)
	return names


def _formatImageSummary(item: ClipboardItemSummary) -> str:
	"""Return concise image metadata for a stored-entry summary."""
	# Translators: Concise image metadata used inside clipboard list labels.
	return _("{width} by {height} pixels, {size}").format(
		width=item.imageWidth or 0,
		height=item.imageHeight or 0,
		size=formatDataSize(item.imageByteCount),
	)


def formatImageDetails(item: ClipboardItemContent) -> str:
	"""Return multiline details describing a stored clipboard image."""
	# Translators: Multiline details shown for a stored clipboard image.
	return _(
		"Image\nDimensions: {width} by {height} pixels\nColor depth: {depth} bits\nStored size: {size}",
	).format(
		width=item.imageWidth or 0,
		height=item.imageHeight or 0,
		depth=item.imageBitDepth or 0,
		size=formatDataSize(item.imageByteCount),
	)


def formatDataSize(byteCount: int) -> str:
	"""Return a compact localized binary-data size."""
	if byteCount >= 1024 * 1024 * 1024 * 1024:
		# Translators: Binary data size in terabytes.
		return _("{size:.1f} TB").format(size=byteCount / (1024 * 1024 * 1024 * 1024))
	if byteCount >= 1024 * 1024 * 1024:
		# Translators: Binary data size in gigabytes.
		return _("{size:.1f} GB").format(size=byteCount / (1024 * 1024 * 1024))
	if byteCount >= 1024 * 1024:
		# Translators: Binary data size in megabytes.
		return _("{size:.1f} MB").format(size=byteCount / (1024 * 1024))
	if byteCount >= 1024:
		# Translators: Binary data size in kilobytes.
		return _("{size:.1f} KB").format(size=byteCount / 1024)
	return ngettext(
		# Translators: Binary data size in bytes.
		"{count} byte",
		"{count} bytes",
		byteCount,
	).format(count=byteCount)


def formatFileSizeSummary(contentSummary: str, progress: FileSizeProgress) -> str:
	"""Return the latest size and item counts for the current clipboard files."""
	hasResults = bool(progress.fileCount or progress.directoryCount)
	if hasResults:
		details = [formatDataSize(progress.byteCount)]
		if progress.fileCount:
			details.append(
				ngettext(
					# Translators: Number of regular files included in a clipboard size calculation.
					"{count} file",
					"{count} files",
					progress.fileCount,
				).format(count=progress.fileCount),
			)
		if progress.directoryCount:
			details.append(
				ngettext(
					# Translators: Number of folders included in a clipboard size calculation.
					"{count} folder",
					"{count} folders",
					progress.directoryCount,
				).format(count=progress.directoryCount),
			)
		# Translators: Separator between file size, file count, and folder count.
		detailsText = _(", ").join(details)
		if progress.isComplete:
			if progress.isIncomplete:
				# Translators: Clipboard file calculation with partial size and item counts.
				summary = _("{summary}; contents total about {details}").format(
					details=detailsText,
					summary=contentSummary,
				)
			else:
				# Translators: Completed clipboard file calculation with size and item counts.
				summary = _("{summary}; contents total {details}").format(
					details=detailsText,
					summary=contentSummary,
				)
		else:
			# Translators: Clipboard file calculation with size and item counts accumulated so far.
			summary = _("{summary}; calculating contents, {details}").format(
				details=detailsText,
				summary=contentSummary,
			)
	else:
		if progress.isComplete:
			# Translators: Clipboard file calculation where no content details were available.
			summary = _("{summary}; could not calculate contents").format(summary=contentSummary)
		else:
			# Translators: Clipboard file calculation before any content details are available.
			summary = _("{summary}; calculating contents").format(summary=contentSummary)
	return summary


def formatTextStatisticsReport(
	contentSummary: str,
	lineNumber: int,
	columnNumber: int,
	progress: TextStatisticsProgress,
) -> str:
	"""Return exact completed text statistics or the current calculation state with the position."""
	summary = _(
		# Translators: Clipboard text type followed by the current one-based navigation line and column.
		"{summary}: line {line}, column {column}",
	).format(summary=contentSummary, line=lineNumber, column=columnNumber)
	if not progress.isComplete:
		summary = _(
			# Translators: Clipboard text summary with its position while exact statistics are calculated.
			"{summary}; calculating; check again shortly",
		).format(summary=summary)
	elif progress.statistics is not None:
		summary = _formatTextStatisticsSummary(summary, progress.statistics)
	return summary


def _formatTextStatisticsSummary(summary: str, statistics: TextStatistics) -> str:
	"""Append exact, non-zero Unicode statistics to a positioned text summary."""
	details = [
		ngettext(
			# Translators: Number of user-perceived Unicode characters including whitespace; line breaks are excluded.
			"{count} character (including whitespace)",
			"{count} characters (including whitespace)",
			statistics.characterCount,
		).format(count=statistics.characterCount),
		ngettext(
			# Translators: Number of user-perceived Unicode characters excluding whitespace and line breaks.
			"{count} character (excluding whitespace)",
			"{count} characters (excluding whitespace)",
			statistics.nonWhitespaceCharacterCount,
		).format(count=statistics.nonWhitespaceCharacterCount),
	]
	attributes = []
	if statistics.hanCharacterCount:
		attributes.append(
			ngettext(
				# Translators: Number of Han ideographic code points in clipboard text.
				"{count} Han character",
				"{count} Han characters",
				statistics.hanCharacterCount,
			).format(count=statistics.hanCharacterCount),
		)
	if statistics.punctuationCount:
		attributes.append(
			ngettext(
				# Translators: Number of Unicode punctuation grapheme clusters in clipboard text.
				"{count} punctuation mark",
				"{count} punctuation marks",
				statistics.punctuationCount,
			).format(count=statistics.punctuationCount),
		)
	if statistics.symbolCount:
		attributes.append(
			ngettext(
				# Translators: Number of Unicode symbol grapheme clusters in clipboard text.
				"{count} symbol",
				"{count} symbols",
				statistics.symbolCount,
			).format(count=statistics.symbolCount),
		)
	# Translators: Separator between exact clipboard text statistics.
	detailsText = _(", ").join(details)
	if attributes:
		# Translators: Selected character attributes follow the main, additive text statistics.
		detailsText = _("{details}; including {attributes}").format(
			details=detailsText,
			attributes=_(", ").join(attributes),
		)
	return ngettext(
		# Translators: Total explicit lines and exact statistics after the clipboard type and current position.
		"{summary}, {count} line in total; {details}",
		"{summary}, {count} lines in total; {details}",
		statistics.lineCount,
	).format(
		summary=summary,
		count=statistics.lineCount,
		details=detailsText,
	)


def formatCurrentSummary(snapshot: ClipboardSnapshot) -> str:
	"""Return a concise description of the current system clipboard."""
	if snapshot.contentType == ClipboardContentType.TEXT:
		# Translators: Type-only summary of plain text before exact statistics are available.
		return _("Plain text")
	if snapshot.contentType == ClipboardContentType.FORMATTED_TEXT:
		# Translators: Type-only summary of formatted text before exact statistics are available.
		return _("Formatted text")
	if snapshot.contentType == ClipboardContentType.TEXT_AND_IMAGE:
		# Translators: Type of clipboard content containing both text and an image.
		return _formatCurrentImageSummary(snapshot, _("Text and image"))
	if snapshot.contentType == ClipboardContentType.FILES:
		itemCount = len(snapshot.files)
		if snapshot.filesWereCut:
			return ngettext(
				# Translators: Number of top-level file system items cut to the clipboard.
				"Cut files: {count} item",
				"Cut files: {count} items",
				itemCount,
			).format(count=itemCount)
		if snapshot.filesWereLinked:
			return ngettext(
				# Translators: Number of top-level file system items linked from the clipboard.
				"Linked files: {count} item",
				"Linked files: {count} items",
				itemCount,
			).format(count=itemCount)
		return ngettext(
			# Translators: Number of top-level file system items copied to the clipboard.
			"Copied files: {count} item",
			"Copied files: {count} items",
			itemCount,
		).format(count=itemCount)
	if snapshot.contentType == ClipboardContentType.IMAGE:
		# Translators: Type of image-only clipboard content.
		return _formatCurrentImageSummary(snapshot, _("Image"))
	if snapshot.contentType == ClipboardContentType.PROTECTED:
		# Translators: Summary for clipboard content excluded from monitoring by its source application.
		return _("Protected clipboard content is not available")
	if snapshot.contentType == ClipboardContentType.EMPTY:
		# Translators: Summary used when the system clipboard is empty.
		return _("Clipboard is empty")
	# Translators: Summary used for clipboard data this add-on cannot navigate.
	return _("Unsupported clipboard content")


def _formatCurrentImageSummary(snapshot: ClipboardSnapshot, contentType: str) -> str:
	"""Return image metadata and trustworthy pixel properties for the current clipboard."""
	if not snapshot.imageWidth or not snapshot.imageHeight:
		return contentType
	if snapshot.imageWidth == snapshot.imageHeight:
		# Translators: Orientation of an image whose width and height are equal.
		orientation = _("square")
	elif snapshot.imageWidth > snapshot.imageHeight:
		# Translators: Orientation of an image wider than it is tall.
		orientation = _("landscape")
	else:
		# Translators: Orientation of an image taller than it is wide.
		orientation = _("portrait")
	# Translators: Clipboard image type, dimensions, and orientation.
	summary = _("{summary}: {width} by {height} pixels, {orientation}").format(
		summary=contentType,
		width=snapshot.imageWidth,
		height=snapshot.imageHeight,
		orientation=orientation,
	)
	if snapshot.imageTransparentPercentage == 100:
		# Translators: Exact image property indicating that every pixel is fully transparent.
		return _("{summary}; fully transparent").format(summary=summary)
	if snapshot.imageColor is not None and snapshot.imageColorPercentage == 100:
		# Translators: Exact image property indicating that every pixel has the same color.
		return _("{summary}; solid {color}").format(
			summary=summary,
			color=_CANONICAL_SOLID_COLOR_NAMES.get(snapshot.imageColor)
			or colors.RGB(*snapshot.imageColor).name,
		)
	if snapshot.imageTransparentPercentage:
		# Translators: Image summary followed by the percentage of fully transparent pixels.
		return _("{summary}; fully transparent pixels: {percentage}%").format(
			summary=summary,
			percentage=f"{snapshot.imageTransparentPercentage:g}",
		)
	if snapshot.imageColor is not None and snapshot.imageColorPercentage:
		# Translators: Image summary followed by the percentage of black or white pixels.
		return _("{summary}; {color} pixels: {percentage}%").format(
			summary=summary,
			color=_CANONICAL_SOLID_COLOR_NAMES.get(snapshot.imageColor)
			or colors.RGB(*snapshot.imageColor).name,
			percentage=f"{snapshot.imageColorPercentage:g}",
		)
	return summary


def formatPutOnClipboardConfirmation(item: ClipboardItem) -> str:
	"""Return a type-specific confirmation after putting a stored entry on the clipboard."""
	if item.contentType == ClipboardItemType.FILES:
		return ngettext(
			# Translators: Confirmation after putting one or multiple files on the clipboard.
			"{count} file put on the clipboard",
			"{count} files put on the clipboard",
			len(item.files),
		).format(count=len(item.files))
	if item.contentType == ClipboardItemType.IMAGE:
		# Translators: Confirmation after putting an image on the clipboard.
		return _("Image put on the clipboard")
	if item.contentType == ClipboardItemType.TEXT_AND_IMAGE:
		# Translators: Confirmation after putting mixed text and image content on the clipboard.
		return _("Text and image put on the clipboard")
	# Translators: Confirmation after putting plain or formatted text on the clipboard.
	return _("Text put on the clipboard")


def formatFileNavigationText(files: tuple[str, ...]) -> str:
	"""Return one navigable line for each file path on the clipboard."""
	lines: list[str] = []
	for index, filePath in enumerate(files, start=1):
		name = Path(filePath).name or filePath
		# Translators: Navigable clipboard file entry with name, position, and full path.
		line = _("{name}, item {index} of {count}, {path}").format(
			name=name,
			index=index,
			count=len(files),
			path=filePath,
		)
		lines.append(line)
	# Translators: Summary used when a file-list clipboard format contains no paths.
	return "\n".join(lines) or _("No files in clipboard")
