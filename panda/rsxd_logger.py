# =============================================================================
# RSXD SCAN LOGGER  --  desktop GUI  (phase 1)
# Diamond Light Source, beamline I10 / RASOR
# =============================================================================
# Run:    python rsxd_logger.py            (or double-click run_rsxd_logger.bat)
# Config: rsxd_config.toml  beside this file
#
# All experiment logic lives in rsxd_core.py. This file is presentation and
# key handling only -- so phase 2 can import the same core, and so this whole
# UI can be dropped into a larger app as a single frame:
#
#     from rsxd_logger import RSXDLoggerFrame
#     RSXDLoggerFrame(parent_notebook).pack(fill="both", expand=True)
#
# Visual language: neutral dark grey instrument chrome, ONE accent used only
# for active/focused/primary state, three status hues (amber/green/red), and
# grey for everything else. Numbers are always mono. See the theme block below.
# =============================================================================

from __future__ import annotations

import sys
import traceback
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, ttk

from rsxd_core import (
    VERSION,
    Config,
    DAT_FOUND_NO,
    DAT_FOUND_UNKNOWN,
    DAT_FOUND_YES,
    Session,
    boundary_scans,
    effective_gain,
    fmt_gain,
    parse_gain,
)

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "rsxd_config.toml"

# Presentation-layer version. The data layer's version lives in rsxd_core and
# is deliberately independent -- a retheme must not imply a data change.
UI_VERSION = "v2026-08-08-gain-tracking"


# =============================================================================
# THEME  --  neutral dark grey, one accent
# =============================================================================
# Reference class is instrument software (Blender, napari, Fusion, Grafana),
# not web design. Two rules govern everything below:
#
#   1. The plot canvas is the loudest object on screen. All chrome recedes.
#   2. Colour encodes STATE, never decoration. One accent for "active",
#      three status hues, and otherwise grey.
#
# Surfaces are deliberately neutral rather than blue-black: the scientific
# colormaps used in the plots are chosen for perceptual uniformity and CVD
# safety, and a blue-tinted chrome shifts how they read.

# Surfaces -- four steps only: base < panel < card < input.
BG_BASE = "#141414"
BG_PANEL = "#1b1b1b"
BG_CARD = "#212121"
BG_INPUT = "#191919"
BG_HOVER = "#2b2b2b"

# Borders -- near invisible. If you can clearly see a border, it is too bright.
BORDER = "#2a2a2a"
BORDER_HI = "#3a3a3a"

# THE accent. Used ONLY for: active tab, focused input, primary button,
# current selection. Never for decoration.
CYAN = "#4da3ff"
CYAN_DIM = "#2f6ea8"

# Status only. AMBER = warning/stale, GREEN = ok/complete, RED = error.
AMBER = "#e0a030"
AMBER_DIM = "#966a1c"
GREEN = "#5fb87a"
GREEN_DIM = "#3d7a50"
RED = "#e05252"
RED_DIM = "#8f3434"

# SEMANTIC, not decorative: "this cell carries an operator note".
# An earlier pass neutralised this to grey on the grounds that it was
# decoration. That was wrong -- a note is a real, distinct piece of state, and
# it needs a marker that is neither the focus accent nor one of the three
# status hues. Desaturated to sit alongside them rather than shout over them.
VIOLET = "#a98ad9"

# Text -- three steps, with real contrast between them.
T1 = "#f0f0f0"              # primary values, headings
T2 = "#9a9a9a"              # labels, secondary
T3 = "#5a5a5a"              # disabled, hints

BG_DUP = "#2e2020"          # duplicate-scan cell tint
SIDEBAR_W = 345             # fixed control column

# Epoch banding. A cell's background steps up a shade for each gain epoch after
# the first, so a grid that is not in consistent units looks different at a
# glance. Deliberately tiny steps: this is information, not decoration, and it
# must not compete with the status borders.
GAIN_TINTS = ["#191919", "#1e1e1e", "#232323", "#282828", "#2d2d2d"]


def _pick_font(candidates, size, weight="normal"):
    """
    Rajdhani / Inter / JetBrains Mono are webfonts the Quanta app pulls from
    Google. On a bare Windows box they are not installed, so fall back to the
    closest thing that ships with Windows 11: Bahnschrift is a condensed
    geometric face that reads very like Rajdhani.
    """
    import tkinter.font as tkfont
    try:
        available = set(tkfont.families())
    except tk.TclError:
        available = set()
    for name in candidates:
        if name in available:
            return (name, size, weight)
    return ("TkDefaultFont", size, weight)


class Fonts:
    """
    Built lazily -- tkfont.families() needs a live Tk root.

    The scale is deliberately wide. Everything used to sit between 8 and 11 px,
    which reads as one flat band and gives the eye nothing to follow: the app
    wordmark, a field label and a log line all looked equally important. The
    range now runs 8 -> 18 so hierarchy is visible at a glance.

    Every NUMBER in the app is rendered in a mono face, so digits line up
    vertically in tables and readouts and a changing value does not reflow.
    """

    def __init__(self):
        # Display / headings: Rajdhani analogue (condensed, geometric).
        self.h1 = _pick_font(["Rajdhani", "Bahnschrift SemiBold", "Segoe UI Semibold"], 17, "bold")
        self.sec = _pick_font(["Rajdhani", "Bahnschrift SemiBold", "Segoe UI Semibold"], 10, "bold")
        self.btn = _pick_font(["Rajdhani", "Bahnschrift SemiBold", "Segoe UI Semibold"], 10, "bold")
        # Body / labels: Inter analogue.
        self.ui = _pick_font(["Inter", "Segoe UI"], 9)
        self.tiny = _pick_font(["Inter", "Segoe UI"], 8)
        # Numerics: JetBrains Mono analogue.
        self.mono = _pick_font(["JetBrains Mono", "Cascadia Mono", "Consolas"], 11)
        self.mono_sm = _pick_font(["JetBrains Mono", "Cascadia Mono", "Consolas"], 8)
        self.mono_big = _pick_font(["JetBrains Mono", "Cascadia Mono", "Consolas"], 18, "bold")
        # Grid cells: one notch down so all 13 analyser angles fit side by side
        # on a 1280-wide laptop screen without scrolling.
        self.cell = _pick_font(["JetBrains Mono", "Cascadia Mono", "Consolas"], 10)


# =============================================================================
# SMALL STYLED BUILDING BLOCKS
# =============================================================================

def section_header(parent, text, fonts, colour=T2):
    """
    A quiet section title: uppercase, small, secondary text, with a hairline
    rule under it.

    The previous version drew a 3px coloured bar to the left of the label. It
    read as a stray glyph rather than as structure, and because each caller
    passed its own accent it turned the sidebar into a colour chart. The
    typography alone carries the hierarchy now.

    `colour` stays in the signature so a genuinely stateful header can be
    coloured (e.g. an error section), but the default -- and every current
    caller -- is neutral.
    """
    wrap = tk.Frame(parent, bg=BG_PANEL)
    tk.Label(wrap, text=text.upper(), font=fonts.sec, fg=colour,
             bg=BG_PANEL, anchor="w").pack(side="left")
    sep = tk.Frame(parent, bg=BORDER, height=1)
    return wrap, sep


def field_label(parent, text, fonts):
    return tk.Label(parent, text=text, font=fonts.tiny, fg=T2, bg=BG_PANEL, anchor="w")


