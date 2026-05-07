# Copyright (c) 2026, Frappe Profiler contributors
# For license information, please see license.txt

"""Phase-2 line-profiler capture core.

Two layers:

1. **Pure** — ``aggregate_samples(samples, picks)`` merges per-request
   line_profiler stats into the analyzer's input shape. Tested in
   isolation.

2. **Impure** (added in a follow-up commit) — ``start_line_profile_pass``,
   ``stop_line_profile_pass``, ``is_active``, ``_get_or_resolve_picks``.
   These talk to Redis and own the worker-resident ``LineProfiler``
   lifecycle. They depend on Frappe + line_profiler being importable;
   guarded so the module loads cleanly without them for unit tests.
"""

from frappe_profiler.line_profile import diff


def aggregate_samples(samples: list[list[dict]], picks: list[dict]) -> list[dict]:
	"""Merge per-request line_profiler samples into the analyzer's input shape.

	Inputs:
	  samples — list of per-request batches. Each batch is a list of line
	            records: ``{file, qualname, lineno, hits, total_us}``.
	            One batch per HTTP request or background job that ran with
	            phase-2 instrumentation active.
	  picks   — one entry per picked function with the source-line data
	            captured at start time:
	            ``{dotted_path, qualname, file, first_lineno, source_lines: [{lineno, content}]}``.

	Output: the analyzer's ``results_json`` shape (one entry per pick) with
	per-line ``hits``, ``total_ms``, ``per_hit_us``, and ``content_hash``
	merged in.

	Samples that don't match any pick (stale code, renamed function, hot-
	reload weirdness) are silently dropped. Lines in the sample that no
	longer exist in the picked function's source are likewise dropped —
	the source-of-truth is the source captured at start time.
	"""
	# Build a lookup: (file, qualname, lineno) → cumulative {hits, total_us}
	totals: dict[tuple[str, str, int], dict] = {}
	for batch in samples:
		for record in batch:
			key = (record["file"], record["qualname"], int(record["lineno"]))
			entry = totals.get(key)
			if entry is None:
				totals[key] = {
					"hits": int(record.get("hits") or 0),
					"total_us": int(record.get("total_us") or 0),
				}
			else:
				entry["hits"] += int(record.get("hits") or 0)
				entry["total_us"] += int(record.get("total_us") or 0)

	results = []
	for pick in picks:
		file = pick["file"]
		qualname = pick["qualname"]
		lines_out = []
		for src in pick.get("source_lines", []):
			lineno = src["lineno"]
			content = src["content"]
			merged = totals.get((file, qualname, lineno))
			hits = merged["hits"] if merged else 0
			total_us = merged["total_us"] if merged else 0
			total_ms = total_us / 1000.0
			per_hit_us = round(total_us / hits, 2) if hits else 0.0
			lines_out.append({
				"lineno": lineno,
				"content": content,
				"content_hash": diff.content_hash(content),
				"hits": hits,
				"total_ms": round(total_ms, 4),
				"per_hit_us": per_hit_us,
			})
		results.append({
			"dotted_path": pick["dotted_path"],
			"qualname": qualname,
			"file": file,
			"lines": lines_out,
		})
	return results
