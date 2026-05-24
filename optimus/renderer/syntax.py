# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Pygments-based Python syntax highlighting for the renderer.

Two responsibilities:

  * **Source-snippet highlighting** for findings + actions. Walks the
    finding / action tree and adds a ``content_html`` field to every
    ``{lineno, content}`` row, carrying Pygments-tokenised span markup.
    The template's CSS (GitHub Light palette via classprefix ``tok-``)
    styles the spans without an external stylesheet.

  * **Diff-block highlighting** for the AI fix card. Walks the rendered
    Markdown output, finds ```diff fenced blocks (with or without an
    explicit language hint), and wraps each ``+`` / ``-`` / ``@@`` line
    in a classed span so the template's CSS can colour the diff.

Pygments is loaded lazily inside :func:`_ensure_pygments` — paths that
never highlight code (DocType save callbacks, janitor sweeps, the bulk
of the regenerate path) don't pay the ~30-50ms import cost at app load.
Module-level slots are populated on first use and cached for the worker
process's lifetime.

The :func:`_highlight_python_block_cached` LRU has ``maxsize=512`` — large
enough that overlapping snippets across N findings tokenise the same
underlying source exactly once, small enough that the cache fits in the
worker's heap on extreme-session sizes.

Failures degrade silently: when Pygments is unavailable or tokenisation
raises (rare — malformed source), the row's ``content_html`` is set to
``None`` and the template falls back to the plain-text ``content``. The
report stays readable; only the colour goes away.
"""

from __future__ import annotations

import functools
import re

# Pygments is loaded lazily inside ``_ensure_pygments`` so paths that
# never highlight code (DocType save callbacks, janitor sweeps, light
# API endpoints) don't pay the ~30-50ms import cost at module load.
# Module-level slots are populated on first use and cached.
_PY_LEXER = None
_PY_HTML_FMT = None
_pyg_highlight = None


def _ensure_pygments() -> bool:
	"""Lazy-init Pygments lexer + formatter. Returns ``False`` if
	Pygments isn't available (graceful degradation - the caller writes
	``content_html = None`` and the template's plain-text fallback
	kicks in)."""
	global _PY_LEXER, _PY_HTML_FMT, _pyg_highlight
	if _pyg_highlight is not None:
		return True
	try:
		from pygments import highlight as _pyg_highlight_fn
		from pygments.formatters import HtmlFormatter
		from pygments.lexers import PythonLexer
		_PY_LEXER = PythonLexer(stripnl=False, ensurenl=False)
		_PY_HTML_FMT = HtmlFormatter(nowrap=True, classprefix="tok-")
		_pyg_highlight = _pyg_highlight_fn
		return True
	except Exception:
		return False


@functools.lru_cache(maxsize=512)
def _highlight_python_block_cached(joined: str) -> str:
	"""Pygments tokenise + format a multi-line Python source block.
	Cached across all calls within the same worker process so N
	overlapping snippets from N findings tokenise the underlying source
	exactly once. ``maxsize=512`` covers reasonable session sizes; LRU
	eviction handles the long-tail.
	"""
	return _pyg_highlight(joined, _PY_LEXER, _PY_HTML_FMT).rstrip("\n")


def _highlight_python_snippet(lines):
	"""Mutate each ``{"lineno", "content"}`` dict in ``lines`` to add a
	``content_html`` field carrying Pygments-highlighted span markup
	(GitHub Light palette via CSS in the template).

	The lines are joined into one source block before highlighting so
	multi-line strings, decorators, and other multi-line constructs
	keep correct tokenisation; the resulting HTML is then split per
	``\\n`` and each chunk assigned back to its source line. Idempotent
	on lines that already carry ``content_html``. Falls back to
	``content_html = None`` (template uses plain ``content``) on any
	Pygments failure or when Pygments isn't available.
	"""
	if not lines:
		return
	if all(isinstance(l, dict) and l.get("content_html") is not None for l in lines):
		return
	if not _ensure_pygments():
		for l in lines:
			if isinstance(l, dict):
				l["content_html"] = None
		return
	try:
		src = "\n".join((l.get("content") or "") for l in lines if isinstance(l, dict))
		out = _highlight_python_block_cached(src)
		chunks = out.split("\n")
		if len(chunks) != len(lines):
			for l in lines:
				if isinstance(l, dict):
					l["content_html"] = None
			return
		for l, chunk in zip(lines, chunks, strict=True):
			if isinstance(l, dict):
				l["content_html"] = chunk
	except Exception:
		for l in lines:
			if isinstance(l, dict):
				l["content_html"] = None


def _highlight_all_snippets(actions, all_findings):
	"""Walk findings + actions and apply VSCode Dark+ syntax highlighting
	to every ``source_snippet`` list reachable from the data shapes the
	template + line-prof builder iterate. Mutates in place.
	"""
	for f in all_findings or []:
		if not isinstance(f, dict):
			continue
		td = f.get("technical_detail")
		if not isinstance(td, dict):
			continue
		cs = td.get("callsite")
		if isinstance(cs, dict):
			_highlight_python_snippet(cs.get("source_snippet") or [])
		for chain_key in ("drilldown_chain", "frame_chain", "call_chain"):
			chain = td.get(chain_key)
			if isinstance(chain, list):
				for step in chain:
					if isinstance(step, dict):
						_highlight_python_snippet(step.get("source_snippet") or [])
	for a in actions or []:
		if not isinstance(a, dict):
			continue
		ec = a.get("entry_callsite")
		if isinstance(ec, dict):
			_highlight_python_snippet(ec.get("source_snippet") or [])


# ---------------------------------------------------------------------------
# Diff-block highlighting — used by the AI fix card
# ---------------------------------------------------------------------------

# <pre> block, optionally wrapping a <code>...</code> (with or without a class).
_PRE_BLOCK_RE = re.compile(
	r'<pre[^>]*>(?:\s*<code([^>]*)>)?(.*?)(?:</code>\s*)?</pre>', re.S
)


def _looks_like_diff(code_attrs: str, lines: list[str]) -> bool:
	if "diff" in (code_attrs or ""):
		return True
	if any(ln.startswith("@@") for ln in lines):
		return True
	has_add = any(ln.startswith("+") for ln in lines)
	has_del = any(ln.startswith("-") for ln in lines)
	return has_add and has_del


def _diff_line_class(line: str) -> str | None:
	if line.startswith("@@") or line.startswith("+++") or line.startswith("---"):
		return "dh-meta"
	if line.startswith("+"):
		return "dh-add"
	if line.startswith("-"):
		return "dh-del"
	return None


def _highlight_diff_html(html: str) -> str:
	"""Wrap +/-/@@ lines inside diff-looking ``<pre>`` blocks in classed
	spans. Pure string transform over already-sanitized HTML — only adds
	``<span class="dh-…">`` wrappers around existing escaped text."""

	def _wrap(match: re.Match) -> str:
		code_attrs = match.group(1) or ""
		inner = match.group(2) or ""
		lines = inner.split("\n")
		# A trailing "" from the markdown renderer's final newline — drop it
		# so we don't emit an empty trailing block-span.
		if lines and lines[-1] == "":
			lines = lines[:-1]
		if not lines or not _looks_like_diff(code_attrs, lines):
			return match.group(0)
		out: list[str] = []
		for ln in lines:
			cls = _diff_line_class(ln)
			label = f"dh-line {cls}" if cls else "dh-line dh-ctx"
			out.append(f'<span class="{label}">{ln or "&#8203;"}</span>')
		code_open = f"<code{code_attrs}>" if code_attrs else "<code>"
		return f'<pre class="dh">{code_open}' + "".join(out) + "</code></pre>"

	return _PRE_BLOCK_RE.sub(_wrap, html)
