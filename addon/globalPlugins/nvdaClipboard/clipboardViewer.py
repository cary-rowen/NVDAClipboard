# A part of the NVDA Clipboard add-on for NVDA.
# Copyright (C) 2026 Cary-rowen <cary-rowen@outlook.com>
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.

"""Render untrusted clipboard text and HTML for NVDA's browseable message."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from html import escape, unescape
from html.parser import HTMLParser
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from xml.etree import ElementTree
from xml.etree.ElementTree import Element

import nh3
from logHandler import log
from markdown import Markdown, markdown
from markdown.extensions import Extension
from markdown.inlinepatterns import InlineProcessor
from markdown.postprocessors import Postprocessor

if TYPE_CHECKING:
	from .clipboardMonitor import ClipboardSnapshot

try:
	from l2m4m import LaTeX2MathMLExtension
	from latex2mathml import converter
except ImportError:  # pragma: no cover - available in supported NVDA builds.
	LaTeX2MathMLExtension = None
	converter = None


if converter is not None:

	class _LatexInlineProcessor(InlineProcessor):
		"""Convert one unescaped inline or display LaTeX expression."""

		def __init__(self, pattern: str, display: str) -> None:
			"""Initialize a processor for one delimiter family."""
			super().__init__(pattern)
			self._display = display

		def handleMatch(self, match: re.Match[str], data: str) -> tuple[Element | None, int | None, int | None]:
			"""Convert a formula, leaving malformed expressions as source text."""
			formula = match.group(1)
			if self._display == "inline" and _shouldSkipDollarFormula(formula, match.end(0), data):
				return None, None, None
			try:
				return (
					_convertFormulaToElement(formula, self._display),
					match.start(0),
					match.end(0),
				)
			except Exception:
				return None, None, None


	class _ParenthesizedLatexProcessor(InlineProcessor):
		"""Convert the common \\(...\\) inline LaTeX form to MathML."""

		def handleMatch(self, match: re.Match[str], data: str) -> tuple[Element | None, int | None, int | None]:
			"""Convert one matched formula, leaving malformed input unchanged."""
			try:
				return _convertFormulaToElement(match.group(1), "inline"), match.start(0), match.end(0)
			except Exception:
				return None, None, None


	class _SafeLatexExtension(Extension):
		"""Use l2m4m's display processor with a safe inline delimiter parser."""

		def extendMarkdown(self, md: Markdown) -> None:
			"""Register l2m4m block support and replace its permissive inline rule."""
			baseExtension = LaTeX2MathMLExtension()
			baseExtension.extendMarkdown(md)
			md.inlinePatterns.deregister("latex-inline")
			for name, pattern, display in (
				(
					"clipboard-latex-display-dollar",
					r"(?<!\\)\$\$(?!\$)([\s\S]+?)(?<!\\)\$\$(?!\$)",
					"block",
				),
				(
					"clipboard-latex-display-bracket",
					r"(?<!\\)\\\[([\s\S]+?)(?<!\\)\\\]",
					"block",
				),
				(
					"clipboard-latex-inline-dollar",
					r"(?<!\\)\$(?!\$)([^$\n]+?)(?<!\\)\$(?!\$)",
					"inline",
				),
			):
				md.inlinePatterns.register(_LatexInlineProcessor(pattern, display), name, 185)
			md.inlinePatterns.register(
				_ParenthesizedLatexProcessor(r"(?<!\\)\\\((.+?)(?<!\\)\\\)"),
				"clipboard-latex-inline-parenthesized",
				185,
			)