def make_button(parent, text, command, fonts, accent=None, fill=False):
    """
    Flat button. tk.Button rather than ttk so the colours are honoured
    identically on every Windows theme.

    Two roles only:

      fill=True   the PRIMARY action of a panel. Solid CYAN -- the one accent,
                  which is what makes it findable. At most one per panel.
      fill=False  everything else. Neutral: card background, hairline border,
                  primary text, hover lifts the background only.

    `accent` is honoured for the filled variant. Outline buttons ignore it by
    design: they used to be tinted amber/green/violet purely to decorate, which
    is exactly the noise this theme removes.
    """
    if fill:
        accent = accent or CYAN
        bg, fg, border = accent, BG_BASE, accent
        hover_bg, hover_fg = _lift(accent), BG_BASE
    else:
        bg, fg, border = BG_CARD, T1, BORDER_HI
        hover_bg, hover_fg = BG_HOVER, T1

    b = tk.Button(
        parent, text=text.upper(), command=command,
        font=fonts.btn, fg=fg, bg=bg,
        activeforeground=hover_fg, activebackground=hover_bg,
        relief="flat", bd=0, highlightthickness=1,
        highlightbackground=border, highlightcolor=border,
        padx=8, pady=5, cursor="hand2",
    )
    b.bind("<Enter>", lambda _e: b.configure(bg=hover_bg, fg=hover_fg))
    b.bind("<Leave>", lambda _e: b.configure(bg=bg, fg=fg))
    return b


def _lift(hex_colour, amount=0.12):
    """Slightly brighter version of a colour, for hover states."""
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    r, g, b = (min(255, int(c + (255 - c) * amount)) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def shorten_path(path, keep=3):
    """
    Ellipsise a path from the LEFT: '…\\rsxd-logger\\output'.

    The tail is the part that identifies the folder; the drive letter and the
    long prefix are the part you already know. Truncating from the right (the
    default behaviour of a too-narrow label) throws away the only useful bit.
    """
    if not path:
        return "(not set)"
    parts = Path(str(path)).parts
    if len(parts) <= keep + 1:
        return str(path)
    return "…\\" + "\\".join(parts[-keep:])


class Tooltip:
    """
    Minimal hover tooltip. Used to carry the full text of anything that had to
    be ellipsised, so shortening a path never loses information.
    """

    def __init__(self, widget, text_fn):
        self.widget = widget
        self.text_fn = text_fn if callable(text_fn) else (lambda: text_fn)
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _event=None):
        text = self.text_fn()
        if not text or self.tip is not None:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=text, bg=BG_CARD, fg=T1,
                 font=("Consolas", 8), relief="flat", bd=0,
                 highlightthickness=1, highlightbackground=BORDER_HI,
                 padx=7, pady=4, justify="left").pack()

    def _hide(self, _event=None):
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


# =============================================================================
# MAIN FRAME
# =============================================================================

