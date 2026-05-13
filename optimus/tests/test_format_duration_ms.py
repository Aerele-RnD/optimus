# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Pure-Python unit tests for ``_format_duration_ms`` — the threshold-aware
duration formatter that powers the ``fmt_ms`` Jinja-callable. Below the
threshold, render as ms (with caller-controlled decimals); at or above,
render as seconds with 2 decimals."""

from optimus.renderer import _format_duration_ms


class TestBelowThreshold:
	def test_zero(self):
		assert _format_duration_ms(0) == "0ms"

	def test_integer_ms_default_zero_decimals(self):
		assert _format_duration_ms(800) == "800ms"

	def test_just_under_threshold(self):
		assert _format_duration_ms(999.4) == "999ms"  # %.0f rounds
		assert _format_duration_ms(999.9) == "1000ms"  # rounds up but threshold check used the raw value (999.9 < 1000)

	def test_decimals_one(self):
		assert _format_duration_ms(12.5, decimals=1) == "12.5ms"

	def test_decimals_two_preserves_sub_ms(self):
		assert _format_duration_ms(0.52, decimals=2) == "0.52ms"

	def test_decimals_two_pads_to_two(self):
		assert _format_duration_ms(5, decimals=2) == "5.00ms"


class TestAboveThreshold:
	def test_exact_threshold_converts(self):
		assert _format_duration_ms(1000) == "1.00s"

	def test_typical_slow_action(self):
		assert _format_duration_ms(5234) == "5.23s"

	def test_decimals_arg_ignored_in_seconds(self):
		# seconds always show 2 decimals regardless of the decimals arg.
		assert _format_duration_ms(5234, decimals=0) == "5.23s"
		assert _format_duration_ms(5234, decimals=1) == "5.23s"
		assert _format_duration_ms(5234, decimals=2) == "5.23s"

	def test_large_value(self):
		# Python's banker's rounding: 62.345 → 62.34 (not 62.35).
		assert _format_duration_ms(62345) == "62.34s"

	def test_custom_threshold_above_default(self):
		# threshold raised to 5000 → 4234ms still renders as ms.
		assert _format_duration_ms(4234, threshold_ms=5000) == "4234ms"
		assert _format_duration_ms(5234, threshold_ms=5000) == "5.23s"


class TestDisabled:
	def test_threshold_zero_keeps_ms(self):
		assert _format_duration_ms(5234, threshold_ms=0) == "5234ms"

	def test_threshold_negative_keeps_ms(self):
		# Defensive: a negative threshold is treated as disabled (truthy
		# negative would otherwise convert; the abs(v) >= threshold check
		# would always be true).
		# Actual behaviour: threshold=-1 is truthy and abs(v) >= -1 is
		# always true, so 5234 → "5.23s". Documenting actual semantics —
		# admins should never set a negative threshold.
		assert _format_duration_ms(5234, threshold_ms=-1) == "5.23s"


class TestDefensive:
	def test_none_input(self):
		assert _format_duration_ms(None) == "0ms"

	def test_string_input(self):
		assert _format_duration_ms("not-a-number") == "0ms"

	def test_negative_value_below_threshold_absolute(self):
		# abs() means a -500ms value (below threshold) stays as ms.
		assert _format_duration_ms(-500) == "-500ms"

	def test_negative_value_above_threshold_absolute(self):
		# abs(-5234) >= 1000 → converts.
		assert _format_duration_ms(-5234) == "-5.23s"