_MAX_RENDERED_TEXT_LENGTH = 4 * 1024 * 1024
_MATHML_NAMESPACE = "http://www.w3.org/1998/Math/MathML"
_MARKDOWN_FENCE_INFO_STRINGS = {"markdown", "md", "mdown", "mkdn", "gfm"}
_TEXT_FENCE_INFO_STRINGS = {"", "text", "plain", "plaintext"}
_MATH_DELIMITERS = (
	("$$", "$$", "block"),
	(r"\[", r"\]", "block"),
	(r"\(", r"\)", "inline"),
	("$", "$", "inline"),
)
_LITERAL_NUMERIC_CHARACTER_REFERENCE_PATTERN = re.compile(
	r"(?:(?:&(?:amp|AMP)|&#0*38|&#[xX]0*26);?)#(?:[xX][0-9A-Fa-f]+|\d+);?",
)
_NUMERIC_CHARACTER_REFERENCE_PATTERN = re.compile(r"&#(?:[xX][0-9A-Fa-f]+|\d+);")
_PRIVATE_USE_MARKER_START = 0xF0000
_PRIVATE_USE_MARKER_END = 0xFFFFD
_MATH_TEXT_ONLY_HTML_TAGS = {
	"iframe", "noembed", "noframes", "noscript", "script", "style", "textarea", "title", "xmp",
}
_MATH_EXCLUDED_HTML_TAGS = _MATH_TEXT_ONLY_HTML_TAGS | {"code", "kbd", "math", "pre", "samp"}
_MATHML_TAGS = {
	"abs", "and", "annotation", "annotation-xml", "apply", "approx", "arccos", "arccosh", "arccot",
	"arccoth", "arccsc", "arccsch", "arcsec", "arcsech", "arcsin", "arcsinh", "arctan", "arctanh", "arg",
	"bind", "bvar", "card", "cartesianproduct", "cbytes", "ceiling", "cerror", "ci", "cn", "codomain",
	"complexes", "compose", "condition", "conjugate", "cos", "cosh", "cot", "coth", "cs", "csc", "csch",
	"csymbol", "curl", "declare", "degree", "determinant", "diff", "divergence", "divide", "domain",
	"domainofapplication", "emptyset", "eq", "equivalent", "eulergamma", "exists", "exp", "exponentiale",
	"factorial", "factorof", "false", "floor", "forall", "gcd", "geq", "grad", "gt", "ident", "image",
	"imaginary", "imaginaryi", "in", "infinity", "int", "integers", "intersect", "lambda", "laplacian", "lcm",
	"leq", "limit", "list", "ln", "log", "logbase", "lowlimit", "lt", "maction", "math", "matrix", "matrixrow",
	"max", "mean", "median", "menclose", "merror", "mfenced", "mfrac", "mglyph", "mi", "min", "minus",
	"mlabeledtr", "mmultiscripts", "mn", "mo", "mode", "moment", "momentabout", "mover", "mpadded", "mphantom",
	"mprescripts", "mroot", "mrow", "ms", "mspace", "msqrt", "mstyle", "msub", "msubsup", "msup", "mtable",
	"mtd", "mtext", "mtr", "munder", "munderover", "naturalnumbers", "neq", "none", "not", "notanumber",
	"notin", "notprsubset", "notsubset", "or", "otherwise", "outerproduct", "partialdiff", "piece", "piecewise",
	"pi", "plus", "power", "primes", "product", "prsubset", "quotient", "rationals", "real", "reals", "rem",
	"reln", "root", "scalarproduct", "sdev", "sec", "sech", "selector", "semantics", "sep", "separation", "set", "setdiff", "sin",
	"sinh", "subset", "sum", "tan", "tanh", "tendsto", "times", "transpose", "true", "union", "uplimit",
	"variance", "vector", "vectorproduct", "xor",
}
_MATHML_ATTRIBUTES = {
	"accent", "accentunder", "actiontype", "align", "bevelled", "charalign", "close", "columnalign", "columnlines",
	"columnspacing", "columnspan", "crossout", "decimalpoint", "denomalign", "depth", "dir", "display", "displaystyle",
	"encoding", "fence", "form", "frame", "framespacing", "groupalign", "height", "indentalign", "indentalignfirst",
	"indentalignlast", "indentshift", "indentshiftfirst", "indentshiftlast", "indenttarget", "largeop", "length",
	"linethickness", "location", "longdivstyle", "lquote", "lspace", "mathbackground", "mathcolor", "mathsize",
	"mathvariant", "maxsize", "minlabelspacing", "minsize", "movablelimits", "notation", "numalign", "open", "overflow",
	"position", "rowalign", "rowlines", "rowspacing", "rowspan", "rquote", "rspace", "scriptlevel", "scriptminsize",
	"scriptsizemultiplier", "selection", "separator", "separators", "side", "stackalign", "stretchy", "subscriptshift",
	"superscriptshift", "symmetric", "voffset", "width", "xmlns", "xref",
}
_SAFE_TAGS = set(nh3.ALLOWED_TAGS) | _MATHML_TAGS | {"address", "font", "main", "search", "section", "tfoot"}
_SAFE_ATTRIBUTES = deepcopy(nh3.ALLOWED_ATTRIBUTES)
_SAFE_ATTRIBUTES.setdefault("*", set()).update(
	{"class", "dir", "id", "lang", "name", "role", "style", "title"},
)
_SAFE_ATTRIBUTES.setdefault("area", set()).update({"alt", "coords", "shape", "target"})
_SAFE_ATTRIBUTES.setdefault("font", set()).update({"color", "face", "size"})
for _tag in ("table", "thead", "tbody", "tfoot", "tr", "th", "td", "col", "colgroup"):
	_SAFE_ATTRIBUTES.setdefault(_tag, set()).update(
		{"bgcolor", "border", "cellpadding", "cellspacing", "height", "valign", "width"},
	)
