"""Pure-function coverage for cansniff.ui.responsive's continuous density
model -- interpolated_density and its supporting anchors/blend helpers.
Qt-free and fast: these are plain dataclass computations, independently
testable from any widget. See tests/test_responsive_layout.py for the
Qt-integration side (MainWindow actually applying this on resize).
"""

from __future__ import annotations

import unittest

from cansniff.ui.responsive import (
    DENSITY_COMPACT, DENSITY_NORMAL, DENSITY_SPACIOUS, DENSITY_ULTRA,
    interpolated_density,
)

_FIELDS = (
    "margin", "spacing", "tight_spacing", "row_height", "button_pad_v",
    "button_pad_h", "input_pad_v", "input_pad_h", "header_pad_v", "header_pad_h",
    "cell_pad_v", "cell_pad_h", "nav_width", "nav_button_height",
    "chip_max_width",
)


class InterpolatedDensityTests(unittest.TestCase):
    def test_below_the_smallest_anchor_clamps_to_ultra(self):
        applied = interpolated_density(200, 200)
        for field in _FIELDS:
            self.assertEqual(getattr(applied, field), getattr(DENSITY_ULTRA, field))
        self.assertEqual(applied.font_delta, DENSITY_ULTRA.font_delta)

    def test_above_the_largest_anchor_clamps_to_spacious_not_beyond(self):
        applied = interpolated_density(5000, 3000)
        for field in _FIELDS:
            self.assertEqual(getattr(applied, field), getattr(DENSITY_SPACIOUS, field))
        # No fifth tier: an even larger window is not given even bigger
        # controls -- see the module docstring on DENSITY_SPACIOUS.
        applied_bigger = interpolated_density(8000, 6000)
        for field in _FIELDS:
            self.assertEqual(getattr(applied, field), getattr(applied_bigger, field))

    def test_width_increases_monotonically_non_decreasing(self):
        widths = list(range(400, 2200, 37))
        previous = None
        for width in widths:
            applied = interpolated_density(width, 2000)  # height never the constraint here
            if previous is not None:
                for field in _FIELDS:
                    self.assertGreaterEqual(
                        getattr(applied, field), getattr(previous, field),
                        "{} decreased from width {} to {}".format(
                            field, width - 37, width))
            previous = applied

    def test_height_increases_monotonically_non_decreasing(self):
        heights = list(range(250, 1200, 31))
        previous = None
        for height in heights:
            applied = interpolated_density(2000, height)  # width never the constraint
            if previous is not None:
                for field in _FIELDS:
                    self.assertGreaterEqual(
                        getattr(applied, field), getattr(previous, field),
                        "{} decreased from height {} to {}".format(
                            field, height - 31, height))
            previous = applied

    def test_font_delta_stays_within_the_narrow_documented_bound(self):
        """Fonts must change considerably less than spacing/controls --
        never below ULTRA's own -0.5pt floor, never above 0.0."""
        for width in range(300, 3000, 97):
            for height in range(250, 1200, 83):
                delta = interpolated_density(width, height).font_delta
                self.assertGreaterEqual(delta, DENSITY_ULTRA.font_delta)
                self.assertLessEqual(delta, 0.0)

    def test_the_more_constrained_axis_governs_every_field(self):
        """A wide-but-short window (plenty of width, very little height)
        must not get spacious sizing just because it is wide -- and a
        tall-but-narrow one must not get it just because it is tall."""
        wide_short = interpolated_density(2200, 250)
        for field in _FIELDS:
            self.assertLessEqual(getattr(wide_short, field), getattr(DENSITY_ULTRA, field) + 1)
        tall_narrow = interpolated_density(250, 2200)
        for field in _FIELDS:
            self.assertLessEqual(getattr(tall_narrow, field), getattr(DENSITY_ULTRA, field) + 1)

    def test_large_desktop_window_is_not_stuck_at_the_same_size_as_a_bare_minimum_one(self):
        """The concrete regression this whole model exists to fix: a
        window barely past the old NORMAL threshold and a genuinely large
        one must not render byte-identical controls."""
        just_normal = interpolated_density(1250, 760)
        clearly_large = interpolated_density(2200, 1200)
        self.assertGreater(clearly_large.margin, just_normal.margin)
        self.assertGreater(clearly_large.nav_width, just_normal.nav_width)
        self.assertGreater(clearly_large.row_height, just_normal.row_height)
        # ...but typography barely moves -- a big monitor is not giant text.
        self.assertLessEqual(clearly_large.font_delta - just_normal.font_delta, 0.5)

    def test_repeated_calls_with_the_same_size_are_identical_no_cumulative_drift(self):
        """Calling this ten times in a row at the same size (as a resize
        settling, or several equal-sized ticks of a drag, would) must never
        compound -- every call derives from the raw width/height, never
        from a previous result."""
        first = interpolated_density(1300, 800)
        for _ in range(10):
            self.assertEqual(interpolated_density(1300, 800), first)

    def test_quantization_makes_nearby_sizes_compare_equal(self):
        """A handful of adjacent single-pixel sizes (one drag frame's worth
        of resizeEvent calls) must collapse onto the same Density object --
        this is what lets Theme.set_density's == check skip the expensive
        restyle sweep on nearly every call. See responsive.py's
        _DENSITY_QUANTUM."""
        base = interpolated_density(1300, 800)
        for offset in range(1, 6):
            self.assertEqual(interpolated_density(1300 + offset, 800), base)

    def test_named_anchors_are_recovered_at_their_own_exact_point(self):
        # Below/above the outer anchors, values are exactly the named
        # constant (quantization cannot shift a clamp-region result).
        self.assertEqual(interpolated_density(1, 1), DENSITY_ULTRA)
        huge = interpolated_density(100000, 100000)
        for field in _FIELDS:
            self.assertEqual(getattr(huge, field), getattr(DENSITY_SPACIOUS, field))

    def test_compact_and_normal_anchor_values_are_still_reachable(self):
        # Near (not necessarily bit-exact past quantization -- see
        # test_responsive_layout.py's _assert_density_matches) the COMPACT
        # and NORMAL anchor widths, values should be close to those anchors.
        near_compact = interpolated_density(1180, 2000)
        for field in _FIELDS:
            self.assertLessEqual(
                abs(getattr(near_compact, field) - getattr(DENSITY_COMPACT, field)), 3)
        near_normal = interpolated_density(1400, 2000)
        for field in _FIELDS:
            self.assertLessEqual(
                abs(getattr(near_normal, field) - getattr(DENSITY_NORMAL, field)), 3)


if __name__ == "__main__":
    unittest.main()
