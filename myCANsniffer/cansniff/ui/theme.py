"""Design tokens and the application stylesheet.

One source of truth for colour, spacing, radius and typography. Widgets never
hard-code a colour; they ask the theme for a token, which makes a restyle a
one-file change.

The application ships a single light palette. Contrast targets: body text on
surface is ~14:1, secondary text ~7:1, and muted text ~4.8:1 — all at or above
WCAG AA for their sizes.
"""

from __future__ import annotations

from string import Template
from typing import Dict, List

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

# ---------------------------------------------------------------------------
# scale
# ---------------------------------------------------------------------------

#: 4-point spacing scale. Every margin and gap in the UI is one of these.
SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24
SPACE_2XL = 32

RADIUS_SM = 4
RADIUS_MD = 6
RADIUS_LG = 8

#: Row heights. Readability first: values need room to be scanned quickly.
ROW_HEIGHT = 30
ROW_HEIGHT_COMPACT = 27
HEADER_HEIGHT = 32

_UI_FONTS = ["Segoe UI Variable Text", "Segoe UI", "Inter", "Noto Sans", "Arial"]
_MONO_FONTS = ["Cascadia Mono", "JetBrains Mono", "Consolas", "DejaVu Sans Mono", "Courier New"]


# ---------------------------------------------------------------------------
# palettes
# ---------------------------------------------------------------------------

PALETTE: Dict[str, str] = {
    "canvas": "#eef1f5",
    "surface": "#ffffff",
    "raised": "#f5f7fa",
    "overlay": "#eef1f5",
    "hover": "#e4e9f0",
    "border_subtle": "#e2e6ec",
    "border": "#ccd3dd",
    "border_strong": "#a8b1bf",
    "text": "#0f141c",
    "text_secondary": "#48515f",
    "text_muted": "#6d7686",
    "text_inverted": "#ffffff",
    "accent": "#1f5fd0",
    "accent_hover": "#3576e0",
    "accent_pressed": "#184ba6",
    "accent_wash": "#e4eefc",
    "success": "#1f7a45",
    "success_wash": "#e3f3e9",
    "warning": "#8a5d0a",
    "warning_wash": "#fbefd8",
    "danger": "#b32d20",
    "danger_wash": "#fce9e7",
    "selection": "#cfe1fb",
    "grid": "#e8ecf1",
    "stripe": "#f8fafc",
}


def _first_available(candidates: List[str], fallback: str) -> str:
    families = set(QFontDatabase.families())
    if not families:
        # No font database yet (queried before QApplication, or a headless
        # platform plugin). Prefer the first choice over the last resort —
        # Qt will substitute if it really is missing.
        return candidates[0] if candidates else fallback
    for name in candidates:
        if name in families:
            return name
    return fallback