for _tag in _MATHML_TAGS:
	_SAFE_ATTRIBUTES[_tag] = _MATHML_ATTRIBUTES
_SAFE_ATTRIBUTES.setdefault("annotation", set()).add("encoding")
_SAFE_ATTRIBUTES.setdefault("annotation-xml", set()).add("encoding")
_SAFE_STYLE_PROPERTIES = {
	"background-color", "border", "border-bottom", "border-bottom-color", "border-bottom-style",
	"border-bottom-width", "border-collapse", "border-color", "border-left", "border-left-color",
	"border-left-style", "border-left-width", "border-right", "border-right-color", "border-right-style",
	"border-right-width", "border-spacing", "border-style", "border-top", "border-top-color",
	"border-top-style", "border-top-width", "border-width", "bottom", "caption-side", "clear", "color",
	"direction", "display", "empty-cells", "float", "font", "font-family", "font-size", "font-style",
	"font-variant", "font-weight", "height", "letter-spacing", "line-height", "margin", "margin-bottom",
	"margin-left", "margin-right", "margin-top", "max-height", "max-width", "min-height", "min-width",
	"opacity", "outline", "outline-color", "outline-style", "outline-width", "padding", "padding-bottom",
	"padding-left", "padding-right", "padding-top", "right", "text-align", "text-decoration", "text-indent",
	"text-transform", "top", "vertical-align", "visibility", "white-space", "width", "word-break",
	"word-spacing", "overflow-wrap",
}
_DANGEROUS_STYLE_PATTERN = re.compile(
	r"(?i)(?:@import|expression\s*\(|(?:-moz-|-ms-)?binding\s*:|behavior\s*:|url\s*\(|(?:java|vb)script\s*:)",
)
_SAFE_DATA_IMAGE_PATTERN = re.compile(
	r"(?i)\Adata:image/(?:avif|bmp|gif|jpe?g|png|webp|x-icon)(?:;[^,]*)?,",
)
_SAFE_URL_SCHEMES = {"http", "https", "mailto"}
_OUTER_FENCE = re.compile(
	r"\A[ \t]*(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)\n(?P<body>.*)\n(?P=fence)[ \t]*\Z",
	re.DOTALL,
)
_CURRENCY_TEXT_PATTERN = re.compile(r"\A\d[\d,.]*\s+[A-Za-z]{2,}\b")
_HIDDEN_STYLE_PATTERN = re.compile(r"(?i)(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\b")
_VOID_HTML_TAGS = {"area", "br", "col", "hr", "img", "wbr"}