class RSXDLoggerFrame(tk.Frame):
    """
    The entire phase-1 UI as one frame.

    Standalone:  main() drops it into a bare Tk root.
    Embedded:    hand it any parent (a ttk.Notebook page, a PanedWindow, ...).
                 It owns no top-level window and installs its shortcuts on its
                 own bind tag, so it will not fight a host app for keys.
    """

    def __init__(self, parent, config_path=CONFIG_PATH, show_header=True,
                 log_sink=None, **kw):
        """
        show_header  draw the app wordmark bar. Standalone: yes. Embedded in a
                     host that already has one (PANDA): no -- two stacked title
                     bars for the same window is wasted vertical space, and the
                     tab is already labelled.
        log_sink     callable(msg, colour). When given, this frame writes into
                     the host's log instead of building a second log panel.
        """
        super().__init__(parent, bg=BG_BASE, **kw)
        self.fonts = Fonts()
        self._show_header = show_header
        self._log_sink = log_sink

        self.cfg = Config.load(config_path)
        self.session = Session(self.cfg)

        self.cells: dict[tuple[str, str], tk.Entry] = {}      # (angle, pol) -> Entry
        self.cell_vars: dict[tuple[str, str], tk.StringVar] = {}
        self.cell_holders: dict[tuple[str, str], tk.Frame] = {}
        self.cell_rules: dict[tuple[str, str], tk.Frame] = {}
        # The gain currently dialled in, as last entered by the operator. It is
        # STICKY: every scan logged from now on inherits it until it changes.
        self.current_gain: float | None = None
        self._grid_needs_scroll = False
        self.count_labels: dict[str, tk.Label] = {}
        self._suspend_trace = False
        self._focus_cell: tuple[str, str] | None = None
        self._note_bound: tuple[str, str] | None = None
        self._csv_job = None

        self._build_theme()
        self._build_header()
        self._build_body()

        self.log(f"RSXD Scan Logger  core {VERSION}  ui {UI_VERSION}")
        self.log(f"Config: {self.cfg.path}")

        restored = self.session.load_session()
        if restored:
            self.log(f"Session restored: {self.session.total_scans()} scans logged.", GREEN)
        else:
            self.log("New session.")
        self._warn_orphans()

        self.peak_var.set(self.session.ui_peak)
        self.temp_var.set(self.session.ui_temp)
        self.out_var.set(str(self.cfg.output_dir))
        self.dat_var.set(self.cfg.dat_dir)
        self._report_dat_dir()

        self.load_grid()
        self.after(120, self.focus_first_empty)

    # -------------------------------------------------------------------------
    # THEME
    # -------------------------------------------------------------------------
    def _build_theme(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")     # the only stock theme that honours colours
        except tk.TclError:
            pass
        style.configure(
            "Q.TCombobox",
            fieldbackground=BG_INPUT, background=BG_INPUT, foreground=T1,
            arrowcolor=T2, bordercolor=BORDER, lightcolor=BORDER,
            darkcolor=BORDER, selectbackground=BG_INPUT, selectforeground=T1,
            padding=4,
        )
        style.map(
            "Q.TCombobox",
            fieldbackground=[("readonly", BG_INPUT)],
            foreground=[("readonly", T1)],
            bordercolor=[("focus", CYAN)], lightcolor=[("focus", CYAN)],
            darkcolor=[("focus", CYAN)],
        )
        root = self.winfo_toplevel()
        root.option_add("*TCombobox*Listbox.background", BG_CARD)
        root.option_add("*TCombobox*Listbox.foreground", T1)
        root.option_add("*TCombobox*Listbox.selectBackground", CYAN)
        root.option_add("*TCombobox*Listbox.selectForeground", BG_BASE)
        root.option_add("*TCombobox*Listbox.font", self.fonts.mono)

    # -------------------------------------------------------------------------
    # HEADER
    # -------------------------------------------------------------------------
    def _build_header(self):
        """
        The wordmark bar. Only drawn standalone -- see show_header. Carries
        nothing but identity, so removing it costs no information.
        """
        if not self._show_header:
            return
        hdr = tk.Frame(self, bg=BG_PANEL, height=56)
        hdr.pack(side="top", fill="x")
        hdr.pack_propagate(False)
        tk.Frame(self, bg=BORDER, height=1).pack(side="top", fill="x")

        logo = tk.Frame(hdr, bg=BG_PANEL)
        logo.pack(side="left", padx=(16, 14), pady=8)
        tk.Label(logo, text="RSXD SCAN LOGGER", font=self.fonts.h1,
                 fg=T1, bg=BG_PANEL, anchor="w").pack(anchor="w")
        tk.Label(logo, text="Diamond · I10 / RASOR", font=self.fonts.tiny,
                 fg=T2, bg=BG_PANEL, anchor="w").pack(anchor="w")

    def _build_status_strip(self, parent):
        """
        The KPI chips and the .dat-check state, in the TAB BODY rather than in
        a title bar. They belong to the grid they describe, so they follow it
        into whichever window hosts this frame.

        Only the value carries colour, and only when it is non-zero: a row of
        permanently-lit numbers is decoration, whereas a single amber count
        against three grey ones is information.
        """
        strip = tk.Frame(parent, bg=BG_BASE)
        strip.pack(side="top", fill="x", pady=(0, 8))

        self.readouts = {}
        for k, label in (("scans", "SCANS LOGGED"),
                         ("empty", "EMPTY THIS GRID"),
                         ("dupes", "DUPLICATES"),
                         ("missing", ".DAT MISSING"),
                         ("gain", "MIXED GAIN")):
            box = tk.Frame(strip, bg=BG_CARD, highlightthickness=1,
                           highlightbackground=BORDER)
            box.pack(side="left", padx=(0, 8), ipadx=12, ipady=4)
            tk.Label(box, text=label, font=self.fonts.tiny, fg=T2,
                     bg=BG_CARD).pack()
            v = tk.Label(box, text="0", font=self.fonts.mono_big, fg=T1,
                         bg=BG_CARD)
            v.pack()
            self.readouts[k] = v

        self.dat_state = tk.Label(strip, text="", font=self.fonts.tiny,
                                  fg=T3, bg=BG_BASE, justify="right", anchor="e")
        self.dat_state.pack(side="right", padx=(8, 0))
        Tooltip(self.dat_state, lambda: self.cfg.dat_dir or "no data directory set")

        # ── epoch summary ───────────────────────────────────────────────────
        # Read-only, one line per epoch. The chip says "something is wrong";
        # this says exactly which scans are in which units.
        self.epoch_box = tk.Frame(parent, bg=BG_PANEL, highlightthickness=1,
                                  highlightbackground=BORDER)
        self.epoch_box.pack(side="top", fill="x", pady=(0, 8))
        inner = tk.Frame(self.epoch_box, bg=BG_PANEL)
        inner.pack(fill="x", padx=12, pady=6)
        tk.Label(inner, text="GAIN EPOCHS", font=self.fonts.sec, fg=T2,
                 bg=BG_PANEL).pack(side="left", padx=(0, 14))
        self.epoch_label = tk.Label(inner, text="—", font=self.fonts.mono_sm,
                                    fg=T2, bg=BG_PANEL, anchor="w",
                                    justify="left")
        self.epoch_label.pack(side="left", fill="x", expand=True)

    # -------------------------------------------------------------------------
    # BODY
    # -------------------------------------------------------------------------
    def _build_body(self):
        body = tk.Frame(self, bg=BG_BASE)
        body.pack(side="top", fill="both", expand=True)

        self._build_sidebar(body)

        right = tk.Frame(body, bg=BG_BASE)
        right.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)

        self._build_status_strip(right)
        self._build_grid(right)
        self._build_notebar(right)
        self._build_log(right)

    # ---- sidebar ------------------------------------------------------------
    def _build_sidebar(self, parent):
        side = tk.Frame(parent, bg=BG_PANEL, width=SIDEBAR_W, highlightthickness=1,
                        highlightbackground=BORDER)
        side.pack(side="left", fill="y", padx=10, pady=10)
        side.pack_propagate(False)
        pad = dict(padx=12, fill="x")

        # ── ACQUISITION ──────────────────────────────────────────────────────
        h, s = section_header(side, "Acquisition", self.fonts)
        h.pack(pady=(12, 0), **pad)
        s.pack(pady=(4, 8), **pad)

        field_label(side, "PEAK   (Alt+Left / Alt+Right)", self.fonts).pack(**pad)
        self.peak_var = tk.StringVar()
        self.peak_box = ttk.Combobox(side, textvariable=self.peak_var, style="Q.TCombobox",
                                     values=list(self.cfg.peaks), state="readonly",
                                     font=self.fonts.mono)
        self.peak_box.pack(pady=(2, 8), **pad)
        self.peak_box.bind("<<ComboboxSelected>>", lambda e: self.on_context_change())

        field_label(side, "TEMPERATURE / K   (Alt+Up / Alt+Down)", self.fonts).pack(**pad)
        self.temp_var = tk.StringVar()
        self.temp_box = ttk.Combobox(side, textvariable=self.temp_var, style="Q.TCombobox",
                                     values=list(self.cfg.temperatures), state="readonly",
                                     font=self.fonts.mono)
        self.temp_box.pack(pady=(2, 8), **pad)
        self.temp_box.bind("<<ComboboxSelected>>", lambda e: self.on_context_change())

        # ── gain ────────────────────────────────────────────────────────────
        # The instrument writes NO gain into the .dat file, so this field is
        # the only record that will ever exist. Sticky by design: the operator
        # sets it when they turn the dial, and every scan logged afterwards
        # inherits it until they turn it again.
        field_label(side, "GAIN (V/A)   sticky", self.fonts).pack(**pad)
        grow = tk.Frame(side, bg=BG_PANEL)
        grow.pack(pady=(2, 2), **pad)
        self.gain_var = tk.StringVar()
        self.gain_entry = self._entry(grow, textvariable=self.gain_var)
        self.gain_entry.pack(side="left", fill="x", expand=True)
        self.gain_entry.bind("<Return>", lambda ev: self.apply_gain())
        self.gain_entry.bind("<FocusOut>", lambda ev: self.apply_gain(quiet=True))
        make_button(grow, "Set", self.apply_gain, self.fonts).pack(side="left",
                                                                  padx=(4, 0))
        self.gain_msg = tk.Label(side, text="not recorded", font=self.fonts.tiny,
                                 fg=T3, bg=BG_PANEL, anchor="w",
                                 wraplength=SIDEBAR_W - 30, justify="left")
        self.gain_msg.pack(pady=(0, 8), **pad)

        self.wb_label = tk.Label(side, text="", font=self.fonts.mono_sm, fg=T2,
                                 bg=BG_PANEL, anchor="w", wraplength=300, justify="left")
        self.wb_label.pack(pady=(0, 10), **pad)

        # ── SEQUENTIAL FILL ──────────────────────────────────────────────────
        h, s = section_header(side, "Sequential fill", self.fonts)
        h.pack(pady=(4, 0), **pad)
        s.pack(pady=(4, 8), **pad)

        row = tk.Frame(side, bg=BG_PANEL)
        row.pack(pady=(0, 6), **pad)
        left = tk.Frame(row, bg=BG_PANEL)
        left.pack(side="left", fill="x", expand=True, padx=(0, 6))
        field_label(left, "START SCAN", self.fonts).pack(anchor="w")
        self.fill_start = self._entry(left, width=10)
        self.fill_start.pack(fill="x")
        rightf = tk.Frame(row, bg=BG_PANEL)
        rightf.pack(side="left")
        field_label(rightf, "STEP", self.fonts).pack(anchor="w")
        self.fill_step = self._entry(rightf, width=5)
        self.fill_step.insert(0, "1")
        self.fill_step.pack()

        field_label(side, "EXTENT", self.fonts).pack(**pad)
        self.fill_mode = tk.StringVar(value="column")
        modes = tk.Frame(side, bg=BG_PANEL)
        modes.pack(pady=(2, 6), **pad)
        for val, text in (("column", "Column"), ("row", "Row"), ("block", "Whole grid")):
            tk.Radiobutton(
                modes, text=text, value=val, variable=self.fill_mode,
                font=self.fonts.ui, fg=T1, bg=BG_PANEL, selectcolor=BG_INPUT,
                activebackground=BG_PANEL, activeforeground=AMBER,
                highlightthickness=0, bd=0, anchor="w",
            ).pack(side="left", padx=(0, 8))

        self.fill_overwrite = tk.BooleanVar(value=False)
        tk.Checkbutton(
            side, text="Overwrite cells that already have a scan",
            variable=self.fill_overwrite, font=self.fonts.tiny, fg=T2, bg=BG_PANEL,
            selectcolor=BG_INPUT, activebackground=BG_PANEL, activeforeground=AMBER,
            highlightthickness=0, bd=0, anchor="w",
        ).pack(anchor="w", padx=12)

        make_button(side, "Fill  (Ctrl+F)", self.do_fill, self.fonts).pack(
            pady=(6, 12), **pad)

        # ── FILES ────────────────────────────────────────────────────────────
        h, s = section_header(side, "Files", self.fonts)
        h.pack(pady=(4, 0), **pad)
        s.pack(pady=(4, 8), **pad)

        # The label carries the left-ellipsised path; the entry below keeps
        # the full, editable value. Putting the ellipsis in the entry itself
        # would mean the widget no longer holds what apply_dirs() reads.
        self.out_dir_label = field_label(side, "OUTPUT DIRECTORY", self.fonts)
        self.out_dir_label.pack(**pad)
        self.out_var = tk.StringVar()
        orow = tk.Frame(side, bg=BG_PANEL)
        orow.pack(pady=(2, 8), **pad)
        e = self._entry(orow, textvariable=self.out_var)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda ev: self.apply_dirs())
        self._out_entry = e
        Tooltip(e, lambda: str(self.cfg.output_dir))
        make_button(orow, "···", self.browse_out, self.fonts).pack(side="left", padx=(4, 0))

        self.dat_dir_label = field_label(side, "DATA DIRECTORY", self.fonts)
        self.dat_dir_label.pack(**pad)
        self.dat_var = tk.StringVar()
        drow = tk.Frame(side, bg=BG_PANEL)
        drow.pack(pady=(2, 8), **pad)
        e = self._entry(drow, textvariable=self.dat_var)
        e.pack(side="left", fill="x", expand=True)
        e.bind("<Return>", lambda ev: self.apply_dirs())
        self._dat_entry = e
        Tooltip(e, lambda: self.cfg.dat_dir or "(not set)")
        make_button(drow, "···", self.browse_dat, self.fonts).pack(side="left", padx=(4, 0))

        make_button(side, "Apply directories", self.apply_dirs, self.fonts).pack(
            pady=(0, 8), **pad)
        make_button(side, "Verify all .dat  (F5)", self.verify_all, self.fonts).pack(
            pady=(0, 6), **pad)
        make_button(side, "Save workbook  (Ctrl+S)", self.save_current, self.fonts,
                    fill=True).pack(pady=(0, 6), **pad)
        make_button(side, "Save every workbook", self.save_all, self.fonts).pack(
            pady=(0, 12), **pad)

        # ── LEGEND ───────────────────────────────────────────────────────────
        h, s = section_header(side, "Cell states", self.fonts)
        h.pack(pady=(4, 0), **pad)
        s.pack(pady=(4, 6), **pad)
        for colour, text in (
            (BORDER, "empty"),
            (GREEN, ".dat present on disk"),
            (AMBER, ".dat not on disk yet"),
            (T3, ".dat check disabled"),
            (RED, "DUPLICATE scan number"),
            (VIOLET, "number in violet = has a note"),
            (AMBER, "left bar = gain changed at this scan"),
            (BORDER_HI, "shaded = a later gain epoch"),
        ):
            r = tk.Frame(side, bg=BG_PANEL)
            r.pack(anchor="w", padx=12, pady=1, fill="x")
            sw = tk.Frame(r, bg=BG_INPUT, width=16, height=11, highlightthickness=2,
                          highlightbackground=colour)
            sw.pack(side="left", padx=(0, 7))
            sw.pack_propagate(False)
            tk.Label(r, text=text, font=self.fonts.tiny, fg=T2, bg=BG_PANEL).pack(side="left")

    def _entry(self, parent, textvariable=None, width=None):
        return tk.Entry(
            parent, textvariable=textvariable, width=width or 12,
            font=self.fonts.mono, fg=T1, bg=BG_INPUT,
            insertbackground=CYAN, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=CYAN,
        )

    # ---- grid ---------------------------------------------------------------
    def _build_grid(self, parent):
        # expand=True: with the log panel gone (embedded mode) the tab would
        # otherwise end in a large patch of raw background. Letting the grid
        # panel absorb the slack keeps the empty space inside a bordered panel,
        # where it reads as room to grow rather than as an unfinished layout.
        panel = tk.Frame(parent, bg=BG_PANEL, highlightthickness=1,
                         highlightbackground=BORDER)
        panel.pack(side="top", fill="both", expand=True)

        top = tk.Frame(panel, bg=BG_PANEL)
        top.pack(fill="x", padx=12, pady=(10, 0))
        h, _ = section_header(top, "Analyser angle / degrees", self.fonts)
        h.pack(side="left")
        self.ctx_label = tk.Label(top, text="", font=self.fonts.mono_sm,
                                  fg=T2, bg=BG_PANEL)
        self.ctx_label.pack(side="right")
        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x", padx=12, pady=(4, 8))

        # The grid sits on a canvas so that a screen too narrow for all the
        # analyser angles scrolls instead of silently cutting columns off.
        # Scrolling follows the keyboard (see focus_cell) -- the mouse is never
        # required to reach a cell.
        holder = tk.Frame(panel, bg=BG_PANEL)
        holder.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self.grid_canvas = tk.Canvas(holder, bg=BG_PANEL, highlightthickness=0, bd=0)
        self.grid_canvas.pack(side="top", fill="both", expand=True)
        self.grid_xbar = tk.Scrollbar(holder, orient="horizontal",
                                      command=self.grid_canvas.xview,
                                      bg=BG_CARD, troughcolor=BG_PANEL,
                                      relief="flat", bd=0, width=9)
        self.grid_canvas.configure(xscrollcommand=self._on_grid_scroll)

        g = tk.Frame(self.grid_canvas, bg=BG_PANEL)
        self._grid_window = self.grid_canvas.create_window(0, 0, window=g, anchor="nw")
        self.grid_frame = g
        g.bind("<Configure>", self._sync_grid_scrollregion)

        angles = self.cfg.angles
        COL0 = 0    # group label
        COL1 = 1    # polarisation label
        CBASE = 2   # first data column

        # The 13 data columns share any surplus width equally, so the grid
        # fills the panel instead of leaving a dead zone on the right.
        for j in range(len(angles)):
            g.grid_columnconfigure(CBASE + j, weight=1, minsize=52)

        # header row: angles. Neutral, not accent: these are labels, not state.
        tk.Label(g, text="", bg=BG_PANEL).grid(row=0, column=COL0)
        for j, a in enumerate(angles):
            tk.Label(g, text=a, font=self.fonts.cell, fg=T2, bg=BG_PANEL,
                     width=5).grid(row=0, column=CBASE + j, padx=1, pady=(0, 1),
                                   sticky="ew")

        # fill-count row -- at a glance, what is still empty in this grid
        tk.Label(g, text="FILLED", font=self.fonts.tiny, fg=T3, bg=BG_PANEL,
                 anchor="e").grid(row=1, column=COL0, columnspan=2, sticky="e", padx=(0, 8))
        for j, a in enumerate(angles):
            lab = tk.Label(g, text="0/4", font=self.fonts.mono_sm, fg=T3, bg=BG_PANEL)
            lab.grid(row=1, column=CBASE + j, padx=1, pady=(0, 4), sticky="ew")
            self.count_labels[a] = lab

        r = 2
        for gi, (group, labels) in enumerate(self.cfg.pol_groups.items()):
            # Group labels are structure, not state -- both neutral.
            gl = tk.Label(g, text=group.upper(), font=self.fonts.sec, fg=T2,
                          bg=BG_PANEL, anchor="e", justify="right", wraplength=118)
            gl.grid(row=r, column=COL0, rowspan=len(labels), sticky="e", padx=(0, 8))
            for lab in labels:
                tk.Label(g, text=lab, font=self.fonts.cell, fg=T1, bg=BG_CARD,
                         width=3).grid(row=r, column=COL1, padx=(0, 5), pady=1, sticky="ns")
                for j, a in enumerate(angles):
                    self._make_cell(g, r, CBASE + j, a, lab)
                r += 1
            if gi == 0:
                tk.Frame(g, bg=BORDER, height=1).grid(
                    row=r, column=COL0, columnspan=CBASE + len(angles),
                    sticky="ew", pady=3)
                r += 1

    def _make_cell(self, parent, row, col, angle, pol):
        key = (angle, pol)
        # A holder + a 2px strip, so a gain-change boundary can be marked on
        # the cell's LEFT EDGE only. An Entry's highlight border is all four
        # sides at once and is already carrying .dat status, so the two states
        # would otherwise overwrite each other.
        holder = tk.Frame(parent, bg=BG_PANEL)
        holder.grid(row=row, column=col, padx=1, pady=2, sticky="ew")
        rule = tk.Frame(holder, bg=BG_PANEL, width=2)
        rule.pack(side="left", fill="y")

        var = tk.StringVar()
        e = tk.Entry(
            holder, textvariable=var, width=6, justify="center",
            font=self.fonts.cell, fg=T1, bg=BG_INPUT,
            insertbackground=CYAN, relief="flat", bd=0,
            highlightthickness=2, highlightbackground=BORDER, highlightcolor=CYAN,
        )
        # ipady: taller cells are easier to hit and read in a dark hutch,
        # and they use some of the height the merged log gave back.
        e.pack(side="left", fill="both", expand=True, ipady=3)

        var.trace_add("write", lambda *_a, k=key: self.on_cell_typed(k))

        def _bind(seq, fn):
            # ISO_Left_Tab and KP_Enter exist on X11 but not on Windows Tk.
            # Bind what the platform has and ignore the rest.
            try:
                e.bind(seq, fn)
            except tk.TclError:
                pass

        _bind("<FocusIn>", lambda ev, k=key: self.on_cell_focus(k))
        _bind("<Tab>", lambda ev, k=key: self.nav(k, +1))
        _bind("<Return>", lambda ev, k=key: self.nav(k, +1))
        _bind("<KP_Enter>", lambda ev, k=key: self.nav(k, +1))
        _bind("<Shift-Tab>", lambda ev, k=key: self.nav(k, -1))
        _bind("<ISO_Left_Tab>", lambda ev, k=key: self.nav(k, -1))
        _bind("<Down>", lambda ev, k=key: self.step(k, 0, +1))
        _bind("<Up>", lambda ev, k=key: self.step(k, 0, -1))
        _bind("<Control-Right>", lambda ev, k=key: self.step(k, +1, 0))
        _bind("<Control-Left>", lambda ev, k=key: self.step(k, -1, 0))
        _bind("<Control-n>", lambda ev, k=key: self.focus_note())
        _bind("<Prior>", lambda ev, k=key: self.step(k, +1, 0))     # PageUp
        _bind("<Next>", lambda ev, k=key: self.step(k, -1, 0))      # PageDown

        self.cells[key] = e
        self.cell_vars[key] = var
        self.cell_holders[key] = holder
        self.cell_rules[key] = rule

    # ---- grid scrolling -----------------------------------------------------
    def _sync_grid_scrollregion(self, _event=None):
        """
        Keep the grid as wide as the canvas whenever there is room to spare, so
        the 13 angle columns share out the surplus instead of huddling on the
        left and leaving a dead zone on the right. Only when the canvas is
        genuinely narrower than the grid's minimum does it become scrollable.
        """
        c, g = self.grid_canvas, self.grid_frame
        need_w, h = g.winfo_reqwidth(), g.winfo_reqheight()
        have_w = c.winfo_width()
        width = max(need_w, have_w)
        c.itemconfigure(self._grid_window, width=width)
        c.configure(scrollregion=(0, 0, width, h))
        self._grid_needs_scroll = need_w > have_w + 2

    def _on_grid_scroll(self, lo, hi):
        """Show the horizontal scrollbar only when the grid does not fit."""
        self.grid_xbar.set(lo, hi)
        if float(lo) <= 0.0 and float(hi) >= 1.0:
            self.grid_xbar.pack_forget()
        elif not self.grid_xbar.winfo_ismapped():
            self.grid_xbar.pack(side="bottom", fill="x", pady=(3, 0))

    def _scroll_into_view(self, widget):
        """Keep the focused cell on screen without touching the mouse."""
        c = self.grid_canvas
        total = self.grid_frame.winfo_reqwidth()
        view = c.winfo_width()
        if total <= view or total <= 0:
            return
        x0 = widget.winfo_x()
        x1 = x0 + widget.winfo_width()
        left = c.canvasx(0)
        if x0 < left + 8:
            c.xview_moveto(max(0.0, (x0 - 8) / total))
        elif x1 > left + view - 8:
            c.xview_moveto(max(0.0, (x1 - view + 8) / total))

    # ---- note bar -----------------------------------------------------------
    def _build_notebar(self, parent):
        bar = tk.Frame(parent, bg=BG_PANEL, highlightthickness=1,
                       highlightbackground=BORDER)
        bar.pack(side="top", fill="x", pady=(8, 0))
        inner = tk.Frame(bar, bg=BG_PANEL)
        inner.pack(fill="x", padx=12, pady=8)

        self.note_label = tk.Label(inner, text="NOTE  —  no cell selected",
                                   font=self.fonts.sec, fg=T2, bg=BG_PANEL,
                                   width=32, anchor="w")
        self.note_label.pack(side="left", padx=(0, 10))

        self.note_var = tk.StringVar()
        self.note_entry = tk.Entry(
            inner, textvariable=self.note_var, font=self.fonts.ui,
            fg=T1, bg=BG_INPUT, insertbackground=CYAN, relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=CYAN,
        )
        self.note_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.note_var.trace_add("write", lambda *_a: self.on_note_typed())
        self.note_entry.bind("<Return>", lambda ev: self.leave_note())
        self.note_entry.bind("<Escape>", lambda ev: self.leave_note())

        tk.Label(inner, text="Ctrl+N", font=self.fonts.mono_sm, fg=T3,
                 bg=BG_PANEL).pack(side="left", padx=(8, 0))

    # ---- log ----------------------------------------------------------------
    def _build_log(self, parent):
        """
        The log panel. Skipped entirely when a host supplied a log_sink: two
        scrolling log panels stacked in one window is duplicated chrome, and
        the reclaimed height goes to the content above.

        The keyboard cheat-sheet stays either way -- it belongs to this tab.
        """
        keys = ("Tab / Enter  next channel   ·   Ctrl+←/→  next angle   ·   "
                "Ctrl+N  note   ·   Ctrl+F  fill   ·   F5  verify   ·   Ctrl+S  save")
        if self._log_sink is not None:
            bar = tk.Frame(parent, bg=BG_BASE)
            bar.pack(side="top", fill="x", pady=(8, 0))
            tk.Label(bar, text=keys, font=self.fonts.tiny, fg=T3,
                     bg=BG_BASE, anchor="e").pack(side="right")
            self.log_text = None
            return

        panel = tk.Frame(parent, bg=BG_PANEL, highlightthickness=1,
                         highlightbackground=BORDER)
        panel.pack(side="top", fill="both", expand=True, pady=(8, 0))

        top = tk.Frame(panel, bg=BG_PANEL)
        top.pack(fill="x", padx=12, pady=(8, 0))
        h, _ = section_header(top, "Log", self.fonts)
        h.pack(side="left")
        tk.Label(top, text=keys, font=self.fonts.tiny, fg=T3,
                 bg=BG_PANEL).pack(side="right")
        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x", padx=12, pady=(4, 6))

        wrap = tk.Frame(panel, bg=BG_CARD)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self.log_text = tk.Text(
            wrap, height=8, bg=BG_CARD, fg=T2, font=self.fonts.mono_sm,
            relief="flat", bd=0, highlightthickness=0, wrap="none",
            insertbackground=CYAN, state="disabled",
        )
        self.log_text.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(wrap, command=self.log_text.yview, bg=BG_CARD,
                          troughcolor=BG_PANEL, relief="flat", bd=0, width=8)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set)
        for name, colour in (("cyan", CYAN), ("green", GREEN), ("amber", AMBER),
                             ("red", RED), ("dim", T3)):
            self.log_text.tag_configure(name, foreground=colour)

    def log(self, msg, colour=None):
        if self._log_sink is not None:
            self._log_sink(msg, colour)
            return
        tag = {CYAN: "cyan", GREEN: "green", AMBER: "amber",
               RED: "red", T3: "dim"}.get(colour, "")
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp}  {msg}\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # =========================================================================
    # SHORTCUTS  --  installed on the toplevel, guarded so an embedded host
    # only sees them while this frame is the visible one.
    # =========================================================================
    def install_shortcuts(self, widget=None):
        w = widget or self.winfo_toplevel()
        w.bind("<F5>", lambda e: self.verify_all())
        w.bind("<Control-s>", lambda e: self.save_current())
        w.bind("<Control-f>", lambda e: self.do_fill())
        w.bind("<Alt-Left>", lambda e: self.cycle_peak(-1))
        w.bind("<Alt-Right>", lambda e: self.cycle_peak(+1))
        w.bind("<Alt-Up>", lambda e: self.cycle_temp(-1))
        w.bind("<Alt-Down>", lambda e: self.cycle_temp(+1))

    # =========================================================================
    # GRID <-> SESSION
    # =========================================================================
    def load_grid(self):
        """Repaint every cell from the session for the current peak + temperature."""
        peak, temp = self.session.ui_peak, self.session.ui_temp
        cells = self.session.cells_for(peak, temp)
        self._suspend_trace = True
        try:
            for key, entry_widget in self.cells.items():
                e = cells.get(key)
                self.cell_vars[key].set(str(e.scan) if (e and e.scan >= 0) else "")
                # VIOLET, not the focus accent: has-note is its own state.
                entry_widget.configure(fg=VIOLET if (e and e.note) else T1)
        finally:
            self._suspend_trace = False
        self.refresh_visuals()
        self.ctx_label.configure(text=f"{peak}   ·   {temp} K")

    def refresh_visuals(self):
        """Recolour every cell and recompute the header readouts."""
        peak, temp = self.session.ui_peak, self.session.ui_temp
        dupes = self.session.duplicates()
        dup_keys = {k for keys in dupes.values() for k in keys}
        counts = self.session.fill_counts(peak, temp)
        enabled = self.session.checker.enabled

        # Gain state for this grid. Epochs are resolved across the whole
        # session because the amplifier setting propagates by scan number and
        # does not reset at a workbook boundary.
        session_eps = self.session.session_epochs()
        bounds = boundary_scans(session_eps)
        grid_eps = self.session.epochs_for(peak, temp)
        epoch_rank = {}
        for i, ep in enumerate(grid_eps):
            epoch_rank[(ep.first_scan, ep.gain)] = i

        def _rank(scan):
            match = None
            for ep in grid_eps:
                if ep.first_scan <= scan:
                    match = ep
                else:
                    break
            if match is None:
                return 0
            return epoch_rank.get((match.first_scan, match.gain), 0)

        for (angle, pol), widget in self.cells.items():
            full_key = (peak, temp, angle, pol)
            entry = self.session.get(full_key)
            has_scan = entry is not None and entry.scan >= 0
            # Status only. "logged but unchecked" is the ABSENCE of
            # information, so it is neutral grey -- CYAN is reserved for focus
            # and would read as a fourth status here.
            # Epoch banding: a shade step per gain epoch after the first.
            band = BG_INPUT
            if has_scan and len(grid_eps) > 1:
                band = GAIN_TINTS[min(_rank(entry.scan), len(GAIN_TINTS) - 1)]

            if not has_scan:
                border, bg = BORDER, BG_INPUT
            elif full_key in dup_keys:
                border, bg = RED, BG_DUP
            elif not enabled or entry.dat_found == DAT_FOUND_UNKNOWN:
                border, bg = T3, band
            elif entry.dat_found == DAT_FOUND_YES:
                border, bg = GREEN, band
            else:
                border, bg = AMBER, band
            widget.configure(highlightbackground=border, bg=bg,
                             fg=VIOLET if (entry and entry.note) else T1)

            # Left rule: this scan is where the gain changed.
            rule = self.cell_rules[(angle, pol)]
            rule.configure(bg=AMBER if (has_scan and entry.scan in bounds)
                           else BG_PANEL)

        n_pol = len(self.cfg.pol_labels)
        for angle, lab in self.count_labels.items():
            n = counts.get(angle, 0)
            lab.configure(text=f"{n}/{n_pol}",
                          fg=GREEN if n == n_pol else (T3 if n == 0 else AMBER))

        # Only a value that means something is coloured. A row of four
        # permanently-lit numbers is decoration; one amber count against three
        # grey ones is information.
        self.readouts["scans"].configure(text=str(self.session.total_scans()),
                                         fg=T1)
        empty = self.session.empty_count(peak, temp)
        self.readouts["empty"].configure(text=str(empty),
                                         fg=GREEN if empty == 0 else T1)
        n_dup = sum(len(v) for v in dupes.values())
        self.readouts["dupes"].configure(text=str(len(dupes)),
                                         fg=T3 if not dupes else RED)
        missing = len(self.session.missing_dat())
        self.readouts["missing"].configure(text=str(missing) if enabled else "—",
                                           fg=T3 if not missing else AMBER)

        # ---- MIXED GAIN --------------------------------------------------
        # The operator's early warning that the tau-series they are building
        # is not in consistent units. 1 is fine; anything else is not.
        n_known = sum(1 for ep in grid_eps if ep.known)
        n_unknown = self.session.unknown_gain_count(peak, temp)
        if not grid_eps:
            self.readouts["gain"].configure(text="—", fg=T3)
        elif n_unknown and n_known:
            self.readouts["gain"].configure(text=f"{n_known}+?", fg=AMBER)
        elif n_unknown:
            self.readouts["gain"].configure(text="?", fg=AMBER)
        elif n_known > 1:
            self.readouts["gain"].configure(text=str(n_known), fg=AMBER)
        else:
            self.readouts["gain"].configure(text="1", fg=T1)

        if not grid_eps:
            self.epoch_label.configure(text="—", fg=T3)
        else:
            lines = "     ".join(ep.label for ep in grid_eps)
            mixed = n_known > 1 or n_unknown > 0
            self.epoch_label.configure(text=lines, fg=AMBER if mixed else T2)

        name = self.session.workbook_name(peak, temp)
        self.wb_label.configure(text=name or "(grid empty — nothing to write yet)")
        return dupes, n_dup

    # =========================================================================
    # GAIN
    # =========================================================================
    def apply_gain(self, quiet=False):
        """
        Read the gain box. Rejects unparseable input rather than coercing it:
        a wrong gain is worse than no gain, because "no gain" is flagged as
        unknown everywhere downstream and a wrong one silently is not.
        """
        text = self.gain_var.get().strip()
        try:
            gain = parse_gain(text)
        except ValueError as exc:
            self.gain_msg.configure(text=str(exc), fg=RED)
            if not quiet:
                self.log(f"Gain: {exc}", RED)
            return "break"

        old = self.current_gain
        self.current_gain = gain
        if gain is None:
            self.gain_msg.configure(
                text="not recorded — scans logged now will have UNKNOWN gain",
                fg=AMBER)
        else:
            self.gain_msg.configure(
                text=f"{fmt_gain(gain)} V/A applies to every scan logged from now",
                fg=T2)
        if not quiet and gain != old:
            self.log(f"Gain set to {fmt_gain(gain) or 'UNKNOWN'} V/A"
                     + (f" (was {fmt_gain(old)})" if old is not None else ""),
                     AMBER if old is not None else None)
        self.refresh_visuals()
        return "break"

    def _gain_change_note(self, old, new) -> str:
        """
        Machine-readable prefix so the reason survives in the human record too.
        Prepended, never overwriting whatever the operator already wrote.
        """
        return f"GAIN {fmt_gain(old) or 'unknown'} -> {fmt_gain(new)};"

    # =========================================================================
    # EDIT HANDLERS
    # =========================================================================
    def on_cell_typed(self, key):
        if self._suspend_trace:
            return
        angle, pol = key
        text = self.cell_vars[key].get()

        # Silently drop anything that is not a scan number rather than beeping
        # at someone who is typing fast in a dark hutch.
        cleaned = "".join(ch for ch in text if ch.isdigit())
        if cleaned != text:
            self._suspend_trace = True
            self.cell_vars[key].set(cleaned)
            self._suspend_trace = False
            text = cleaned

        full_key = self.session.key(angle, pol)
        # What was in force for this scan number BEFORE this edit -- used to
        # decide whether the operator has just crossed a gain boundary.
        gain_before = self.session.gain_of(int(text)) if text.isdigit() else None
        try:
            self.session.set_scan(full_key, text, gain=self.current_gain)
        except ValueError:
            return

        # A scan logged at a gain different from the one already in force at
        # that point in the sequence IS the boundary. Mark it in the note as
        # well as in the column, so the reason survives in the human record.
        if (text and self.current_gain is not None
                and gain_before is not None
                and gain_before != self.current_gain):
            entry = self.session.get(full_key)
            if entry is not None:
                prefix = self._gain_change_note(gain_before, self.current_gain)
                if not entry.note.startswith("GAIN "):
                    entry.note = (prefix + " " + entry.note).strip()
                    if self._note_bound == key:
                        self._suspend_trace = True
                        self.note_var.set(entry.note)
                        self._suspend_trace = False
                self.log(f"Gain change at scan {text}: "
                         f"{fmt_gain(gain_before)} -> {fmt_gain(self.current_gain)} V/A. "
                         f"This scan and every later one are in new units.", AMBER)

        self.autosave()
        _, n_dup = self.refresh_visuals()

        if text:
            dupes = self.session.duplicates()
            scan = int(text)
            if scan in dupes:
                where = ", ".join(
                    f"{k[0]} {k[1]}K {k[2]}° {k[3]}" for k in dupes[scan])
                self.log(f"DUPLICATE  scan {scan} appears in {len(dupes[scan])} cells: {where}",
                         RED)

    def on_cell_focus(self, key):
        self._focus_cell = key
        self.bind_note(key)

    def bind_note(self, key):
        angle, pol = key
        self._note_bound = key
        entry = self.session.get(self.session.key(angle, pol))
        self._suspend_trace = True
        self.note_var.set(entry.note if entry else "")
        self._suspend_trace = False
        self.note_label.configure(
            text=f"NOTE  —  {self.session.ui_peak} {self.session.ui_temp}K  {angle}°  {pol}")

    def on_note_typed(self):
        if self._suspend_trace or self._note_bound is None:
            return
        angle, pol = self._note_bound
        self.session.set_note(self.session.key(angle, pol), self.note_var.get())
        self.cells[self._note_bound].configure(
            fg=VIOLET if self.note_var.get().strip() else T1)
        self.autosave()

    def focus_note(self):
        if self._focus_cell is not None:
            self.note_entry.focus_set()
            self.note_entry.icursor("end")
        return "break"

    def leave_note(self):
        if self._focus_cell is not None:
            self.cells[self._focus_cell].focus_set()
            self.cells[self._focus_cell].icursor("end")
        return "break"

    # =========================================================================
    # NAVIGATION
    # =========================================================================
    def _order(self):
        """
        Cell order for Tab: down the four polarisation channels at one angle,
        then on to the next angle. That is the order the measurement is
        actually taken in, so Tab follows the hand.
        """
        return [(a, p) for a in self.cfg.angles for p in self.cfg.pol_labels]

    def nav(self, key, delta):
        order = self._order()
        i = (order.index(key) + delta) % len(order)
        self.focus_cell(order[i])
        return "break"

    def step(self, key, dcol, drow):
        angles, pols = self.cfg.angles, self.cfg.pol_labels
        a, p = key
        j = (angles.index(a) + dcol) % len(angles)
        i = (pols.index(p) + drow) % len(pols)
        self.focus_cell((angles[j], pols[i]))
        return "break"

    def focus_cell(self, key):
        w = self.cells[key]
        w.focus_set()
        w.icursor("end")
        w.select_range(0, "end")
        # Scroll on the HOLDER: the Entry's x is now relative to its holder,
        # not to the grid, so using it would scroll to the wrong place.
        self._scroll_into_view(self.cell_holders[key])

    def focus_first_empty(self):
        for key in self._order():
            if not self.cell_vars[key].get():
                self.focus_cell(key)
                return
        self.focus_cell(self._order()[0])

    def cycle_peak(self, delta):
        peaks = list(self.cfg.peaks)
        i = (peaks.index(self.session.ui_peak) + delta) % len(peaks)
        self.peak_var.set(peaks[i])
        self.on_context_change()
        return "break"

    def cycle_temp(self, delta):
        temps = list(self.cfg.temperatures)
        i = (temps.index(self.session.ui_temp) + delta) % len(temps)
        self.temp_var.set(temps[i])
        self.on_context_change()
        return "break"

    def on_context_change(self):
        """
        Peak or temperature changed. Write out the workbook we are leaving so
        its filename can never drift out of date, then repaint.
        """
        old_peak, old_temp = self.session.ui_peak, self.session.ui_temp
        new_peak, new_temp = self.peak_var.get(), self.temp_var.get()
        if (old_peak, old_temp) == (new_peak, new_temp):
            return
        if self.session.scans_for(old_peak, old_temp):
            self._export(old_peak, old_temp, quiet=True)
        self.session.ui_peak = new_peak
        self.session.ui_temp = new_temp
        self.session.save_session()
        self.load_grid()
        self.log(f"→ {new_peak}  {new_temp} K", CYAN)
        self.focus_first_empty()

    # =========================================================================
    # AUTOSAVE
    # =========================================================================
    def autosave(self):
        """
        Session JSON is written synchronously on every single keystroke -- that
        is the crash guarantee. The CSV is regenerated on a short debounce
        because it is derived and rewriting it per keystroke is wasted I/O.
        """
        try:
            self.session.save_session()
        except OSError as exc:
            self.log(f"AUTOSAVE FAILED: {exc}", RED)
            return
        if self._csv_job is not None:
            try:
                self.after_cancel(self._csv_job)
            except (tk.TclError, ValueError):
                pass
        self._csv_job = self.after(400, self._write_csv_now)

    def _write_csv_now(self):
        self._csv_job = None
        try:
            self.session.write_csv()
        except OSError as exc:
            self.log(f"CSV WRITE FAILED: {exc}", RED)

    def flush(self):
        """Force everything to disk. Called on close and before any export."""
        if self._csv_job is not None:
            try:
                self.after_cancel(self._csv_job)
            except (tk.TclError, ValueError):
                pass
            self._csv_job = None
        try:
            self.session.save_session()
            self.session.write_csv()
        except OSError as exc:
            self.log(f"FLUSH FAILED: {exc}", RED)

    # =========================================================================
    # ACTIONS
    # =========================================================================
    def do_fill(self):
        """
        Sequential fill. Partial grids are normal, so this never overwrites an
        existing scan number unless explicitly asked, and everything it writes
        stays editable afterwards.
        """
        text = self.fill_start.get().strip()
        if not text.isdigit():
            self.log("Sequential fill: enter a starting scan number first.", AMBER)
            self.fill_start.focus_set()
            return "break"
        try:
            step = int(self.fill_step.get().strip() or "1")
        except ValueError:
            step = 1

        mode = self.fill_mode.get()
        angle = pol = None
        if self._focus_cell is not None:
            angle, pol = self._focus_cell
        elif mode in ("column", "row"):
            self.log("Sequential fill: click or Tab into a cell first so I know "
                     "which column / row you mean.", AMBER)
            return "break"

        plan = self.session.plan_sequential(
            self.session.ui_peak, self.session.ui_temp, int(text), mode,
            angle=angle, pol=pol, step=step,
            overwrite=self.fill_overwrite.get(),
        )
        if not plan:
            self.log("Sequential fill: nothing to fill (all target cells already have "
                     "a scan — tick Overwrite to replace them).", AMBER)
            return "break"

        n = self.session.apply_sequential(plan)
        self.flush()
        self.load_grid()
        first, last = plan[0][1], plan[-1][1]
        where = {"column": f"angle {angle}°", "row": f"channel {pol}",
                 "block": "whole grid"}[mode]
        self.log(f"Sequential fill: {n} cells across {where}, {first} → {last} "
                 f"(step {step}). All still editable.", AMBER)
        dupes = self.session.duplicates()
        if dupes:
            self.log(f"WARNING  {len(dupes)} duplicate scan number(s) after fill.", RED)
        self.focus_first_empty()
        return "break"

    def verify_all(self):
        """Re-check every logged scan against the data directory. Never blocks."""
        if not self.session.checker.enabled:
            self.session.verify_all()
            self.refresh_visuals()
            self.log("Verify: data directory is not set or not reachable — "
                     ".dat check stays disabled. Everything else works normally.", CYAN)
            return "break"

        checked, missing, _ = self.session.verify_all()
        self.flush()
        self.refresh_visuals()
        if checked == 0:
            self.log("Verify: no scans logged yet.", CYAN)
        elif not missing:
            self.log(f"Verify: all {checked} logged scans found on disk.", GREEN)
        else:
            self.log(f"Verify: {checked} scans checked, {len(missing)} still missing:",
                     AMBER)
            for k, e in missing[:25]:
                self.log(f"    {e.scan}   {k[0]}  {k[1]}K  {k[2]}°  {k[3]}", AMBER)
            if len(missing) > 25:
                self.log(f"    … and {len(missing) - 25} more.", AMBER)
        return "break"

    def _export(self, peak, temp, quiet=False):
        try:
            path, removed = self.session.export_xlsx(peak, temp)
        except Exception as exc:                      # noqa: BLE001 - never die mid-beamtime
            self.log(f"EXPORT FAILED for {peak} {temp}K: {exc}", RED)
            return None
        if path is None:
            if not quiet:
                self.log(f"{peak} {temp}K: grid is empty, nothing written.", CYAN)
            return None
        if removed:
            self.log(f"Saved {path.name}   (renamed from {', '.join(removed)})", GREEN)
        elif not quiet:
            self.log(f"Saved {path.name}", GREEN)
        else:
            self.log(f"Saved {path.name}", T3)
        return path

    def save_current(self):
        """
        Explicit save. Partial grids save without complaint -- that is the
        normal case, not an error.
        """
        self.flush()
        peak, temp = self.session.ui_peak, self.session.ui_temp
        filled = len(self.session.scans_for(peak, temp))
        total = len(self.cfg.angles) * len(self.cfg.pol_labels)
        self._export(peak, temp)
        self.log(f"    {filled}/{total} cells filled   ·   CSV: "
                 f"{self.session.csv_path.name} ({self.session.total_scans()} rows)", T3)
        self.refresh_visuals()
        return "break"

    def save_all(self):
        self.flush()
        results = self.session.export_all_xlsx()
        if not results:
            self.log("Nothing to save yet.", CYAN)
        else:
            for path, removed in results:
                if removed:
                    self.log(f"Saved {path.name}   (renamed from {', '.join(removed)})", GREEN)
                else:
                    self.log(f"Saved {path.name}", GREEN)
            self.log(f"{len(results)} workbook(s) written to {self.session.output_dir}",
                     GREEN)
        return "break"

    # ---- directories --------------------------------------------------------
    def browse_out(self):
        d = filedialog.askdirectory(title="Output directory",
                                    initialdir=str(self.cfg.output_dir))
        if d:
            self.out_var.set(d)
            self.apply_dirs()

    def browse_dat(self):
        d = filedialog.askdirectory(title="Data (.dat) directory",
                                    initialdir=self.cfg.dat_dir or str(Path.home()))
        if d:
            self.dat_var.set(d)
            self.apply_dirs()

    def apply_dirs(self):
        out = self.out_var.get().strip()
        dat = self.dat_var.get().strip()
        old_out = str(self.cfg.output_dir)
        self.session.rebind_paths(output_dir=out or None, dat_dir=dat)
        if str(self.cfg.output_dir) != old_out:
            self.log(f"Output directory → {self.cfg.output_dir}", CYAN)
            self.flush()
        self._report_dat_dir()
        self.verify_all()
        return "break"

    @staticmethod
    def _short_path(path, keep=2):
        # Ellipsise from the left: the tail identifies the folder, the prefix
        # is the part you already know. Full path is in the tooltip.
        return shorten_path(path, keep)

    def _report_dat_dir(self):
        if not self.cfg.dat_dir:
            self.dat_state.configure(text=".dat check  DISABLED\n(no data directory set)",
                                     fg=T3)
            self.log(".dat check disabled — no data directory set.", T3)
        elif not self.session.checker.enabled:
            self.dat_state.configure(
                text=".dat check  DISABLED\n(directory not reachable)", fg=AMBER)
            self.log(f".dat check disabled — {self.cfg.dat_dir!r} is not a reachable "
                     f"directory. Everything else works normally.", AMBER)
        else:
            self.dat_state.configure(
                text=f".dat check  ON\n{self._short_path(self.cfg.dat_dir)}", fg=GREEN)
            self.log(f".dat check on — {self.cfg.dat_dir}", GREEN)
        # Long paths: the label carries a left-ellipsised form, the entry is
        # scrolled to its tail, and both have the full path on hover.
        self.out_dir_label.configure(
            text=f"OUTPUT DIRECTORY   {shorten_path(self.cfg.output_dir, 2)}")
        self.dat_dir_label.configure(
            text=f"DATA DIRECTORY   {shorten_path(self.cfg.dat_dir, 2)}"
                 if self.cfg.dat_dir else "DATA DIRECTORY   (not set)")
        for e in (self._out_entry, self._dat_entry):
            e.xview_moveto(1.0)

    def _warn_orphans(self):
        """
        Entries in the restored session that the current config no longer
        describes -- e.g. after the temperature list was edited. They are kept
        (nothing is ever silently dropped) but they will not be visible in the
        grid, so say so out loud.
        """
        known_t = set(self.cfg.temperatures)
        known_p = set(self.cfg.peaks)
        known_a = set(self.cfg.angles)
        known_c = set(self.cfg.pol_labels)
        orphans = [k for k in self.session.entries
                   if k[0] not in known_p or k[1] not in known_t
                   or k[2] not in known_a or k[3] not in known_c]
        if orphans:
            shown = ", ".join(f"{k[0]} {k[1]}K {k[2]}° {k[3]}" for k in orphans[:6])
            self.log(f"NOTE  {len(orphans)} logged scan(s) no longer match the config "
                     f"(peak/temperature/angle changed). They are kept in the session "
                     f"and the CSV but are not shown in the grid: {shown}"
                     + (" …" if len(orphans) > 6 else ""), AMBER)

    # ---- geometry -----------------------------------------------------------
    def preferred_size(self):
        """
        Window size that shows the whole grid, clamped to what the screen can
        actually display. Beamtime laptops are not always wide, and the number
        of analyser angles comes from the config, so this cannot be a constant.
        """
        self.update_idletasks()
        # The grid lives on a canvas, so the frame's own reqwidth does not
        # include it -- add the grid's requested width explicitly.
        chrome = SIDEBAR_W + 62
        w = max(chrome + self.grid_frame.winfo_reqwidth(), 1000)
        h = max(self.winfo_reqheight() + 200, 700)
        sw = self.winfo_screenwidth() - 16
        sh = self.winfo_screenheight() - 80
        return min(w, sw), min(h, sh)

    # ---- shutdown -----------------------------------------------------------
    def on_close(self):
        """
        Last chance to get everything on disk. Nothing here may raise: a window
        that refuses to close at 4 a.m. is worse than a workbook that has to be
        re-saved, and the session JSON is written on every keystroke anyway.
        """
        try:
            self.flush()
        except Exception:                             # noqa: BLE001
            traceback.print_exc()
        try:
            self.session.export_all_xlsx()
        except Exception:                             # noqa: BLE001
            traceback.print_exc()


# =============================================================================
# STANDALONE LAUNCHER
# =============================================================================

def main():
    print(f"RSXD Scan Logger {VERSION}")
    # Ask Windows for per-monitor DPI awareness before Tk sizes anything,
    # otherwise a 150 % display gives a blurry bitmap-stretched window.
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    root.title("RSXD Scan Logger  —  I10 / RASOR")
    root.configure(bg=BG_BASE)
    root.minsize(900, 620)

    try:
        app = RSXDLoggerFrame(root)
    except Exception as exc:                          # noqa: BLE001
        traceback.print_exc()
        from tkinter import messagebox
        messagebox.showerror(
            "RSXD Scan Logger — startup failed",
            f"{exc}\n\nThis is almost always a problem in rsxd_config.toml.\n"
            f"Config: {CONFIG_PATH}",
        )
        raise SystemExit(1)

    app.pack(fill="both", expand=True)
    app.install_shortcuts(root)
    w, h = app.preferred_size()
    root.geometry(f"{w}x{h}")

    def _close():
        try:
            app.on_close()
        finally:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", _close)
    root.mainloop()


if __name__ == "__main__":
    main()