class Theme:
    """Resolved tokens plus the stylesheet built from them.

    Fonts and colours are cached: ``ui_font``/``mono_font`` are called from
    model ``data()`` and from delegate ``paint()``, i.e. once per visible cell
    per repaint, so constructing a fresh QFont each time is pure waste.
    """

    def __init__(self, ui_size: float = 9.0, mono_size: float = 9.5,
                 mono_family: str = "", ui_family: str = ""):
        self.tokens = dict(PALETTE)
        self.ui_size = float(ui_size)
        self.mono_size = float(mono_size)
        self.ui_family = ui_family or _first_available(_UI_FONTS, "Arial")
        self.mono_family = mono_family or _first_available(_MONO_FONTS, "Courier New")
        self._font_cache: Dict[tuple, QFont] = {}
        self._color_cache: Dict[tuple, QColor] = {}

    def set_fonts(self, ui_size: float = 0.0, mono_size: float = 0.0,
                  mono_family: str = "") -> None:
        """Change typography in place; widgets keep their Theme reference."""
        if ui_size:
            self.ui_size = float(ui_size)
        if mono_size:
            self.mono_size = float(mono_size)
        if mono_family:
            self.mono_family = _first_available([mono_family] + _MONO_FONTS, self.mono_family)
        self._font_cache.clear()

    # -- token access ---------------------------------------------------

    def hex(self, token: str) -> str:
        return self.tokens.get(token, "#ff00ff")

    def color(self, token: str, alpha: int = 255) -> QColor:
        key = (token, alpha)
        color = self._color_cache.get(key)
        if color is None:
            color = QColor(self.hex(token))
            if alpha < 255:
                color.setAlpha(alpha)
            self._color_cache[key] = color
        return color

    def mix(self, token_a: str, token_b: str, ratio: float) -> QColor:
        """Blend two tokens; ratio 0 = a, 1 = b."""
        key = ("mix", token_a, token_b, round(ratio, 3))
        color = self._color_cache.get(key)
        if color is None:
            a, b = self.color(token_a), self.color(token_b)
            clamped = max(0.0, min(1.0, ratio))
            color = QColor(
                int(a.red() + (b.red() - a.red()) * clamped),
                int(a.green() + (b.green() - a.green()) * clamped),
                int(a.blue() + (b.blue() - a.blue()) * clamped),
            )
            self._color_cache[key] = color
        return color

    # -- fonts ----------------------------------------------------------

    def ui_font(self, size_delta: float = 0.0, bold: bool = False) -> QFont:
        key = ("ui", size_delta, bold)
        font = self._font_cache.get(key)
        if font is None:
            font = QFont(self.ui_family)
            font.setPointSizeF(self.ui_size + size_delta)
            font.setBold(bold)
            self._font_cache[key] = font
        return font

    def mono_font(self, size_delta: float = 0.0, bold: bool = False) -> QFont:
        key = ("mono", size_delta, bold)
        font = self._font_cache.get(key)
        if font is None:
            font = QFont(self.mono_family)
            font.setPointSizeF(self.mono_size + size_delta)
            font.setBold(bold)
            # Digits keep a constant advance so columns of numbers line up.
            font.setStyleHint(QFont.Monospace)
            font.setFixedPitch(True)
            self._font_cache[key] = font
        return font

    def label_font(self) -> QFont:
        """Small uppercase label used for section headers."""
        key = ("label",)
        font = self._font_cache.get(key)
        if font is None:
            font = QFont(self.ui_family)
            font.setPointSizeF(self.ui_size - 0.5)
            font.setBold(True)
            font.setCapitalization(QFont.AllUppercase)
            font.setLetterSpacing(QFont.PercentageSpacing, 108)
            self._font_cache[key] = font
        return font

    # -- application palette --------------------------------------------

    def qpalette(self) -> QPalette:
        palette = QPalette()
        palette.setColor(QPalette.Window, self.color("canvas"))
        palette.setColor(QPalette.WindowText, self.color("text"))
        palette.setColor(QPalette.Base, self.color("surface"))
        palette.setColor(QPalette.AlternateBase, self.color("stripe"))
        palette.setColor(QPalette.Text, self.color("text"))
        palette.setColor(QPalette.PlaceholderText, self.color("text_muted"))
        palette.setColor(QPalette.Button, self.color("raised"))
        palette.setColor(QPalette.ButtonText, self.color("text"))
        palette.setColor(QPalette.Highlight, self.color("selection"))
        palette.setColor(QPalette.HighlightedText, self.color("text"))
        palette.setColor(QPalette.ToolTipBase, self.color("overlay"))
        palette.setColor(QPalette.ToolTipText, self.color("text"))
        palette.setColor(QPalette.Link, self.color("accent"))
        disabled = self.color("text_muted")
        for group in (QPalette.Disabled,):
            palette.setColor(group, QPalette.Text, disabled)
            palette.setColor(group, QPalette.ButtonText, disabled)
            palette.setColor(group, QPalette.WindowText, disabled)
        return palette

    # -- stylesheet ------------------------------------------------------

    def stylesheet(self) -> str:
        values = dict(self.tokens)
        values.update({
            "ui_family": self.ui_family,
            "mono_family": self.mono_family,
            "ui_size": "{:g}pt".format(self.ui_size),
            "ui_size_sm": "{:g}pt".format(self.ui_size - 1),
            "mono_size": "{:g}pt".format(self.mono_size),
            "radius_sm": "{}px".format(RADIUS_SM),
            "radius_md": "{}px".format(RADIUS_MD),
            "radius_lg": "{}px".format(RADIUS_LG),
            "space_xs": "{}px".format(SPACE_XS),
            "space_sm": "{}px".format(SPACE_SM),
            "space_md": "{}px".format(SPACE_MD),
            "row_height": "{}px".format(ROW_HEIGHT),
            "header_height": "{}px".format(HEADER_HEIGHT),
        })
        return Template(_QSS).safe_substitute(values)