def _shouldSkipDollarFormula(formula: str, matchEnd: int, text: str) -> bool:
	"""Return whether a dollar-delimited match is more likely ordinary currency text."""
	if not formula or not formula[0].isdigit():
		return False
	if matchEnd < len(text) and text[matchEnd].isdigit():
		return True
	return bool(_CURRENCY_TEXT_PATTERN.match(formula))


def extractHtmlFragment(data: bytes | None) -> str | None:
	"""Extract the bounded HTML fragment from a Windows CF_HTML payload."""
	if not data:
		return None
	data = data[:_MAX_RENDERED_TEXT_LENGTH]
	headers = {
		key.lower(): value
		for key, value in re.findall(rb"(?im)^([a-z]+):([0-9]+)", data[:4096])
	}
	start = _offset(headers.get("startfragment"), len(data))
	end = _offset(headers.get("endfragment"), len(data))
	if start is not None and end is not None and 0 < start < end:
		return data[start:end].decode("utf-8", errors="replace")
	startMarker, endMarker = "<!--StartFragment-->", "<!--EndFragment-->"
	startBytes, endBytes = startMarker.encode(), endMarker.encode()
	lowerData = data.lower()
	start = lowerData.find(startBytes.lower())
	end = lowerData.find(endBytes.lower(), start + len(startBytes)) if start >= 0 else -1
	if start >= 0 and end > start:
		return data[start + len(startBytes) : end].decode("utf-8", errors="replace")
	text = data.decode("utf-8", errors="replace")
	match = re.search(
		r"<(?:html|body|head|div|p|span|h[1-6]|table|pre|section|article|ul|ol|blockquote|math)\b",
		text,
		re.IGNORECASE,
	)
	return text[match.start() :] if match else None


def _offset(value: bytes | None, length: int) -> int | None:
	"""Return a valid bounded byte offset from a CF_HTML header value."""
	if value is None:
		return None
	try:
		offset = int(value)
	except ValueError:
		return None
	return offset if 0 <= offset <= length else None


def _looksLikeMarkdownDocument(text: str) -> bool:
	"""Recognize plain-text fences that contain a Markdown document, not source code."""
	indicators = 0
	for line in text.splitlines():
		strippedLine = line.strip()
		if not strippedLine:
			continue
		if re.match(r"#{1,6}\s+\S", strippedLine):
			indicators += 1
		elif re.match(r"(?:[-*+]|\d+[.)])\s+\S", strippedLine):
			indicators += 1
		elif re.match(r">\s+\S", strippedLine):
			indicators += 1
		elif strippedLine.startswith("|") and strippedLine.endswith("|"):
			indicators += 1
		elif "**" in strippedLine or "__" in strippedLine:
			indicators += 1
		if indicators >= 2:
			return True
	return False


def _unwrapOuterMarkdownFence(text: str) -> str:
	"""Unwrap a copied document enclosed in one Markdown fence."""
	strippedText = text.strip()
	match = _OUTER_FENCE.match(strippedText)
	if not match:
		return text
	infoParts = match.group("info").strip().lower().split(None, 1)
	language = infoParts[0] if infoParts else ""
	body = match.group("body")
	if language in _MARKDOWN_FENCE_INFO_STRINGS:
		return body.strip("\n")
	if language in _TEXT_FENCE_INFO_STRINGS and _looksLikeMarkdownDocument(body):
		return body.strip("\n")
	return text


