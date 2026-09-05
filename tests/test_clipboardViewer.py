# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Focused tests for clipboard HTML extraction and safe rendering."""

from __future__ import annotations

from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests._module_loader import loadAddonModule


_MODULE_PATH = Path(__file__).parents[1] / "addon" / "globalPlugins" / "nvdaClipboard" / "clipboardViewer.py"

try:
	import markdown  # noqa: F401
	import nh3  # noqa: F401
except ImportError:
	clipboardViewer = None
else:
	clipboardMonitor = ModuleType("tests.clipboardMonitor")
	clipboardMonitor.ClipboardSnapshot = object
	clipboardViewer = loadAddonModule(
		"tests.clipboardViewer",
		_MODULE_PATH,
		injectedModules={
			"logHandler": SimpleNamespace(log=Mock()),
			"tests.clipboardMonitor": clipboardMonitor,
		},
	)


@unittest.skipIf(clipboardViewer is None, "NVDA runtime rendering dependencies are unavailable")
class ClipboardViewerTests(unittest.TestCase):
	"""Verify bounded CF_HTML parsing, sanitization, and common rendering."""

	def testExtractHtmlFragmentUsesByteOffsets(self) -> None:
		"""Use CF_HTML byte offsets correctly when the fragment has Unicode text."""
		body = "<html><body><!--StartFragment-->你好 <strong>world</strong><!--EndFragment--></body></html>".encode()
		headerTemplate = (
			b"Version:0.9\r\n"
			b"StartHTML:%010d\r\n"
			b"EndHTML:%010d\r\n"
			b"StartFragment:%010d\r\n"
			b"EndFragment:%010d\r\n\r\n"
		)
		headerLength = len(headerTemplate % (0, 0, 0, 0))
		startFragment = headerLength + body.index(b"<!--StartFragment-->") + len(b"<!--StartFragment-->")
		endFragment = headerLength + body.index(b"<!--EndFragment-->")
		payload = headerTemplate % (headerLength, headerLength + len(body), startFragment, endFragment) + body

		self.assertEqual(
			"你好 <strong>world</strong>",
			clipboardViewer.extractHtmlFragment(payload),
		)

	def testExtractHtmlFragmentFallsBackToMarkersWhenOffsetsAreMalformed(self) -> None:
		"""Recover a marker-delimited fragment when producer offsets are invalid."""
		payload = b"StartFragment:bad\r\n<!--StartFragment--><p>text</p><!--EndFragment-->"
		self.assertEqual("<p>text</p>", clipboardViewer.extractHtmlFragment(payload))

	def testSanitizeHtmlPreservesPassivePresentationContent(self) -> None:
		"""Keep passive structure, styling, links, metadata, and embedded raster images."""
		html = (
			"<section id='copy' class='document' aria-label='Copied content' data-source='test'>"
			"<a href='/docs' title='Documentation'>link</a>"
			"<table border='1' style='border-collapse:collapse'><tr><th colspan='2'>A</th></tr>"
			"<tr><td>1</td><td>2</td></tr></table>"
			"<img src='data:image/png;base64,AAAA' alt='diagram' width='10' height='20'>"
			"</section>"
		)
		rendered = clipboardViewer.sanitizeHtml(html)
		self.assertIn(
			'<section id="copy" class="document" aria-label="Copied content" data-source="test">',
			rendered,
		)
		self.assertIn('<a href="/docs"', rendered)
		self.assertIn('<table border="1" style="border-collapse:collapse">', rendered)
		self.assertIn('<th colspan="2">A</th>', rendered)
		self.assertIn('<img src="data:image/png;base64,AAAA" alt="diagram" width="10" height="20">', rendered)

	def testSanitizeHtmlRemovesActiveContentAndUnsafeSources(self) -> None:
		"""Remove executable markup while retaining readable fallback text."""
		html = (
			"<script>alert(1)</script><style>body { color: red }</style>"
			"<iframe src='https://example.test/frame'>frame fallback</iframe>"
			"<form action='https://example.test'><button>submit</button></form>"
			"<p onclick='alert(1)' style='color:red;position:fixed;background-image:url(https://example.test/a)'>text</p>"
			"<img src='https://example.test/a.png' alt='remote image'>"
			"<a href='javascript:alert(1)'>bad</a><a href='//example.test'>network</a>"
			"<a href='https://example.test'>good</a>"
		)
		rendered = clipboardViewer.sanitizeHtml(html)
		self.assertNotIn("alert(1)", rendered)
		self.assertNotIn("<script", rendered)
		self.assertNotIn("<style", rendered)
		self.assertNotIn("<iframe", rendered)
		self.assertNotIn("<form", rendered)
		self.assertNotIn("onclick", rendered)
		self.assertNotIn("javascript:", rendered)
		self.assertNotIn('href="//example.test"', rendered)
		self.assertIn("<p>text</p>", rendered)
		self.assertIn('<img alt="remote image">', rendered)
		self.assertIn("good", rendered)

	def testSanitizeHtmlPreservesMathMl(self) -> None:
		"""Keep MathML supplied by a rich-text clipboard after sanitization."""
		rendered = clipboardViewer.sanitizeHtml(
			'<math xmlns="http://www.w3.org/1998/Math/MathML"><mfrac><mi>x</mi><mn>2</mn></mfrac></math>',
		)
		self.assertIn("<math", rendered)
		self.assertIn("<mfrac>", rendered)

	def testRenderTextSupportsMarkdownTablesCodeAndLatex(self) -> None:
		"""Render the common structured forms users copy from documentation."""
		rendered = clipboardViewer.renderText(
			"# Heading\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n`code` and `$notmath$` and $x^2$ and \\(y+1\\)",
		)
		self.assertIn("<h1>Heading</h1>", rendered)
		self.assertIn("<table>", rendered)
		self.assertIn("<code>code</code>", rendered)
		self.assertIn("<code>$notmath$</code>", rendered)
		if clipboardViewer.LaTeX2MathMLExtension is not None:
			self.assertGreaterEqual(rendered.count("<math"), 2)

	def testRenderTextFallsBackWhenSanitizationRemovesRawHtml(self) -> None:
		"""Preserve plain text that Markdown exposes only as removable HTML."""
		rendered = clipboardViewer.renderText("<script>alert(1)</script>")
		self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered)

	def testRenderTextUnwrapsMarkdownDocumentFence(self) -> None:
		"""Treat a copied Markdown document fence as a wrapper, not a code block."""
		rendered = clipboardViewer.renderText("```markdown\n# Heading\n\n- item\n```")
		self.assertIn("<h1>Heading</h1>", rendered)
		self.assertIn("<li>item</li>", rendered)

	def testRenderTextKeepsEscapedParenthesizedLatexLiteral(self) -> None:
		"""Do not turn a Markdown-escaped ``\\(formula\\)`` into MathML."""
		rendered = clipboardViewer.renderText(r"Literal \\(x+1\\)")
		self.assertIn(r"\(x+1\)", rendered)
		self.assertNotIn("<math", rendered)

	def testMathConversionPreservesCurrencyText(self) -> None:
		"""Do not consume currency text while looking for dollar-delimited formulas."""
		rendered = clipboardViewer._convertMathText(
			"The price is $5.00 and $x^2$",
			lambda formula, _display: f"<math>{formula}</math>",
		)
		self.assertEqual("The price is $5.00 and $x^2$", rendered)
		self.assertIn(
			"The price is $5.00 and $x^2$",
			clipboardViewer.renderText("The price is $5.00 and $x^2$"),
		)

	def testRenderHtmlConvertsVisibleLatexButNotCode(self) -> None:
		"""Convert formulas in rich text while preserving literal code samples."""
		html = clipboardViewer.renderHtml("<p>$x+1$</p><pre>$y+1$</pre><code>$z+1$</code>")
		if clipboardViewer.converter is None:
			self.assertIn("$x+1$", html)
			return
		self.assertEqual(1, html.count("<math"))
		self.assertIn("$y+1$", html)
		self.assertIn("$z+1$", html)
		currencyHtml = clipboardViewer.renderHtml("<p>The price is $5.00 and $x^2$</p>")
		self.assertIn("The price is $5.00 and $x^2$", currencyHtml)

	def testRenderSnapshotPrefersRichHtmlAndFallsBackWhenItIsEmpty(self) -> None:
		"""Prefer a readable rich fragment, but retain text if sanitization removes it."""
		richSnapshot = SimpleNamespace(html=b"<p><strong>rich</strong></p>", text="plain", files=())
		rendered = clipboardViewer.renderSnapshot(richSnapshot)
		self.assertIn("<strong>rich</strong>", rendered)

		emptySnapshot = SimpleNamespace(
			html=b"<!--StartFragment--><script>alert(1)</script><!--EndFragment-->",
			text="plain",
			files=(),
		)
		rendered = clipboardViewer.renderSnapshot(emptySnapshot)
		self.assertIn("plain", rendered)

	def testRenderSnapshotFallsBackWhenSanitizedHtmlHasNoReadableContent(self) -> None:
		"""Do not hide plain text behind empty sanitized markup."""
		snapshot = SimpleNamespace(
			html=b"<p><img src='https://example.test/x'></p>",
			text="plain",
			files=(),
		)
		self.assertIn("plain", clipboardViewer.renderSnapshot(snapshot))

	def testRenderSnapshotIgnoresHiddenHtmlWhenCheckingReadability(self) -> None:
		"""Fall back to plain text when the sanitized fragment only contains hidden nodes."""
		snapshot = SimpleNamespace(
			html=(
				b"<section><p style='display:none'>hidden</p><p aria-hidden='true'>also hidden</p></section>"
			),
			text="plain",
			files=(),
		)
		self.assertIn("plain", clipboardViewer.renderSnapshot(snapshot))

	def testShowSnapshotRendersHtmlOnlyContent(self) -> None:
		"""Render a snapshot that has HTML but no plain-text clipboard format."""
		snapshot = SimpleNamespace(html=b"<p><strong>rich</strong></p>", text="", files=())
		browseableMessage = Mock()
		with patch.dict(sys.modules, {"ui": SimpleNamespace(browseableMessage=browseableMessage)}):
			clipboardViewer.showSnapshot(snapshot, "title")
		self.assertIn("<strong>rich</strong>", browseableMessage.call_args.args[0])

	def testShowSnapshotDoesNotAddCopyButtonInEitherMode(self) -> None:
		"""Open rendered and raw content without adding a copy button."""
		snapshot = SimpleNamespace(html=b"<p><strong>rich</strong></p>", text="plain", files=())
		browseableMessage = Mock()
		with patch.dict(sys.modules, {"ui": SimpleNamespace(browseableMessage=browseableMessage)}):
			clipboardViewer.showSnapshot(snapshot, "title")
			self.assertNotIn("copyButton", browseableMessage.call_args.kwargs)
			self.assertTrue(browseableMessage.call_args.kwargs["isHtml"])

		browseableMessage.reset_mock()
		with patch.dict(sys.modules, {"ui": SimpleNamespace(browseableMessage=browseableMessage)}):
			clipboardViewer.showSnapshot(snapshot, "title", showAsPlainText=True)
		browseableMessage.assert_called_once_with("plain", title="title", closeButton=True)


if __name__ == "__main__":
	unittest.main()