# ---------------------------------------------------------------------------
# stylesheet source
# ---------------------------------------------------------------------------
# Written with string.Template ($token) because QSS itself uses braces.

_QSS = """
QWidget {
    background-color: $canvas;
    color: $text;
    font-family: "$ui_family";
    font-size: $ui_size;
}

QMainWindow, QDialog { background-color: $canvas; }

/* Text should sit on whatever it is placed over. Without this every label
   inherits the canvas fill above and paints a grey bar behind itself, which
   is obvious the moment one lands on a white card. The few labels that do
   want a fill (#Banner, #CountBadge, chips) set it themselves and their ID
   selectors outrank this. */
QLabel { background: transparent; }

QToolTip {
    background-color: $overlay;
    color: $text;
    border: 1px solid $border;
    border-radius: $radius_sm;
    padding: 6px 8px;
}

/* ---------------------------------------------------------------- panels */

QFrame#Panel {
    background-color: $surface;
    border: 1px solid $border_subtle;
    border-radius: $radius_lg;
}

QFrame#Divider {
    background-color: $border_subtle;
    max-height: 1px;
    border: none;
}

QLabel#SectionLabel { color: $text_muted; }
QLabel#Caption { color: $text_secondary; font-size: $ui_size_sm; }
QLabel#Muted { color: $text_muted; font-size: $ui_size_sm; }

QLabel#Banner {
    background-color: $danger_wash;
    color: $danger;
    border: 1px solid $danger;
    border-radius: $radius_md;
    padding: 10px 14px;
    font-weight: 600;
}

/* --------------------------------------------------------------- top bar */

QFrame#TopBar {
    background-color: $surface;
    border-bottom: 1px solid $border_subtle;
}

QLabel#AppTitle { font-size: $ui_size; font-weight: 600; color: $text; }

/* ------------------------------------------------------------ navigation */

QFrame#NavRail {
    background-color: $surface;
    border-right: 1px solid $border_subtle;
}
QPushButton#NavButton {
    background: transparent;
    border: none;
    padding: 0px;
    margin: 0px;
    min-height: 58px;
    text-align: center;
}

/* ------------------------------------------------------- columns popup  */

QFrame#ColumnsPopup {
    background-color: $surface;
    border: 1px solid $border_strong;
    border-radius: $radius_lg;
}
QListWidget#ColumnsList {
    background-color: $surface;
    border: 1px solid $border_subtle;
    border-radius: $radius_md;
    outline: none;
    padding: 2px;
}
QListWidget#ColumnsList::item {
    padding: 5px 6px;
    border-radius: $radius_sm;
    color: $text;
}
QListWidget#ColumnsList::item:hover { background-color: $hover; }
QListWidget#ColumnsList::item:selected {
    background-color: $accent_wash;
    color: $text;
}
/* On/off has to be readable at a glance across a 17-row list, so a checked
   column fills solid accent rather than showing a small tick on white. */
QListWidget#ColumnsList::indicator {
    width: 15px;
    height: 15px;
    border-radius: $radius_sm;
    border: 1px solid $border_strong;
    background-color: $surface;
    margin-right: 2px;
}
QListWidget#ColumnsList::indicator:hover { border-color: $accent; }
QListWidget#ColumnsList::indicator:checked {
    background-color: $accent;
    border-color: $accent;
}

/* Bare containers pick up the canvas colour from the global QWidget rule,
   which reads as a stray grey slab when they sit inside a white card. */
QWidget#PayloadDetail,
QScrollArea#PayloadScroll,
QScrollArea#PayloadScroll > QWidget > QWidget { background: transparent; }

QPushButton#Disclosure {
    background: transparent;
    border: none;
    padding: 2px 4px;
    color: $text_secondary;
    font-weight: 600;
    text-align: left;
}
QPushButton#Disclosure:hover   { color: $accent; }
QPushButton#Disclosure:checked { color: $text; }

/* Compact prev/next transfer navigator -- see isotp_view.py's own
   _build_navigator. Small and round rather than the default button padding,
   which would make a `<` on its own look like an oddly narrow label. */
QPushButton#NavArrow {
    background-color: $raised;
    border: 1px solid $border;
    border-radius: 12px;
    padding: 0px;
    font-weight: 700;
    color: $text_secondary;
}
QPushButton#NavArrow:hover    { background-color: $hover; color: $text; border-color: $border_strong; }
QPushButton#NavArrow:pressed  { background-color: $overlay; }
QPushButton#NavArrow:disabled { color: $text_muted; background-color: $surface; border-color: $border_subtle; }

QLabel#CountBadge {
    background-color: $accent;
    color: $text_inverted;
    border-radius: 9px;
    min-width: 18px;
    min-height: 18px;
    padding: 0px 5px;
    font-size: $ui_size_sm;
    font-weight: 700;
}

/* ---------------------------------------------------------------- chips  */

QFrame#FilterChip {
    background-color: $accent_wash;
    border: 1px solid $accent;
    border-radius: $radius_sm;
}
QFrame#FilterChip QLabel { color: $accent; background: transparent; }
QPushButton#ChipClose {
    background: transparent;
    border: none;
    color: $accent;
    font-weight: 700;
    padding: 0px;
}
QPushButton#ChipClose:hover {
    background-color: $accent;
    color: $text_inverted;
    border-radius: 8px;
}

QFrame#FilterPanel {
    background-color: $raised;
    border: 1px solid $border_subtle;
    border-radius: $radius_md;
}

/* --------------------------------------------------------------- buttons */

QPushButton {
    background-color: $raised;
    color: $text;
    border: 1px solid $border;
    border-radius: $radius_md;
    padding: 6px 14px;
    min-height: 18px;
}
QPushButton:hover  { background-color: $hover; border-color: $border_strong; }
QPushButton:pressed{ background-color: $overlay; }
QPushButton:disabled { color: $text_muted; background-color: $surface; border-color: $border_subtle; }

QPushButton#Primary {
    background-color: $accent;
    color: $text_inverted;
    border: 1px solid $accent;
    font-weight: 600;
}
QPushButton#Primary:hover   { background-color: $accent_hover; border-color: $accent_hover; }
QPushButton#Primary:pressed { background-color: $accent_pressed; }
QPushButton#Primary:disabled {
    background-color: $overlay; color: $text_muted; border-color: $border_subtle;
}

QPushButton#Ghost {
    background-color: transparent;
    border-color: transparent;
    color: $text_secondary;
}
QPushButton#Ghost:hover { background-color: $hover; color: $text; }

QPushButton#Danger { border-color: $danger; color: $danger; }
QPushButton#Danger:hover { background-color: $danger_wash; }

QPushButton:checked {
    background-color: $accent_wash;
    border-color: $accent;
    color: $accent;
}

QToolButton {
    background-color: $raised;
    border: 1px solid $border;
    border-radius: $radius_md;
    padding: 5px 10px;
    color: $text;
}
QToolButton:hover { background-color: $hover; }
QToolButton::menu-indicator { image: none; }

/* ------------------------------------------------------------- segmented */

QWidget#Segmented { background: transparent; }
QWidget#Segmented QPushButton {
    background-color: $raised;
    border: 1px solid $border;
    border-radius: 0px;
    padding: 5px 12px;
    margin: 0px;
    color: $text_secondary;
}
QWidget#Segmented QPushButton:first-child {
    border-top-left-radius: $radius_md; border-bottom-left-radius: $radius_md;
}
QWidget#Segmented QPushButton:last-child {
    border-top-right-radius: $radius_md; border-bottom-right-radius: $radius_md;
}
QWidget#Segmented QPushButton:checked {
    background-color: $accent_wash; color: $accent; border-color: $accent;
}
QWidget#Segmented QPushButton:hover:!checked { background-color: $hover; color: $text; }

/* ---------------------------------------------------------------- inputs */

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: $canvas;
    color: $text;
    border: 1px solid $border;
    border-radius: $radius_md;
    padding: 5px 8px;
    selection-background-color: $selection;
    selection-color: $text;
}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QComboBox:focus {
    border-color: $accent;
}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {
    color: $text_muted; background-color: $surface;
}
QLineEdit#Search { padding-left: 10px; }

QComboBox::drop-down { border: none; width: 18px; }
QComboBox::down-arrow {
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid $text_secondary;
    margin-right: 6px;
}
QComboBox QAbstractItemView {
    background-color: $overlay;
    border: 1px solid $border;
    border-radius: $radius_md;
    selection-background-color: $selection;
    outline: none;
    padding: 4px;
}

/* Spin buttons are deliberately left to the native style. Styling the button
   sub-control without also supplying arrow images makes Qt drop the arrows
   entirely, and the CSS border-triangle substitute renders as a filled block
   rather than an arrow. */

QCheckBox { spacing: 8px; color: $text; }
QCheckBox::indicator {
    width: 15px; height: 15px;
    border: 1px solid $border_strong;
    border-radius: $radius_sm;
    background-color: $canvas;
}
QCheckBox::indicator:hover { border-color: $accent; }
QCheckBox::indicator:checked {
    background-color: $accent;
    border-color: $accent;
    image: none;
}

/* ---------------------------------------------------------------- tables */

QTableView {
    background-color: $surface;
    alternate-background-color: $stripe;
    gridline-color: $grid;
    border: 1px solid $border;
    border-radius: $radius_md;
    selection-background-color: $selection;
    selection-color: $text;
    outline: none;
}
QTableView::item { padding: 5px 10px; border: none; }
QTableView::item:selected { background-color: $selection; color: $text; }

QHeaderView { background-color: $raised; }
QHeaderView::section {
    background-color: $raised;
    color: $text_secondary;
    padding: 8px 10px;
    border: none;
    border-right: 1px solid $border_subtle;
    border-bottom: 2px solid $border;
    font-size: $ui_size_sm;
    font-weight: 700;
}
QHeaderView::section:hover { color: $text; background-color: $hover; }
QHeaderView::section:last { border-right: none; }
QTableCornerButton::section { background-color: $raised; border: none; }

/* ------------------------------------------------------------------ tabs */

QTabWidget::pane {
    border: 1px solid $border_subtle;
    border-radius: $radius_md;
    background-color: $surface;
    top: -1px;
}
QTabBar::tab {
    background-color: transparent;
    color: $text_muted;
    padding: 7px 16px;
    margin-right: 2px;
    border: 1px solid transparent;
    border-top-left-radius: $radius_md;
    border-top-right-radius: $radius_md;
}
QTabBar::tab:hover { color: $text; background-color: $hover; }
QTabBar::tab:selected {
    color: $text;
    background-color: $surface;
    border-color: $border_subtle;
    border-bottom-color: $surface;
}

/* ------------------------------------------------------------- scrollbar */

QScrollBar:vertical {
    background: transparent; width: 12px; margin: 0px;
}
QScrollBar::handle:vertical {
    background: $border_strong; min-height: 32px;
    border-radius: 6px; margin: 2px;
}
QScrollBar::handle:vertical:hover { background: $text_muted; }
QScrollBar:horizontal {
    background: transparent; height: 12px; margin: 0px;
}
QScrollBar::handle:horizontal {
    background: $border_strong; min-width: 32px;
    border-radius: 6px; margin: 2px;
}
QScrollBar::handle:horizontal:hover { background: $text_muted; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0px; width: 0px; border: none; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

/* ----------------------------------------------------------------- menus */

QMenu {
    background-color: $overlay;
    border: 1px solid $border;
    border-radius: $radius_md;
    padding: 6px;
}
QMenu::item {
    padding: 6px 26px 6px 22px;
    border-radius: $radius_sm;
    color: $text;
}
QMenu::item:selected { background-color: $selection; }
QMenu::indicator {
    width: 14px; height: 14px; left: 6px;
    border: 1px solid $border_strong;
    border-radius: 3px;
    background: $canvas;
}
QMenu::indicator:checked { background: $accent; border-color: $accent; }
QMenu::separator { height: 1px; background: $border_subtle; margin: 5px 8px; }

/* ------------------------------------------------------------ group box */

QGroupBox {
    background-color: $surface;
    border: 1px solid $border_subtle;
    border-radius: $radius_md;
    margin-top: 18px;
    padding: $space_md;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0px 4px;
    color: $text_secondary;
}

/* -------------------------------------------------------------- splitter */

QSplitter::handle { background-color: transparent; }
QSplitter::handle:horizontal { width: 8px; }
QSplitter::handle:vertical { height: 8px; }
QSplitter::handle:hover { background-color: $accent_wash; }

/* ------------------------------------------------------------ status bar */

QStatusBar { background-color: $surface; border-top: 1px solid $border_subtle; }
QStatusBar::item { border: none; }

/* ------------------------------------------------------------- scrollarea */

QScrollArea { border: none; background: transparent; }
QDialogButtonBox QPushButton { min-width: 84px; }
"""