def _protectLiteralNumericCharacterReferences(formula: str) -> tuple[str, dict[str, str]]:
	"""Protect literal entity text while the LaTeX converter parses entities."""
	protectedReferences: dict[str, str] = {}
	markerCodePoint = _PRIVATE_USE_MARKER_START
	unescapedFormula = unescape(formula)

	def replaceReference(match: re.Match[str]) -> str:
		"""Replace one literal reference with an unused private marker."""
		nonlocal markerCodePoint
		while markerCodePoint <= _PRIVATE_USE_MARKER_END:
			marker = chr(markerCodePoint)
			markerCodePoint += 1
			if marker not in unescapedFormula and marker not in protectedReferences:
				protectedReferences[marker] = match.group(0)
				return marker
		raise ValueError("Too many literal numeric character references in formula")

	return _LITERAL_NUMERIC_CHARACTER_REFERENCE_PATTERN.sub(replaceReference, formula), protectedReferences


def _decodeMathMlNumericCharacterReferences(
	mathElement: ElementTree.Element,
	protectedReferences: dict[str, str],
) -> None:
	"""Restore literal entities after conversion to MathML."""
	referencePattern = re.compile(
		"|".join((*map(re.escape, protectedReferences), _NUMERIC_CHARACTER_REFERENCE_PATTERN.pattern)),
	)

	def decode(value: str) -> str:
		"""Decode generated numeric references and restore protected text."""
		return referencePattern.sub(
			lambda match: unescape(protectedReferences.get(match.group(0), match.group(0))),
			value,
		)

	for element in mathElement.iter():
		if element.text:
			element.text = decode(element.text)
		if element.tail:
			element.tail = decode(element.tail)
		for name, value in tuple(element.attrib.items()):
			element.set(name, decode(value))


def _convertFormulaToElement(formula: str, display: str) -> Element:
	"""Convert LaTeX while preserving entities intended as literal formula text."""
	protectedFormula, protectedReferences = _protectLiteralNumericCharacterReferences(formula)
	mathElement = converter.convert_to_element(unescape(protectedFormula), display=display)
	_decodeMathMlNumericCharacterReferences(mathElement, protectedReferences)
	return mathElement


def _convertFormulaToMathMl(formula: str, display: str) -> str:
	"""Convert LaTeX to serialized MathML for raw HTML processing."""
	return ElementTree.tostring(_convertFormulaToElement(formula, display), encoding="unicode")


def _isEscaped(text: str, position: int) -> bool:
	"""Return whether a delimiter is preceded by an odd number of backslashes."""
	backslashCount = 0
	position -= 1
	while position >= 0 and text[position] == "\\":
		backslashCount += 1
		position -= 1
	return backslashCount % 2 == 1


def _matchMathOpener(text: str, position: int) -> tuple[str, str, str] | None:
	"""Return the supported math delimiter at a position, if any."""
	for opener, closer, display in _MATH_DELIMITERS:
		if text.startswith(opener, position) and not _isEscaped(text, position):
			return opener, closer, display
	return None


def _findMathCloser(text: str, position: int, closer: str) -> tuple[int | None, int]:
	"""Find a matching unescaped closer without crossing another opener."""
	while position < len(text):
		if text.startswith(closer, position) and not _isEscaped(text, position):
			return position, position + len(closer)
		if _matchMathOpener(text, position) is not None:
			return None, position
		position += 1
	return None, position


# ponytail: Conversion stays synchronous; add a formula budget or worker if large formula-heavy clips stall NVDA.
def _convertMathText(text: str, convertFormula: Callable[[str, str], str]) -> str:
	"""Convert supported LaTeX delimiters in one forward pass."""
	output: list[str] = []
	unchangedStart = 0
	position = 0
	while position < len(text):
		delimiter = _matchMathOpener(text, position)
		if delimiter is None:
			position += 1
			continue
		opener, closer, display = delimiter
		formulaStart = position + len(opener)
		formulaEnd, resumePosition = _findMathCloser(text, formulaStart, closer)
		if formulaEnd is None:
			if resumePosition >= len(text):
				break
			position = resumePosition
			continue
		matchEnd = formulaEnd + len(closer)
		formula = text[formulaStart:formulaEnd]
		if not formula:
			position = matchEnd
			continue
		# ponytail: A dollar pair around a number is usually currency, not math.
		if opener == "$" and _shouldSkipDollarFormula(formula, matchEnd, text):
			position = matchEnd
			continue
		try:
			mathMl = convertFormula(formula, display)
		except Exception:
			log.debug("Unable to convert clipboard LaTeX formula", exc_info=True)
			position = matchEnd
			continue
		output.extend((text[unchangedStart:position], mathMl))
		unchangedStart = matchEnd
		position = matchEnd
	output.append(text[unchangedStart:])
	return "".join(output)


