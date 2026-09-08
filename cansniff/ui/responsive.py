"""Central, small responsive/density model shared across the UI.

This is deliberately not a framework: it is a handful of thresholds, a
tri-state classification per axis, and three named bundles of spacing/size
tokens. Individual widgets decide *what* to do in response (reflow, collapse,
shrink a font) -- this module only decides *which regime* the window is
currently in and centralizes the magic numbers behind that decision, so
every file that cares reads the same threshold instead of scattering its own.

Width and height are classified independently (see ``ResponsiveState``): an
on-screen keyboard shrinking height should not necessarily trigger the same
response as a narrow-but-tall window, and vice versa. ``ResponsiveState.
overall`` -- the more constrained of the two -- is what drives shared density
(padding/row height/fonts); individual call sites that only care about one
axis (the sidebar auto-collapse cares about width alone; bit-activity
collapse cares about height alone) read ``width_class``/``height_class``
directly instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .theme import (
    HEADER_HEIGHT, RADIUS_MD, ROW_HEIGHT, ROW_HEIGHT_COMPACT, SPACE_LG,
    SPACE_MD, SPACE_SM, SPACE_XS,
)


class SizeClass(IntEnum):
    """Ordered so ``max()``/comparison picks the more constrained class."""

    NORMAL = 0
    COMPACT = 1
    ULTRA = 2


#: Content-area width thresholds (the splitter + nav rail, i.e.
#: MainWindow.width() -- not the physical screen). Chosen from what the UI
#: actually needs, not a named device: below WIDTH_COMPACT the top bar's
#: secondary actions start wrapping and the two-panel splitter's floors
#: shrink; below WIDTH_ULTRA the packet-list sidebar auto-collapses because
#: 220 (its own floor) + 280 (InterpretView's) + 76 (nav rail) no longer
#: leaves either pane a usable width.
WIDTH_COMPACT = 1180
WIDTH_ULTRA = 900

#: Content-area height thresholds (central widget height -- excludes the
#: title bar/window decoration Qt itself accounts for). Below HEIGHT_COMPACT,
#: vertical margins/spacing shrink and the bit-activity matrix's own toggle
#: starts being backed off; below HEIGHT_ULTRA (representative of an
#: on-screen keyboard eating a large fraction of a small display -- see
#: the 1024x350 test scenario) bit activity is responsively hidden
#: (never the persisted preference) and every remaining margin is minimal.
HEIGHT_COMPACT = 620
HEIGHT_ULTRA = 460


def classify_width(width: int) -> SizeClass:
    if width < WIDTH_ULTRA:
        return SizeClass.ULTRA
    if width < WIDTH_COMPACT:
        return SizeClass.COMPACT
    return SizeClass.NORMAL


def classify_height(height: int) -> SizeClass:
    if height < HEIGHT_ULTRA:
        return SizeClass.ULTRA
    if height < HEIGHT_COMPACT:
        return SizeClass.COMPACT
    return SizeClass.NORMAL


@dataclass(frozen=True)
class ResponsiveState:
    width_class: SizeClass
    height_class: SizeClass

    @property
    def overall(self) -> SizeClass:
        return max(self.width_class, self.height_class)

    @property
    def compact(self) -> bool:
        """Either axis is at least compact -- the coarse "reduce density" flag."""
        return self.overall != SizeClass.NORMAL

    @property
    def ultra(self) -> bool:
        return self.overall == SizeClass.ULTRA

    @property
    def narrow(self) -> bool:
        return self.width_class != SizeClass.NORMAL

    @property
    def very_narrow(self) -> bool:
        return self.width_class == SizeClass.ULTRA

    @property
    def short(self) -> bool:
        return self.height_class != SizeClass.NORMAL

    @property
    def very_short(self) -> bool:
        return self.height_class == SizeClass.ULTRA


#: The state every window starts in before its first real layout pass --
#: also what a headless/offscreen test with no explicit geometry gets.
NORMAL_STATE = ResponsiveState(SizeClass.NORMAL, SizeClass.NORMAL)


def compute_responsive_state(width: int, height: int) -> ResponsiveState:
    return ResponsiveState(classify_width(width), classify_height(height))


@dataclass(frozen=True)
class Density:
    """One named bundle of spacing/size tokens. Never mutated in place --
    switching density means swapping which instance ``Theme.density`` points
    to, so a stale reference elsewhere is impossible by construction.
    """

    name: str
    #: Outer panel/card margins (SPACE_MD in NORMAL).
    margin: int
    #: Generic control spacing (SPACE_SM in NORMAL).
    spacing: int
    #: Tighter spacing for a row that is already reflowing (SPACE_XS-ish).
    tight_spacing: int
    #: Table row height, passed to setDefaultSectionSize.
    row_height: int
    #: QPushButton/QToolButton vertical/horizontal padding, in px.
    button_pad_v: int
    button_pad_h: int
    #: QLineEdit/QComboBox/QSpinBox vertical/horizontal padding, in px.
    input_pad_v: int
    input_pad_h: int
    #: QHeaderView::section padding, in px (each side).
    header_pad_v: int
    header_pad_h: int
    #: QTableView::item padding, in px.
    cell_pad_v: int
    cell_pad_h: int
    #: NavRail width and each _NavButton's height.
    nav_width: int
    nav_button_height: int
    #: Chip::setMaximumWidth ceiling for the top bar's source/DBC chips.
    chip_max_width: int
    #: Applied on top of (never in place of) the user's configured base
    #: font size -- see Theme.ui_font/mono_font. Always <= 0.
    font_delta: float


DENSITY_NORMAL = Density(
    name="normal", margin=SPACE_MD, spacing=SPACE_SM, tight_spacing=SPACE_XS,
    row_height=ROW_HEIGHT_COMPACT, button_pad_v=6, button_pad_h=14,
    input_pad_v=5, input_pad_h=8, header_pad_v=8, header_pad_h=10,
    cell_pad_v=5, cell_pad_h=10,
    nav_width=76, nav_button_height=58, chip_max_width=220, font_delta=0.0,
)

DENSITY_COMPACT = Density(
    name="compact", margin=SPACE_SM, spacing=SPACE_XS, tight_spacing=SPACE_XS,
    row_height=24, button_pad_v=4, button_pad_h=10,
    input_pad_v=3, input_pad_h=6, header_pad_v=5, header_pad_h=7,
    cell_pad_v=3, cell_pad_h=7,
    nav_width=64, nav_button_height=50, chip_max_width=160, font_delta=0.0,
)

DENSITY_ULTRA = Density(
    name="ultra", margin=SPACE_XS, spacing=SPACE_XS, tight_spacing=2,
    row_height=22, button_pad_v=3, button_pad_h=8,
    input_pad_v=2, input_pad_h=5, header_pad_v=4, header_pad_h=6,
    cell_pad_v=2, cell_pad_h=5,
    # 40px stays at/above the ~40-44px minimum recommended touch target
    # size even at this density -- see this project's own touch-usability
    # requirement; density never goes below this for a tappable control.
    nav_width=56, nav_button_height=40, chip_max_width=120, font_delta=-0.5,
)

_DENSITY_BY_CLASS = {
    SizeClass.NORMAL: DENSITY_NORMAL,
    SizeClass.COMPACT: DENSITY_COMPACT,
    SizeClass.ULTRA: DENSITY_ULTRA,
}


def density_for(size_class: SizeClass) -> Density:
    return _DENSITY_BY_CLASS[size_class]


#: A fourth, more spacious anchor for windows well past NORMAL's own
#: threshold -- without this, DENSITY_NORMAL was a hard ceiling: a
#: 1200px-wide window and a 2400px-wide one got byte-identical padding, nav
#: width and row height, so all of the second window's extra room went to
#: bare stretch/empty space while its controls read as needlessly small
#: relative to it. font_delta stays at 0.0, unchanged from NORMAL -- a
#: bigger monitor should not mean proportionally bigger text, only a
#: modestly larger, more comfortable set of controls and more breathing
#: room; see interpolated_density below for how far each property actually
#: grows between NORMAL and this.
DENSITY_SPACIOUS = Density(
    name="spacious", margin=SPACE_LG, spacing=SPACE_MD, tight_spacing=SPACE_SM,
    row_height=ROW_HEIGHT, button_pad_v=8, button_pad_h=18,
    input_pad_v=6, input_pad_h=10, header_pad_v=10, header_pad_h=12,
    cell_pad_v=6, cell_pad_h=12,
    nav_width=84, nav_button_height=64, chip_max_width=260, font_delta=0.0,
)


def _clamp01(t: float) -> float:
    return max(0.0, min(1.0, t))


def _blend_density(a: Density, b: Density, t: float) -> Density:
    """Linearly interpolate every numeric field between two named density
    anchors -- t=0 is exactly ``a``, t=1 is exactly ``b``. This is what lets
    the applied density change smoothly with actual window size instead of
    snapping between fixed tiers at each threshold; see interpolated_density.
    """
    t = _clamp01(t)

    def lerp(name: str) -> float:
        return getattr(a, name) + (getattr(b, name) - getattr(a, name)) * t

    return Density(
        name="responsive",
        margin=round(lerp("margin")), spacing=round(lerp("spacing")),
        tight_spacing=round(lerp("tight_spacing")), row_height=round(lerp("row_height")),
        button_pad_v=round(lerp("button_pad_v")), button_pad_h=round(lerp("button_pad_h")),
        input_pad_v=round(lerp("input_pad_v")), input_pad_h=round(lerp("input_pad_h")),
        header_pad_v=round(lerp("header_pad_v")), header_pad_h=round(lerp("header_pad_h")),
        cell_pad_v=round(lerp("cell_pad_v")), cell_pad_h=round(lerp("cell_pad_h")),
        nav_width=round(lerp("nav_width")), nav_button_height=round(lerp("nav_button_height")),
        chip_max_width=round(lerp("chip_max_width")), font_delta=lerp("font_delta"),
    )


def _min_density(a: Density, b: Density) -> Density:
    """Field-by-field "more constrained wins": every field here is larger
    for a more spacious density (a font_delta nearer 0 counts as "larger"
    too), so a plain per-field min() is exactly "whichever axis -- width or
    height -- is more constrained governs this property", the same rule
    ResponsiveState.overall already applies at the coarser, discrete level
    via max() over SizeClass.
    """
    return Density(
        name="responsive",
        margin=min(a.margin, b.margin), spacing=min(a.spacing, b.spacing),
        tight_spacing=min(a.tight_spacing, b.tight_spacing),
        row_height=min(a.row_height, b.row_height),
        button_pad_v=min(a.button_pad_v, b.button_pad_v),
        button_pad_h=min(a.button_pad_h, b.button_pad_h),
        input_pad_v=min(a.input_pad_v, b.input_pad_v),
        input_pad_h=min(a.input_pad_h, b.input_pad_h),
        header_pad_v=min(a.header_pad_v, b.header_pad_v),
        header_pad_h=min(a.header_pad_h, b.header_pad_h),
        cell_pad_v=min(a.cell_pad_v, b.cell_pad_v),
        cell_pad_h=min(a.cell_pad_h, b.cell_pad_h),
        nav_width=min(a.nav_width, b.nav_width),
        nav_button_height=min(a.nav_button_height, b.nav_button_height),
        chip_max_width=min(a.chip_max_width, b.chip_max_width),
        font_delta=min(a.font_delta, b.font_delta),
    )


def floor_density(density: Density, floor: Density) -> Density:
    """Field-by-field "roomier wins" (max()) -- the mirror image of
    _min_density's "more constrained wins" above. Raises every field of
    ``density`` up to at least ``floor``'s own value, never down: a
    ``density`` that is already roomier than ``floor`` on some or all axes
    (e.g. a host screen bigger than ``floor`` itself) passes through those
    fields unchanged, so this only ever raises a floor, never imposes a
    ceiling.

    Existing for exactly one caller: MainWindow's Lite edition, whose fixed
    small-touchscreen target relies on scrolling (see cansniff/ui/
    main_window.py's own Lite section) rather than density shrinking
    everything down to fit -- passing ``floor=DENSITY_NORMAL`` there means
    Lite's table/nav/chrome never renders smaller than the original
    desktop's own normal-density sizing, while a Lite window on a larger
    host screen can still grow past that via interpolated_density's own
    SPACIOUS anchors exactly as before.

    ``name`` becomes ``floor.name`` when every field actually landed on
    ``floor``'s own value (the common case for a small-screen ``density``),
    ``density.name`` when nothing needed raising, and "responsive"
    otherwise (a genuine per-field mix, e.g. one axis already past floor
    while another was not) -- this keeps every existing ``density.name==
    "normal"``-style check elsewhere (SignalPlot.restyle, DatabaseWindow,
    widgets.BitActivityMatrix) working correctly against a floored density,
    with no changes needed at any of those call sites.
    """
    fields = (
        "margin", "spacing", "tight_spacing", "row_height", "button_pad_v",
        "button_pad_h", "input_pad_v", "input_pad_h", "header_pad_v",
        "header_pad_h", "cell_pad_v", "cell_pad_h", "nav_width",
        "nav_button_height", "chip_max_width", "font_delta",
    )
    merged = {name: max(getattr(density, name), getattr(floor, name)) for name in fields}
    if all(merged[name] == getattr(floor, name) for name in fields):
        merged["name"] = floor.name
    elif all(merged[name] == getattr(density, name) for name in fields):
        merged["name"] = density.name
    else:
        merged["name"] = "responsive"
    return Density(**merged)


#: Anchor points for continuous density interpolation: ascending
#: (dimension, Density) pairs. Below the first anchor's dimension, density
#: is clamped to that anchor (never more compact than ULTRA); above the
#: last, clamped to that anchor (never more spacious than SPACIOUS -- there
#: is no fifth tier). Deliberately independent of classify_width/
#: classify_height's own WIDTH_COMPACT/WIDTH_ULTRA/HEIGHT_COMPACT/
#: HEIGHT_ULTRA thresholds: those drive discrete layout decisions (sidebar
#: auto-collapse, bit-activity hide, ...) that genuinely need one hard
#: boundary each; the *density* applied within a stretch of window sizes
#: that all share one discrete SizeClass does not, and blending it on its
#: own axis is what makes a 1300px window look different from a 2400px one
#: instead of both reading as flatly "NORMAL".
_WIDTH_DENSITY_ANCHORS = (
    (700, DENSITY_ULTRA), (1180, DENSITY_COMPACT),
    (1400, DENSITY_NORMAL), (1800, DENSITY_SPACIOUS),
)
_HEIGHT_DENSITY_ANCHORS = (
    (340, DENSITY_ULTRA), (460, DENSITY_COMPACT),
    (650, DENSITY_NORMAL), (850, DENSITY_SPACIOUS),
)

#: Quantization grid, in px, for the width/height interpolated_density
#: reads. Rounding down to this grid before interpolating means the small
#: (often 1px) size changes a resizeEvent delivers on every intermediate
#: frame of a drag reliably produce the *same* Density object -- frozen
#: dataclasses compare by value, so Theme.set_density's == check (see its
#: own docstring) skips the expensive findChildren(QWidget) restyle sweep
#: for the overwhelming majority of calls, exactly as the discrete
#: three-tier scheme this replaces already guaranteed by construction. See
#: MainWindow.resizeEvent's own docstring on why that sweep must stay rare.
_DENSITY_QUANTUM = 24


def _axis_density(value: int, anchors) -> Density:
    if value <= anchors[0][0]:
        return anchors[0][1]
    if value >= anchors[-1][0]:
        return anchors[-1][1]
    for (lo_v, lo_d), (hi_v, hi_d) in zip(anchors, anchors[1:]):
        if lo_v <= value <= hi_v:
            return _blend_density(lo_d, hi_d, (value - lo_v) / float(hi_v - lo_v))
    return anchors[-1][1]  # unreachable; keeps this total for type-checkers


def interpolated_density(width: int, height: int) -> Density:
    """The density actually applied at ``width`` x ``height`` -- continuous
    in both dimensions, rather than the 3-4 fixed steps ``density_for``
    alone gives. Width and height are each blended independently against
    their own anchors above, then combined field-by-field with whichever
    axis is more constrained winning (_min_density) -- matching
    ResponsiveState.overall's "most constrained wins" rule at the coarser,
    discrete level. Quantized (see _DENSITY_QUANTUM) so nearby sizes
    collapse onto identical, ``==``-comparable Density instances.
    """
    qw = (max(1, int(width)) // _DENSITY_QUANTUM) * _DENSITY_QUANTUM
    qh = (max(1, int(height)) // _DENSITY_QUANTUM) * _DENSITY_QUANTUM
    by_width = _axis_density(qw, _WIDTH_DENSITY_ANCHORS)
    by_height = _axis_density(qh, _HEIGHT_DENSITY_ANCHORS)
    if by_width is by_height:
        # Both axes clamped to (or blended onto) the exact same anchor --
        # most commonly both far below the smallest or far above the
        # largest. Returning it directly keeps a window that is tiny (or
        # huge) on both axes labelled with its real name instead of the
        # generic "responsive" _min_density always produces, and skips a
        # pointless field-by-field min() of a value against itself.
        return by_width
    return _min_density(by_width, by_height)


#: Smallest a responsive font is ever allowed to shrink to, regardless of how
#: negative a Density.font_delta or how small the user's own configured base
#: size is -- readability/touchability floor (see theme.py's Theme.ui_font).
MIN_FONT_PT = 7.0


__all__ = [
    "SizeClass", "ResponsiveState", "NORMAL_STATE", "compute_responsive_state",
    "classify_width", "classify_height", "WIDTH_COMPACT", "WIDTH_ULTRA",
    "HEIGHT_COMPACT", "HEIGHT_ULTRA", "Density", "DENSITY_NORMAL",
    "DENSITY_COMPACT", "DENSITY_ULTRA", "DENSITY_SPACIOUS", "density_for",
    "interpolated_density", "floor_density", "MIN_FONT_PT",
]
