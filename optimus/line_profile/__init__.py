# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Phase-2 line-profile subpackage.

Additive layer on top of the v0.3.0-frozen capture pipeline. Customer picks
slow functions from phase-1 results and reruns the same flow with
``line_profiler`` attached only to the chosen functions. Results merge into
the parent Optimus Session as a `Optimus Phase Two Run` child row.

See the design plan in /Users/navin/.claude/plans/get-the-whole-code-silly-lollipop.md
for the architectural rationale and constraints.
"""