class _ReadableHtmlParser(HTMLParser):
	"""Collect visible text and accessible alternatives from sanitized HTML."""

	def __init__(self) -> None:
		"""Initialize the parser with character references decoded."""
		super().__init__(convert_charrefs=True)
		self.parts: list[str] = []
		self._hiddenTags: list[str] = []
		self._hasVisibleImage = False

	def handle_data(self, data: str) -> None:
		"""Collect visible text nodes."""
		if not self._hiddenTags:
			self.parts.append(data)

	def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		"""Collect alternative labels that replace otherwise empty elements."""
		tag = tag.lower()
		isHidden = bool(self._hiddenTags) or self._hasHiddenAttribute(attrs)
		if not isHidden:
			self._collectAlternativeLabels(attrs)
			if tag == "img" and self._hasSafeImageSource(attrs):
				self._hasVisibleImage = True
		if isHidden and tag not in _VOID_HTML_TAGS:
			self._hiddenTags.append(tag)

	def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		"""Collect accessible alternatives from visible self-closing elements."""
		if not self._hiddenTags and not self._hasHiddenAttribute(attrs):
			self._collectAlternativeLabels(attrs)
			if tag.lower() == "img" and self._hasSafeImageSource(attrs):
				self._hasVisibleImage = True

	def handle_endtag(self, tag: str) -> None:
		"""Close the matching hidden element and any malformed descendants."""
		tag = tag.lower()
		for position in range(len(self._hiddenTags) - 1, -1, -1):
			if self._hiddenTags[position] == tag:
				del self._hiddenTags[position:]
				break

	def _hasHiddenAttribute(self, attrs: list[tuple[str, str | None]]) -> bool:
		"""Return whether attributes hide an element from visual or assistive output."""
		for name, value in attrs:
			if not value:
				continue
			name = name.lower()
			if name == "aria-hidden" and value.strip().lower() == "true":
				return True
			if name == "style" and _HIDDEN_STYLE_PATTERN.search(value):
				return True
		return False

	def _collectAlternativeLabels(self, attrs: list[tuple[str, str | None]]) -> None:
		"""Collect alternative labels exposed by otherwise empty elements."""
		for name, value in attrs:
			if name.lower() in {"alt", "aria-label"} and value:
				self.parts.append(value)

	def _hasSafeImageSource(self, attrs: list[tuple[str, str | None]]) -> bool:
		"""Return whether attributes contain a retained embedded raster image source."""
		return any(
			name.lower() == "src"
			and value
			and _SAFE_DATA_IMAGE_PATTERN.match(value.strip())
			for name, value in attrs
		)


def _hasReadableHtml(html: str) -> bool:
	"""Return whether sanitized HTML contains visible or accessible text."""
	parser = _ReadableHtmlParser()
	try:
		parser.feed(html)
		parser.close()
	except Exception:
		return False
	return bool("".join(parser.parts).strip()) or parser._hasVisibleImage


class _RawHtmlMathParser(HTMLParser):
	"""Convert formulas in HTML text while leaving code and MathML untouched."""

	def __init__(self) -> None:
		"""Initialize the parser without decoding character references."""
		super().__init__(convert_charrefs=False)
		self._excludedTags: list[str] = []
		self._htmlParts: list[str] = []
		self._sourceLines: list[str] = []
		self._textParts: list[str] = []

	def convert(self, htmlText: str) -> str:
		"""Return HTML with formulas converted in eligible text nodes."""
		self._sourceLines = htmlText.split("\n")
		self.feed(htmlText)
		self.close()
		self._flushText()
		return "".join(self._htmlParts)

	def _flushText(self) -> None:
		"""Append buffered text, converting it outside excluded elements."""
		if not self._textParts:
			return
		text = "".join(self._textParts)
		self._textParts.clear()
		self._htmlParts.append(text if self._excludedTags else _convertMathText(text, _convertFormulaToMathMl))

	def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		"""Preserve a start tag and track excluded content."""
		self._flushText()
		self._htmlParts.append(self.get_starttag_text() or f"<{tag}>")
		if tag.lower() in _MATH_EXCLUDED_HTML_TAGS:
			self._excludedTags.append(tag.lower())

	def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		"""Preserve a self-closing tag."""
		self._flushText()
		self._htmlParts.append(self.get_starttag_text() or f"<{tag} />")

	def handle_endtag(self, tag: str) -> None:
		"""Preserve an end tag and update excluded-content state."""
		self._flushText()
		self._htmlParts.append(f"</{tag}>")
		tag = tag.lower()
		if tag in self._excludedTags:
			tagPosition = len(self._excludedTags) - 1 - self._excludedTags[::-1].index(tag)
			del self._excludedTags[tagPosition:]

	def handle_data(self, data: str) -> None:
		"""Buffer text for formula conversion."""
		self._textParts.append(data)

	def _appendCharacterReference(self, reference: str) -> None:
		"""Preserve one source character reference and its optional semicolon."""
		lineNumber, offset = self.getpos()
		if self._sourceLines[lineNumber - 1].startswith(";", offset + len(reference)):
			reference += ";"
		self._textParts.append(reference)

	def handle_entityref(self, name: str) -> None:
		"""Preserve a named character reference."""
		self._appendCharacterReference(f"&{name}")

	def handle_charref(self, name: str) -> None:
		"""Preserve a numeric character reference."""
		self._appendCharacterReference(f"&#{name}")

	def handle_comment(self, data: str) -> None:
		"""Preserve a comment for subsequent sanitization."""
		self._flushText()
		self._htmlParts.append(f"<!--{data}-->")


def _convertMathInRawHtml(htmlText: str) -> str:
	"""Convert LaTeX in raw HTML, preserving malformed markup for sanitization."""
	if not any(delimiter[0] in htmlText for delimiter in _MATH_DELIMITERS):
		return htmlText
	try:
		return _RawHtmlMathParser().convert(htmlText)
	except Exception:
		log.debug("Unable to convert clipboard formulas in HTML", exc_info=True)
		return htmlText


class _RawHtmlMathPostprocessor(Postprocessor):
	"""Convert formulas in Markdown's raw HTML stash."""

	def __init__(self, md: Markdown) -> None:
		"""Initialize the postprocessor for one Markdown instance."""
		super().__init__(md)
		self._markdown = md

	def run(self, text: str) -> str:
		"""Convert formulas in raw HTML before Markdown restores it."""
		for position, htmlBlock in enumerate(self._markdown.htmlStash.rawHtmlBlocks):
			self._markdown.htmlStash.rawHtmlBlocks[position] = _convertMathInRawHtml(htmlBlock)
		return text


class _RawHtmlMathExtension(Extension):
	"""Add the raw HTML math postprocessor to Python-Markdown."""

	def extendMarkdown(self, md: Markdown) -> None:
		"""Register raw HTML formula conversion."""
		for tag in _MATH_TEXT_ONLY_HTML_TAGS:
			if tag not in md.block_level_elements:
				md.block_level_elements.append(tag)
		md.postprocessors.register(
			_RawHtmlMathPostprocessor(md),
			"clipboardRawHtmlMath",
			35,
		)


def _getMarkdownExtensions() -> list[str | Extension]:
	"""Build Markdown extensions, adding the optional LaTeX pipeline."""
	extensions: list[str | Extension] = ["extra", "nl2br"]
	if converter is None:
		return extensions

	extensions.extend(
		(
			_SafeLatexExtension(),
			_RawHtmlMathExtension(),
		),
	)
	return extensions


def _safeAttribute(tag: str, attribute: str, value: str) -> str | None:
	"""Keep presentation attributes while rejecting executable or active URLs."""
	tag = tag.lower()
	attribute = attribute.lower()
	if attribute == "style":
		return None if _DANGEROUS_STYLE_PATTERN.search(value) else value
	if attribute == "src":
		if tag != "img":
			return None
		strippedValue = value.strip()
		return value if _SAFE_DATA_IMAGE_PATTERN.match(strippedValue) else None
	if attribute == "href":
		strippedValue = value.strip()
		if not strippedValue:
			return value
		if strippedValue.replace("\\", "/").startswith("//"):
			return None
		try:
			scheme = urlsplit(strippedValue).scheme.lower()
		except ValueError:
			return None
		return value if not scheme or scheme in _SAFE_URL_SCHEMES else None
	if tag in _MATHML_TAGS and attribute == "xmlns":
		return value if value == _MATHML_NAMESPACE else None
	return value


def sanitizeHtml(html: str) -> str:
	"""Sanitize HTML while preserving passive structure, presentation, and accessibility metadata."""
	return nh3.clean(
		html[:_MAX_RENDERED_TEXT_LENGTH],
		tags=_SAFE_TAGS,
		clean_content_tags={"script", "style"},
		attributes=_SAFE_ATTRIBUTES,
		attribute_filter=_safeAttribute,
		strip_comments=True,
		generic_attribute_prefixes={"aria-", "data-"},
		filter_style_properties=_SAFE_STYLE_PROPERTIES,
		url_schemes=_SAFE_URL_SCHEMES | {"data"},
	)


def renderText(text: str) -> str:
	"""Render plain clipboard text as Markdown, with optional LaTeX MathML."""
	text = _unwrapOuterMarkdownFence(text[:_MAX_RENDERED_TEXT_LENGTH])
	try:
		rendered = sanitizeHtml(markdown(text, extensions=_getMarkdownExtensions()))
	except Exception:
		try:
			rendered = sanitizeHtml(markdown(text, extensions=["extra", "nl2br"]))
		except Exception:
			rendered = ""
	return rendered or f"<pre>{escape(text)}</pre>"


def renderHtml(html: str) -> str:
	"""Render and sanitize clipboard HTML, converting LaTeX in visible text."""
	html = html[:_MAX_RENDERED_TEXT_LENGTH]
	if converter is not None:
		html = _convertMathInRawHtml(html)
	return sanitizeHtml(html)


def renderSnapshot(snapshot: ClipboardSnapshot, fallbackText: str = "") -> str:
	"""Return safe HTML for one clipboard snapshot."""
	if snapshot.html:
		fragment = extractHtmlFragment(snapshot.html)
		if fragment:
			renderedHtml = renderHtml(fragment)
			if _hasReadableHtml(renderedHtml):
				return renderedHtml
	if snapshot.text:
		return renderText(snapshot.text)
	if snapshot.files:
		return f"<pre>{escape(chr(10).join(snapshot.files))}</pre>"
	return f"<p>{escape(fallbackText)}</p>"


def showSnapshot(
	snapshot: ClipboardSnapshot,
	title: str,
	fallbackText: str = "",
	*,
	showAsPlainText: bool = False,
) -> None:
	"""Open one clipboard snapshot in a rendered or plain browseable message."""
	import ui

	if showAsPlainText:
		message = (
			snapshot.text
			or chr(10).join(snapshot.files)
			or (extractHtmlFragment(snapshot.html) if snapshot.html else "")
			or fallbackText
		)
		ui.browseableMessage(message, title=title, closeButton=True)
		return

	html = renderSnapshot(snapshot, fallbackText)
	ui.browseableMessage(
		html,
		title=title,
		isHtml=True,
		closeButton=True,
		# renderSnapshot already applies the custom sanitizer; avoid parsing the full HTML again.
		sanitizeHtmlFunc=lambda message: message,
	)
