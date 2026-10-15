# =============================================================================
# PANDA  --  GUI tabs           (phase 2 of the RSXD logger)
# Diamond Light Source, beamline I10 / RASOR
# =============================================================================
# Four tabs, one per analysis stage. Presentation and event handling only --
# every number that appears here is computed in rsxd_analysis.py, which imports
# no GUI code at all. Same split as phase 1 (rsxd_core / rsxd_logger).
#
# Visual language is imported wholesale from rsxd_logger: the same theme
# constants, the same section_header, the same make_button, the same monospaced
# numerics. PANDA defines no INTERFACE colour of its own.
#
# It does define a DATA palette (see DATA_SEQ / DATA_CAT below), and that
# separation is the point: chrome is grey with one accent, data is drawn from a
# perceptually uniform CVD-safe map. If the two shared a palette you could not
# tell a selected control from a plotted series.
#
# Local tkinter + matplotlib. No web server, no remote access, no auth.
# =============================================================================

from __future__ import annotations

import math
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import tkinter as tk
from tkinter import filedialog, ttk

import numpy as np

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

import rsxd_analysis as A
from rsxd_core import Config
from rsxd_core import fmt_gain
from rsxd_logger import (
    AMBER, AMBER_DIM, BG_BASE, BG_CARD, BG_INPUT, BG_PANEL, BORDER, BORDER_HI,
    CYAN, GREEN, RED, T1, T2, T3, Fonts, field_label, make_button,
    section_header,
)

VERSION = "v2026-08-08-gain-tracking"

SIDE_W = 330                     # PANDA's control column


# =============================================================================
# MATPLOTLIB THEME  --  same palette as the tkinter side
# =============================================================================

def _style_axes(ax, xlabel="", ylabel="", title=""):
    """
    Chrome recedes so the data reads. Spines and grid sit barely above the
    panel; only the plotted series and the axis title carry any weight.
    """
    ax.set_facecolor(BG_CARD)
    for spine in ax.spines.values():
        spine.set_color(BORDER)
    ax.tick_params(colors=T2, labelsize=8, which="both")
    ax.grid(True, color=BORDER, linewidth=0.6, alpha=0.5)
    ax.set_axisbelow(True)
    if xlabel:
        ax.set_xlabel(xlabel, color=T2, fontsize=9)
    if ylabel:
        ax.set_ylabel(ylabel, color=T2, fontsize=9)
    if title:
        ax.set_title(title, color=T1, fontsize=10, loc="left")


class PlotPanel(tk.Frame):
    """An embedded matplotlib figure, themed and with a styled toolbar."""

    def __init__(self, parent, height=3.2, toolbar=True):
        super().__init__(parent, bg=BG_PANEL, highlightthickness=1,
                         highlightbackground=BORDER)
        self.figure = Figure(figsize=(6.0, height), dpi=100,
                             facecolor=BG_CARD, constrained_layout=True)
        self.canvas = FigureCanvasTkAgg(self.figure, master=self)
        widget = self.canvas.get_tk_widget()
        widget.configure(bg=BG_CARD, highlightthickness=0)
        widget.pack(side="top", fill="both", expand=True)
        if toolbar:
            bar = NavigationToolbar2Tk(self.canvas, self, pack_toolbar=False)
            bar.configure(bg=BG_PANEL, highlightthickness=0, bd=0)
            for child in bar.winfo_children():
                try:
                    child.configure(bg=BG_PANEL, fg=T2, highlightthickness=0, bd=0)
                except tk.TclError:
                    pass
            bar.update()
            bar.pack(side="bottom", fill="x")
        self.clear()

    def clear(self, message="", ylabel="", xlabel=""):
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        _style_axes(ax, xlabel=xlabel, ylabel=ylabel)
        if message:
            ax.text(0.5, 0.5, message, transform=ax.transAxes, ha="center",
                    va="center", color=T3, fontsize=10)
            ax.set_xticks([])
            ax.set_yticks([])
        self.canvas.draw_idle()
        return ax

    def axes(self, **kw):
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        _style_axes(ax, **kw)
        return ax

    def draw(self):
        self.canvas.draw_idle()

    def legend(self, ax, **kw):
        leg = ax.legend(facecolor=BG_CARD, edgecolor=BORDER, labelcolor=T2,
                        fontsize=8, framealpha=0.92, **kw)
        return leg


# =============================================================================
# DATA COLOURS  --  deliberately NOT the UI tokens
# =============================================================================
# Data must never be drawn in the interface accent: if the plotted series and
# the "this control is focused" colour are the same blue, the eye cannot tell
# chrome from measurement. These come from batlow (Crameri), a perceptually
# uniform, colour-vision-deficiency-safe sequential map -- the same family used
# for scientific figures elsewhere in the project.
#
# tau is an ORDERED variable (-90 -> +90 deg), so it gets a sequential ramp,
# not a categorical set: adjacent analyser angles should look adjacent.
DATA_SEQ = ["#011959", "#12496b", "#3c6d56", "#7f8b2f", "#d69c48", "#faccfa"]

# Categorical series (Stage 4 groups). Okabe-Ito, the standard CVD-safe set.
DATA_CAT = ["#0072b2", "#e69f00", "#009e73", "#cc79a7",
            "#56b4e9", "#d55e00", "#f0e442"]

# Single-series marker/line colour, used where there is only one thing plotted.
DATA_PRIMARY = "#e69f00"       # Okabe-Ito orange: the measured points
# The fitted curve deliberately avoids the blue end of the set: #56b4e9 sits
# only a few degrees of hue from the UI accent (#4da3ff), and a fit line that
# looks like a focused control defeats the whole chrome/data separation.
DATA_FIT = "#cc79a7"           # Okabe-Ito reddish purple


def tau_colour(tau, lo=-90.0, hi=90.0):
    """Position along the CVD-safe sequential ramp for one analyser angle."""
    import matplotlib.colors as mcolors
    cmap = mcolors.LinearSegmentedColormap.from_list("panda_seq", DATA_SEQ)
    t = 0.0 if hi == lo else (float(tau) - lo) / (hi - lo)
    return cmap(min(max(t, 0.0), 1.0))


# =============================================================================
# SHARED STATE
# =============================================================================

@dataclass
class PandaState:
    """
    Everything the four tabs pass between them.

    Stage 3b consumes Stage 3a's output rather than recomputing it -- that is
    the whole point of the design, because it makes 3a a direct ground-truth
    check on 3b. So the integrals live here, computed once by the tau-curve
    tab and read by the Stokes tab.
    """
    logger_cfg: Config
    panda_cfg: A.PandaConfig
    log: Callable[[str, Optional[str]], None]

    series_index: List[A.Series] = field(default_factory=list)
    series: Optional[A.Series] = None
    polarisation: str = ""

    reduced: List[A.ReducedScan] = field(default_factory=list)
    manual_files: List[Path] = field(default_factory=list)

    taus: List[float] = field(default_factory=list)
    integrals: List[A.PeakIntegral] = field(default_factory=list)
    window: Optional[Tuple[float, float]] = None
    method: str = "trapezoid"

    fit: Optional[A.StokesFit] = None
    comparison: List[A.SeriesSummary] = field(default_factory=list)
    # Gain state of whatever is currently loaded. Carried on the state object
    # because Stage 3b inherits Stage 3a's verdict rather than recomputing it.
    result: Optional[A.SeriesResult] = None
    normalise_gain: bool = False

    listeners: List[Callable[[str], None]] = field(default_factory=list)

    # ---- paths --------------------------------------------------------------
    @property
    def raw_root(self) -> Optional[Path]:
        return self.panda_cfg.raw_root(self.logger_cfg)

    @property
    def processed_root(self) -> Path:
        return self.panda_cfg.processed_root(self.logger_cfg)

    @property
    def scan_log_csv(self) -> Path:
        return Path(self.logger_cfg.output_dir) / self.logger_cfg.csv_filename

    def series_out_dir(self) -> Path:
        """processed_data/<Peak>_<T>K/ -- the per-series sub-structure."""
        if self.series is None:
            return self.processed_root
        return self.processed_root / self.series.slug(self.logger_cfg)

    def slug(self) -> str:
        if self.series is None:
            return "manual"
        return self.series.slug(self.logger_cfg)

    # ---- change notification -----------------------------------------------
    def notify(self, what: str) -> None:
        for fn in list(self.listeners):
            try:
                fn(what)
            except Exception:                          # noqa: BLE001
                traceback.print_exc()

    def reset_downstream(self, from_stage: int) -> None:
        """Editing an early stage invalidates the later ones. Never show stale numbers."""
        if from_stage <= 1:
            self.taus, self.integrals = [], []
            self.fit = None
        elif from_stage <= 2:
            self.fit = None


# =============================================================================
# SMALL SHARED WIDGETS
# =============================================================================

def entry(parent, fonts, textvariable=None, width=12):
    return tk.Entry(parent, textvariable=textvariable, width=width,
                    font=fonts.mono, fg=T1, bg=BG_INPUT, insertbackground=CYAN,
                    relief="flat", bd=0, highlightthickness=1,
                    highlightbackground=BORDER, highlightcolor=CYAN)


def readout(parent, fonts, label, colour=T1, width=11):
    box = tk.Frame(parent, bg=BG_CARD, highlightthickness=1,
                   highlightbackground=BORDER)
    tk.Label(box, text=label, font=fonts.tiny, fg=T2, bg=BG_CARD).pack(padx=8)
    val = tk.Label(box, text="—", font=fonts.mono_big, fg=colour, bg=BG_CARD,
                   width=width)
    val.pack(padx=8, pady=(0, 3))
    return box, val


def make_tree(parent, columns, widths, fonts, height=8):
    """A ttk.Treeview themed to match, since tkinter has no table widget."""
    style = ttk.Style(parent)
    style.configure("Panda.Treeview", background=BG_CARD, fieldbackground=BG_CARD,
                    foreground=T1, borderwidth=0, rowheight=20,
                    font=fonts.mono_sm)
    style.configure("Panda.Treeview.Heading", background=BG_INPUT, foreground=T2,
                    borderwidth=0, font=fonts.sec)
    style.map("Panda.Treeview", background=[("selected", BORDER_HI)],
              foreground=[("selected", T1)])

    wrap = tk.Frame(parent, bg=BG_PANEL, highlightthickness=1,
                    highlightbackground=BORDER)
    tree = ttk.Treeview(wrap, columns=columns, show="headings", height=height,
                        style="Panda.Treeview")
    for c, w in zip(columns, widths):
        tree.heading(c, text=c.upper())
        tree.column(c, width=w, anchor="center", stretch=True)
    sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    # Rows are neutral by default; only genuinely stateful rows take colour.
    tree.tag_configure("ok", foreground=T1)
    tree.tag_configure("warn", foreground=AMBER)
    tree.tag_configure("bad", foreground=RED)
    return wrap, tree


class Banner(tk.Frame):
    """
    A non-dismissable warning strip.

    Deliberately not a dialog: a dialog is clicked away and forgotten, whereas
    this stays on the tab for as long as the condition holds and is therefore
    still present in a screenshot of the plot.
    """

    def __init__(self, parent, fonts):
        super().__init__(parent, bg=BG_CARD, highlightthickness=1,
                         highlightbackground=AMBER_DIM)
        self._fonts = fonts
        self.rule = tk.Frame(self, bg=AMBER, width=3)
        self.rule.pack(side="left", fill="y")
        inner = tk.Frame(self, bg=BG_CARD)
        inner.pack(side="left", fill="both", expand=True, padx=10, pady=6)
        self.title = tk.Label(inner, text="", font=fonts.sec, fg=AMBER,
                              bg=BG_CARD, anchor="w")
        self.title.pack(fill="x")
        self.body = tk.Label(inner, text="", font=fonts.mono_sm, fg=T1,
                             bg=BG_CARD, anchor="w", justify="left")
        self.body.pack(fill="x")
        # Wrap to the banner's actual width. A warning that runs off the right
        # edge loses precisely the sentence explaining what to do about it.
        inner.bind("<Configure>",
                   lambda e: self.body.configure(wraplength=max(e.width - 8, 200)))
        self._shown = False

    def show(self, title, body, colour=AMBER):
        self.title.configure(text=title.upper(), fg=colour)
        self.body.configure(text=body)
        self.rule.configure(bg=colour)
        self.configure(highlightbackground=colour)
        self._shown = True

    def hide(self):
        self._shown = False

    @property
    def shown(self):
        return self._shown


class StageTab(tk.Frame):
    """Common scaffolding: a control column on the left, content on the right."""

    STAGE = 0
    TITLE = ""
    SUBTITLE = ""

    def __init__(self, parent, state: PandaState):
        super().__init__(parent, bg=BG_BASE)
        self.state = state
        self.fonts = Fonts()

        # The control column carries a fair amount of explanatory text and its
        # height varies with the result being shown, so it scrolls rather than
        # silently clipping the bottom control off a shorter screen.
        outer = tk.Frame(self, bg=BG_PANEL, width=SIDE_W, highlightthickness=1,
                         highlightbackground=BORDER)
        outer.pack(side="left", fill="y", padx=(10, 0), pady=10)
        outer.pack_propagate(False)
        self._side_canvas = tk.Canvas(outer, bg=BG_PANEL, highlightthickness=0,
                                      bd=0, width=SIDE_W - 2)
        self._side_canvas.pack(side="left", fill="both", expand=True)
        self._side_bar = tk.Scrollbar(outer, orient="vertical",
                                      command=self._side_canvas.yview,
                                      bg=BG_CARD, troughcolor=BG_PANEL,
                                      relief="flat", bd=0, width=9)
        self._side_canvas.configure(yscrollcommand=self._on_side_scroll)

        self.side = tk.Frame(self._side_canvas, bg=BG_PANEL)
        self._side_window = self._side_canvas.create_window(
            0, 0, window=self.side, anchor="nw")
        self.side.bind("<Configure>", self._sync_side)
        self._side_canvas.bind("<Configure>", self._sync_side)
        for w in (self._side_canvas, self.side):
            w.bind("<MouseWheel>", self._side_wheel)

        self.main = tk.Frame(self, bg=BG_BASE)
        self.main.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        head = tk.Frame(self.side, bg=BG_PANEL)
        head.pack(fill="x", padx=12, pady=(12, 0))
        h, s = section_header(head, self.TITLE, self.fonts, T1)
        h.pack(anchor="w")
        tk.Label(self.side, text=self.SUBTITLE, font=self.fonts.tiny, fg=T3,
                 bg=BG_PANEL, wraplength=SIDE_W - 30, justify="left",
                 anchor="w").pack(fill="x", padx=12, pady=(6, 8))
        tk.Frame(self.side, bg=BORDER, height=1).pack(fill="x", padx=12, pady=(0, 10))

        state.listeners.append(self._on_state)
        self.build()
        self._bind_wheel(self.side)
        self.after(60, self._sync_side)

    def _bind_wheel(self, widget):
        for child in widget.winfo_children():
            try:
                child.bind("<MouseWheel>", self._side_wheel)
            except tk.TclError:
                pass
            self._bind_wheel(child)

    # ---- scrolling control column ------------------------------------------
    def _sync_side(self, _event=None):
        """
        Keep the scrollregion in step with the controls, and show the scrollbar
        exactly when they do not fit.

        The visibility decision is made here from the measured heights rather
        than inside the yscrollcommand callback: Tk only invokes that callback
        when the *view* changes, so a panel that grew taller while the view
        stayed at the top would never trigger it, and the overflow would be
        silently clipped instead of becoming scrollable.
        """
        c = self._side_canvas
        need = self.side.winfo_reqheight()
        have = c.winfo_height()
        c.itemconfigure(self._side_window, width=c.winfo_width())
        c.configure(scrollregion=(0, 0, c.winfo_width(), need))
        if need > have + 1:
            if not self._side_bar.winfo_ismapped():
                # before=canvas matters: the canvas is packed with expand=True
                # and claims the whole cavity, so a scrollbar packed after it
                # gets zero width and is never mapped at all.
                self._side_bar.pack(side="right", fill="y",
                                    before=self._side_canvas)
        elif self._side_bar.winfo_ismapped():
            self._side_bar.pack_forget()
            c.yview_moveto(0.0)

    def _on_side_scroll(self, lo, hi):
        self._side_bar.set(lo, hi)

    def _side_wheel(self, event):
        if self._side_bar.winfo_ismapped():
            self._side_canvas.yview_scroll(int(-event.delta / 120), "units")

    def build(self):                                   # pragma: no cover - override
        raise NotImplementedError

    def _on_state(self, what: str) -> None:
        pass

    def log(self, msg, colour=None):
        self.state.log(msg, colour)

    def content_grid(self, rows):
        """
        Lay the content column out with grid, not pack.

        pack distributes surplus space to expand=True children, but when the
        requested sizes are close to the cavity it will also happily squeeze a
        non-expanding sibling below its request -- which silently flattened the
        result tables to zero visible rows. grid with an explicit row weight is
        deterministic: exactly one row absorbs the surplus, every other row
        keeps the height it asked for.

        rows: sequence of (widget, weight, pady)
        """
        self.main.grid_columnconfigure(0, weight=1)
        for i, (widget, weight, pady) in enumerate(rows):
            widget.grid(row=i, column=0, sticky="nsew" if weight else "ew",
                        pady=pady)
            self.main.grid_rowconfigure(i, weight=weight)

    def sec(self, text, accent=None):
        h, s = section_header(self.side, text, self.fonts)
        h.pack(fill="x", padx=12, pady=(6, 0))
        s.pack(fill="x", padx=12, pady=(4, 8))

    def label(self, text):
        field_label(self.side, text, self.fonts).pack(fill="x", padx=12)

    def button(self, text, cmd, accent=None, fill=False):
        # accent is ignored for outline buttons by design (see make_button);
        # the filled primary always takes the single UI accent.
        b = make_button(self.side, text, cmd, self.fonts, None, fill)
        b.pack(fill="x", padx=12, pady=(4, 6))
        return b


# =============================================================================
# TAB 1  --  STAGE 1: REDUCE
# =============================================================================

class ReduceTab(StageTab):
    STAGE = 1
    TITLE = "Stage 1 · Reduce"
    SUBTITLE = ("Parse the SRS files of one tau-series, apply the column map, "
                "and normalise to I = Im / I0 with Poisson errors.")

    def build(self):
        st = self.state

        # ── series selection ────────────────────────────────────────────────
        self.sec("Tau series")
        self.label("FROM THE PHASE-1 SCAN LOG")
        self.series_var = tk.StringVar()
        self.series_box = ttk.Combobox(self.side, textvariable=self.series_var,
                                       style="Q.TCombobox", state="readonly",
                                       font=self.fonts.mono, values=[])
        self.series_box.pack(fill="x", padx=12, pady=(2, 6))
        self.series_box.bind("<<ComboboxSelected>>", lambda e: self.on_series_pick())

        self.label("POLARISATION CHANNEL")
        self.pol_var = tk.StringVar()
        self.pol_box = ttk.Combobox(self.side, textvariable=self.pol_var,
                                    style="Q.TCombobox", state="readonly",
                                    font=self.fonts.mono, values=[])
        self.pol_box.pack(fill="x", padx=12, pady=(2, 8))

        self.button("Reload scan log", self.reload_index)
        self.button("Reduce series", self.reduce_series, fill=True)

        tk.Frame(self.side, bg=BORDER, height=1).pack(fill="x", padx=12, pady=8)
        self.label("OR POINT AT .dat FILES DIRECTLY")
        self.button("Choose .dat files…", self.choose_files)

        # ── column map ──────────────────────────────────────────────────────
        self.sec("Column map")
        red = st.panda_cfg.reduction
        info = tk.Label(
            self.side,
            text=(f"2θ  :  {red.abscissa_column or '(first column)'}\n"
                  f"I₀  :  {red.i0_column}\n"
                  f"Iₘ  :  {red.im_column}"),
            font=self.fonts.mono, fg=T1, bg=BG_PANEL, justify="left", anchor="w")
        info.pack(fill="x", padx=12, pady=(0, 4))
        tk.Label(self.side,
                 text="Placeholder → panda_config.toml [reduction]",
                 font=self.fonts.tiny, fg=T3, bg=BG_PANEL, wraplength=SIDE_W - 30,
                 justify="left", anchor="w").pack(fill="x", padx=12, pady=(0, 8))

        # ── output ──────────────────────────────────────────────────────────
        self.sec("Output")
        self.button("Save reduced scans", self.save_reduced)

        # ── main: readouts, plot, table ─────────────────────────────────────
        strip = tk.Frame(self.main, bg=BG_BASE)
        self.ro = {}
        for k, lab, col in (("scans", "SCANS REDUCED", T1),
                            ("points", "POINTS / SCAN", T1),
                            ("missing", ".dat MISSING", T3),
                            ("energy", "MEAN ENERGY / eV", T1)):
            box, val = readout(strip, self.fonts, lab, col)
            box.pack(side="left", padx=(0, 8))
            self.ro[k] = val

        self.plot = PlotPanel(self.main, height=3.0)
        self.plot.clear("Choose a tau series and press REDUCE SERIES",
                        xlabel="2θ / degrees", ylabel="I = Iₘ / I₀")

        wrap, self.tree = make_tree(
            self.main,
            ["tau", "scan", "points", "energy", "field", "T", "gain", "pol",
             "status"],
            [60, 80, 60, 80, 70, 60, 85, 55, 200], self.fonts, height=8)
        self.content_grid([(strip, 0, (0, 0)), (self.plot, 1, (8, 8)),
                           (wrap, 0, (0, 0))])

        self.reload_index()

    # ---- data ---------------------------------------------------------------
    def reload_index(self):
        st = self.state
        try:
            st.series_index = A.load_series_index(st.scan_log_csv)
        except (ValueError, OSError) as exc:
            self.log(f"Scan log: {exc}", RED)
            st.series_index = []
        labels = [s.label for s in st.series_index]
        self.series_box.configure(values=labels)
        if not labels:
            self.log(f"No tau series found in {st.scan_log_csv}. "
                     f"Log some scans in the Scan Log tab, or point at .dat "
                     f"files directly.", AMBER)
            return
        if self.series_var.get() not in labels:
            self.series_var.set(labels[0])
        self.log(f"Scan log: {len(st.series_index)} tau series "
                 f"({sum(len(s.entries) for s in st.series_index)} scans).", GREEN)
        self.on_series_pick()

    def _current_series(self) -> Optional[A.Series]:
        for s in self.state.series_index:
            if s.label == self.series_var.get():
                return s
        return None

    def on_series_pick(self):
        s = self._current_series()
        if s is None:
            return
        pols = s.polarisations
        self.pol_box.configure(values=pols)
        if self.pol_var.get() not in pols:
            self.pol_var.set(pols[0] if pols else "")
        self.state.series = s
        self.state.polarisation = self.pol_var.get()

    def choose_files(self):
        paths = filedialog.askopenfilenames(
            title="Choose SRS .dat files",
            initialdir=str(self.state.raw_root or Path.home()),
            filetypes=[("SRS scan files", "*.dat"), ("All files", "*.*")])
        if not paths:
            return
        self.state.manual_files = [Path(p) for p in paths]
        self.state.series = None
        self.log(f"{len(paths)} file(s) chosen manually.")
        self.reduce_series(manual=True)

    def reduce_series(self, manual=False):
        st = self.state
        st.reduced = []
        st.reset_downstream(1)
        red = st.panda_cfg.reduction
        rows: List[Tuple] = []
        missing: List[int] = []

        if manual or (st.series is None and st.manual_files):
            jobs = [(None, p) for p in st.manual_files]
        else:
            self.on_series_pick()
            s = st.series
            if s is None:
                self.log("Choose a tau series first.", AMBER)
                return
            st.polarisation = self.pol_var.get()
            entries = s.by_polarisation(st.polarisation)
            if not entries:
                self.log(f"No scans logged for channel {st.polarisation!r} "
                         f"in {s.label}.", AMBER)
                return
            jobs = []
            for e in entries:
                p = A.resolve_dat(e.scan_number, st.raw_root,
                                  st.logger_cfg.dat_patterns)
                if p is None:
                    missing.append(e.scan_number)
                    rows.append((e.tau_deg, e.scan_number, "—", "—", "—", "—",
                                 "—", st.polarisation, ".dat not on disk",
                                 "warn"))
                else:
                    jobs.append((e, p))

        if st.raw_root is None:
            self.log("No raw data directory. Set dat_dir in rsxd_config.toml "
                     "(or raw_dir in panda_config.toml).", RED)

        n_fail = 0
        for e, path in jobs:
            try:
                scan = A.parse_srs(path)
                extra = {}
                gain = None
                if e is not None:
                    # Resolve the gain from the log's epochs. Without this the
                    # tab would report UNKNOWN for every scan even when the
                    # CSV records the gain perfectly well, and the mixed-gain
                    # guard downstream would fire for the wrong reason.
                    gain = st.series.gain_of(e.scan_number)
                    extra = dict(tau_deg=A._as_float(e.tau_deg),
                                 peak=st.series.peak,
                                 series_temperature_K=st.series.temperature_K,
                                 note=e.note,
                                 gain=gain)
                divisor = gain if (st.normalise_gain and gain is not None) else None
                r = A.reduce_scan(scan, red, normalise_gain=divisor, **extra)
            except A.ColumnMapError as exc:
                # The error the operator will actually hit on day one.
                n_fail += 1
                self.log(str(exc), RED)
                rows.append(((e.tau_deg if e else "—"),
                             (e.scan_number if e else path.stem), "—", "—", "—",
                             "—", "—", "—",
                             "column map does not match this file", "bad"))
                continue
            except A.SRSParseError as exc:
                n_fail += 1
                self.log(str(exc), RED)
                rows.append(((e.tau_deg if e else "—"),
                             (e.scan_number if e else path.stem), "—", "—", "—",
                             "—", "—", "—", "unreadable", "bad"))
                continue

            st.reduced.append(r)
            status = "ok"
            tag = "ok"
            if r.n_dropped:
                status = f"ok · {r.n_dropped} pt dropped (I₀ low)"
                tag = "warn"
            rows.append((_fmt(r.tau_deg, 0), r.scan_number, r.n_points,
                         _fmt(r.energy_eV, 3), _fmt(r.field_T, 4),
                         _fmt(r.temperature_K, 1),
                         fmt_gain(r.gain) or "unknown",
                         r.polarisation, status, tag))

        rows.sort(key=lambda t: (_sortable(t[0])))
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            self.tree.insert("", "end", values=r[:-1], tags=(r[-1],))

        # Publish the gain state for the later stages. Built by hand here
        # because Stage 1 may also have been driven from hand-picked files.
        st.result = A.SeriesResult(
            series=st.series, polarisation=st.polarisation, reduced=st.reduced,
            integrals=[], taus=[r.tau_deg for r in st.reduced],
            fit=A.fit_stokes([], [], None), gain_normalised=False)

        self.ro["scans"].configure(text=str(len(st.reduced)))
        self.ro["points"].configure(
            text=str(st.reduced[0].n_points) if st.reduced else "—")
        self.ro["missing"].configure(text=str(len(missing)) if missing else "0",
                                     fg=AMBER if missing else T3)
        energies = [r.energy_eV for r in st.reduced if r.energy_eV is not None]
        self.ro["energy"].configure(
            text=f"{np.mean(energies):.2f}" if energies else "—")

        if missing:
            self.log(f"{len(missing)} scan(s) have no .dat on disk yet: "
                     f"{', '.join(str(m) for m in missing[:12])}"
                     f"{' …' if len(missing) > 12 else ''}. "
                     f"Partial series still reduce.", AMBER)
        if n_fail:
            self.log(f"{n_fail} file(s) could not be reduced — see above.", RED)
        if st.reduced:
            self.log(f"Stage 1: reduced {len(st.reduced)} scans "
                     f"({st.reduced[0].n_points} points each). I = Iₘ/I₀.", GREEN)
            if st.result.mixed_gain:
                self.log(f"Stage 1: MIXED GAIN — {st.result.gain_summary()}. "
                         f"Intensities across this series are not in the same "
                         f"units.", AMBER)
            self.replot()
        else:
            self.plot.clear("Nothing reduced", xlabel="2θ / degrees",
                            ylabel="I = Iₘ / I₀")
        st.notify("reduced")

    def replot(self):
        st = self.state
        ax = self.plot.axes(xlabel=f"{st.reduced[0].abscissa_name} / degrees",
                            ylabel="I = Iₘ / I₀",
                            title=f"Stage 1 · {st.slug()} · {st.polarisation}")
        taus = [r.tau_deg for r in st.reduced if r.tau_deg is not None]
        lo, hi = (min(taus), max(taus)) if taus else (-90.0, 90.0)
        for r in st.reduced:
            c = tau_colour(r.tau_deg if r.tau_deg is not None else 0.0, lo, hi)
            ax.plot(r.two_theta, r.intensity, lw=1.0, color=c,
                    label=f"τ={_fmt(r.tau_deg, 0)}°")
        if len(st.reduced) <= 14:
            self.plot.legend(ax, ncol=2, loc="upper right")
        self.plot.draw()

    def save_reduced(self):
        st = self.state
        if not st.reduced:
            self.log("Nothing to save — reduce a series first.", AMBER)
            return
        n = 0
        for r in st.reduced:
            out = A.mirror_dir(r.source, st.raw_root, st.processed_root)
            try:
                p = A.save_reduced(r, out, fmt=st.panda_cfg.out_format)
                n += 1
            except OSError as exc:
                self.log(f"Save failed for {r.source.name}: {exc}", RED)
        if n:
            self.log(f"Stage 1: saved {n} reduced scan(s) under "
                     f"{st.processed_root} (mirroring raw_data).", GREEN)

    def _on_state(self, what):
        if what == "config":
            self.reload_index()


# =============================================================================
# TAB 2  --  STAGE 3a: TAU CURVE  (the established method)
# =============================================================================

class TauCurveTab(StageTab):
    STAGE = 2
    TITLE = "Stage 3a · Integrate"
    SUBTITLE = ("Integrate the reflection in each reduced scan and plot "
                "intensity against analyser angle τ. Model-free by default — "
                "this is the established RASOR method.")

    def build(self):
        st = self.state

        self.sec("Integration window")
        row = tk.Frame(self.side, bg=BG_PANEL)
        row.pack(fill="x", padx=12, pady=(0, 6))
        left = tk.Frame(row, bg=BG_PANEL); left.pack(side="left", expand=True, fill="x")
        field_label(left, "FROM", self.fonts).pack(anchor="w")
        self.wmin = entry(left, self.fonts, width=9); self.wmin.pack(fill="x")
        right = tk.Frame(row, bg=BG_PANEL); right.pack(side="left", expand=True,
                                                      fill="x", padx=(6, 0))
        field_label(right, "TO", self.fonts).pack(anchor="w")
        self.wmax = entry(right, self.fonts, width=9); self.wmax.pack(fill="x")
        if st.panda_cfg.window_min or st.panda_cfg.window_max:
            self.wmin.insert(0, f"{st.panda_cfg.window_min:g}")
            self.wmax.insert(0, f"{st.panda_cfg.window_max:g}")
        self.button("Use full scan range", self.full_range)

        self.sec("Method")
        self.method_var = tk.StringVar(value=st.panda_cfg.method)
        for val, text in (("trapezoid", "Trapezoid — model-free (default)"),
                          ("voigt", "Voigt fit — cross-check")):
            tk.Radiobutton(self.side, text=text, value=val,
                           variable=self.method_var, font=self.fonts.ui,
                           fg=T1, bg=BG_PANEL, selectcolor=BG_INPUT,
                           activebackground=BG_PANEL, activeforeground=T1,
                           highlightthickness=0, bd=0, anchor="w"
                           ).pack(fill="x", padx=12)
        tk.Label(self.side,
                 text=("A wrong shape biases the area τ-dependently — "
                       "indistinguishable from real dichroism."),
                 font=self.fonts.tiny, fg=T3, bg=BG_PANEL, wraplength=SIDE_W - 30,
                 justify="left", anchor="w").pack(fill="x", padx=12, pady=(4, 8))

        self.bkg_var = tk.StringVar(value=st.panda_cfg.background)
        tk.Checkbutton(self.side, text="Linear background (endpoints)",
                       variable=self.bkg_var, onvalue="linear", offvalue="none",
                       font=self.fonts.tiny, fg=T2, bg=BG_PANEL,
                       selectcolor=BG_INPUT, activebackground=BG_PANEL,
                       activeforeground=T1, highlightthickness=0, bd=0,
                       anchor="w").pack(fill="x", padx=12, pady=(0, 6))

        # ── gain ────────────────────────────────────────────────────────────
        self.sec("Detector gain")
        self.norm_var = tk.BooleanVar(value=False)
        self.norm_check = tk.Checkbutton(
            self.side, text="Normalise by gain", variable=self.norm_var,
            font=self.fonts.ui, fg=T1, bg=BG_PANEL, selectcolor=BG_INPUT,
            activebackground=BG_PANEL, activeforeground=T1,
            highlightthickness=0, bd=0, anchor="w", command=self.on_norm_toggle)
        self.norm_check.pack(fill="x", padx=12)
        self.norm_note = tk.Label(
            self.side,
            text=("Off by default. Divides Iₘ by each scan's gain so scans "
                  "taken at different amplifier settings are comparable."),
            font=self.fonts.tiny, fg=T3, bg=BG_PANEL,
            wraplength=SIDE_W - 30, justify="left", anchor="w")
        self.norm_note.pack(fill="x", padx=12, pady=(2, 8))

        self.button("Integrate", self.integrate, fill=True)
        self.sec("Output")
        self.button("Save curve + errors", self.save)

        strip = tk.Frame(self.main, bg=BG_BASE)
        self.ro = {}
        for k, lab, col in (("n", "POINTS ON CURVE", T1),
                            ("mod", "MODULATION DEPTH", T1),
                            ("snr", "MEDIAN I / σ", T1),
                            ("method", "METHOD", T1)):
            box, val = readout(strip, self.fonts, lab, col)
            box.pack(side="left", padx=(0, 8))
            self.ro[k] = val

        self.banner = Banner(self.main, self.fonts)
        self.plot = PlotPanel(self.main, height=3.0)
        self.plot.clear("Reduce a series in Stage 1, then press INTEGRATE",
                        xlabel="analyser angle τ / degrees",
                        ylabel="integrated intensity")

        wrap, self.tree = make_tree(
            self.main,
            ["tau", "scan", "integrated I", "sigma", "I/sigma", "pts", "note"],
            [60, 80, 130, 110, 80, 55, 220], self.fonts, height=8)
        self._rows = [(strip, 0, (0, 0)), (self.banner, 0, (8, 0)),
                      (self.plot, 1, (8, 8)), (wrap, 0, (0, 0))]
        self.content_grid(self._rows)
        self.banner.grid_remove()

    def on_norm_toggle(self):
        """Re-reduce through Stage 1 with the new setting, then re-integrate."""
        self.state.normalise_gain = bool(self.norm_var.get())
        self.log(f"Stage 3a: gain normalisation "
                 f"{'ON' if self.state.normalise_gain else 'OFF'}.", AMBER)
        self.integrate(reduce_first=True)

    def _update_banner(self):
        """
        Show or hide the mixed-gain warning, and enable/disable the toggle.

        The toggle is disabled when ANY scan has unknown gain: you cannot
        divide by a number you do not have, and offering a control that would
        silently do nothing for some points is worse than not offering it.
        """
        st = self.state
        res = st.result
        if res is None or not res.reduced:
            self.banner.grid_remove()
            self.norm_check.configure(state="normal")
            return

        if res.has_unknown_gain:
            self.norm_var.set(False)
            st.normalise_gain = False
            self.norm_check.configure(state="disabled")
            self.norm_note.configure(
                text="Disabled: some scans have no recorded gain. You cannot "
                     "normalise by a number that was never written down.")
        else:
            self.norm_check.configure(state="normal")
            self.norm_note.configure(
                text=("Off by default. Divides Iₘ by each scan's gain so scans "
                      "taken at different amplifier settings are comparable."))

        if not res.mixed_gain:
            self.banner.grid_remove()
            return

        if st.normalise_gain and not res.has_unknown_gain:
            self.banner.show(
                "gain normalised",
                f"This series spans more than one detector gain "
                f"({res.gain_summary()}). Iₘ has been divided by each scan's "
                f"gain, so the points below are in common units.", GREEN)
        else:
            self.banner.show(
                "mixed detector gain — tau curve not in consistent units",
                f"{res.gain_summary()}\n"
                f"A gain change multiplies part of this curve by a constant. "
                f"That step is indistinguishable in shape from real dichroism, "
                f"and Stage 3b will fit it. Turn on 'Normalise by gain', or "
                f"treat the result as suspect.")
        self.banner.grid()

    def full_range(self):
        st = self.state
        if not st.reduced:
            self.log("Reduce a series first.", AMBER)
            return
        lo = min(float(np.min(r.two_theta)) for r in st.reduced)
        hi = max(float(np.max(r.two_theta)) for r in st.reduced)
        self.wmin.delete(0, "end"); self.wmin.insert(0, f"{lo:g}")
        self.wmax.delete(0, "end"); self.wmax.insert(0, f"{hi:g}")

    def _window(self) -> Optional[Tuple[float, float]]:
        a, b = self.wmin.get().strip(), self.wmax.get().strip()
        if not a or not b:
            return None
        try:
            lo, hi = float(a), float(b)
        except ValueError:
            self.log("2θ window: not a number. Using the full scan range.", AMBER)
            return None
        if lo == hi:
            return None
        return (lo, hi)

    def integrate(self, reduce_first=False):
        st = self.state
        if not st.reduced:
            self.log("Nothing to integrate — reduce a series in Stage 1 first.",
                     AMBER)
            return
        if reduce_first and st.series is not None and st.polarisation:
            # Normalisation happens during reduction (it scales Im), so a
            # change of setting has to go back through Stage 1.
            res = A.process_series(st.series, st.polarisation, st.logger_cfg,
                                   st.panda_cfg, window=st.window,
                                   method=st.method,
                                   normalise_gain=st.normalise_gain)
            if res.reduced:
                st.reduced = res.reduced
                st.result = res
        st.reset_downstream(2)
        st.window = self._window()
        st.method = self.method_var.get()
        bkg = self.bkg_var.get()

        st.taus, st.integrals = [], []
        rows = []
        for r in st.reduced:
            it = A.integrate_peak(r, window=st.window, method=st.method,
                                  background=bkg)
            st.taus.append(r.tau_deg if r.tau_deg is not None else float("nan"))
            st.integrals.append(it)
            snr = (it.area / it.sigma) if it.sigma and math.isfinite(it.sigma) \
                and it.sigma > 0 else float("nan")
            tag = "ok" if it.fit_ok else "bad"
            rows.append(((_fmt(r.tau_deg, 0), r.scan_number, _fmt_g(it.area, 6),
                          _fmt_g(it.sigma, 3), _fmt(snr, 1), it.n_points,
                          it.message or r.note), tag))
            if not it.fit_ok:
                self.log(f"τ={_fmt(r.tau_deg,0)}°: {it.message}", AMBER)

        self.tree.delete(*self.tree.get_children())
        for vals, tag in rows:
            self.tree.insert("", "end", values=vals, tags=(tag,))

        areas = np.array([i.area for i in st.integrals], dtype=float)
        sigs = np.array([i.sigma for i in st.integrals], dtype=float)
        finite = np.isfinite(areas)
        self.ro["n"].configure(text=str(int(finite.sum())))
        if finite.sum() >= 2 and np.nanmean(areas[finite]) != 0:
            depth = ((np.nanmax(areas[finite]) - np.nanmin(areas[finite]))
                     / (np.nanmax(areas[finite]) + np.nanmin(areas[finite])))
            self.ro["mod"].configure(text=f"{depth:.3f}")
        else:
            self.ro["mod"].configure(text="—")
        good = finite & np.isfinite(sigs) & (sigs > 0)
        self.ro["snr"].configure(
            text=f"{np.median(areas[good] / sigs[good]):.0f}" if good.any() else "—")
        self.ro["method"].configure(text=st.method)

        # Keep the shared result in step so Stage 3b inherits this verdict.
        if st.result is not None:
            st.result.integrals = list(st.integrals)
            st.result.taus = list(st.taus)
            st.result.gain_normalised = bool(st.normalise_gain)

        win = st.window or (float(np.min(st.reduced[0].two_theta)),
                            float(np.max(st.reduced[0].two_theta)))
        self.log(f"Stage 3a: integrated {int(finite.sum())} scans over "
                 f"2θ ∈ [{win[0]:g}, {win[1]:g}], method={st.method}, "
                 f"background={bkg}.", AMBER)
        if st.result is not None and st.result.mixed_gain:
            self.log(f"Stage 3a: MIXED GAIN — {st.result.gain_summary()}"
                     + ("  (normalised)" if st.normalise_gain
                        else "  — NOT normalised; the curve contains an "
                             "amplifier step"), AMBER)
        self._update_banner()
        self.replot()
        st.notify("integrated")

    def replot(self):
        st = self.state
        ax = self.plot.axes(xlabel="analyser angle τ / degrees",
                            ylabel="integrated intensity",
                            title=f"Stage 3a · {st.slug()} · {st.polarisation}")
        t = np.array(st.taus, dtype=float)
        y = np.array([i.area for i in st.integrals], dtype=float)
        e = np.array([i.sigma for i in st.integrals], dtype=float)
        ax.errorbar(t, y, yerr=e, fmt="o", ms=5, color=DATA_PRIMARY,
                    ecolor=DATA_PRIMARY, elinewidth=1.2, capsize=3,
                    mfc=BG_CARD, mew=1.4, label="integrated intensity")
        ax.plot(t, y, lw=0.8, color=DATA_PRIMARY, alpha=0.35)

        # Mark the points that belong to a later gain epoch ON THE PLOT, so
        # the warning survives being screenshotted out of the app.
        res = st.result
        if res is not None and res.mixed_gain and not st.normalise_gain:
            gains = [r.gain for r in st.reduced]
            first = next((g for g in gains if g is not None), None)
            odd = [i for i, g in enumerate(gains) if g != first]
            if odd:
                ax.plot(t[odd], y[odd], "o", ms=11, mfc="none",
                        mec=AMBER, mew=1.6, zorder=4,
                        label="different / unknown gain")
        self.plot.legend(ax, loc="best")
        self.plot.draw()

    def save(self):
        st = self.state
        if not st.integrals:
            self.log("Nothing to save — press INTEGRATE first.", AMBER)
            return
        out = st.series_out_dir()
        points = []
        for r, it in zip(st.reduced, st.integrals):
            points.append({
                "analyser_angle_deg": r.tau_deg,
                "scan_number": r.scan_number,
                "integrated_intensity": it.area,
                "sigma": it.sigma,
                "window_min": it.window[0], "window_max": it.window[1],
                "n_points": it.n_points, "method": it.method,
                "background": it.background,
                "energy_eV": r.energy_eV, "field_T": r.field_T,
                "temperature_K": r.temperature_K,
                "gain": fmt_gain(r.gain) or "unknown",
                "gain_normalised": "yes" if r.gain_normalised else "no",
                "note": r.note,
            })
        try:
            res = st.result
            gain_note = (res.gain_summary() if res is not None else "")
            if res is not None and res.mixed_gain:
                gain_note += ("  -- NORMALISED (Im divided by each scan's gain)"
                              if st.normalise_gain
                              else "  -- NOT NORMALISED: this curve mixes units")
            p = A.save_tau_curve(st.slug(), st.polarisation or "all", points, out,
                                 fmt=st.panda_cfg.out_format, gain_note=gain_note)
            self.log(f"Stage 3a: saved {p}", GREEN)
        except OSError as exc:
            self.log(f"Save failed: {exc}", RED)

    def _on_state(self, what):
        if what == "reduced":
            self.tree.delete(*self.tree.get_children())
            for k in self.ro:
                self.ro[k].configure(text="—")
            self.plot.clear("Press INTEGRATE",
                            xlabel="analyser angle τ / degrees",
                            ylabel="integrated intensity")
            if self.state.reduced and not self.wmin.get().strip():
                self.full_range()




# =============================================================================
# TAB 3  --  STAGE 3b: STOKES FIT
# =============================================================================

class StokesTab(StageTab):
    STAGE = 3
    TITLE = "Stage 3b · Stokes fit"
    SUBTITLE = ("Fit I(τ) = (I₀/2)[1 + P₁′cos2τ + P₂′sin2τ] to the SAME "
                "integrated numbers Stage 3a produced, so 3a is a direct "
                "ground-truth check on 3b.")

    def build(self):
        self.sec("Fit")
        tk.Label(self.side,
                 text="Input: the (τ, I, σ) table from Stage 3a.",
                 font=self.fonts.tiny, fg=T3, bg=BG_PANEL,
                 wraplength=SIDE_W - 30, justify="left",
                 anchor="w").pack(fill="x", padx=12, pady=(0, 8))
        self.button("Fit Stokes parameters", self.fit, fill=True)

        self.sec("Result")
        self.result = tk.Label(self.side, text="not fitted", font=self.fonts.mono,
                               fg=T1, bg=BG_PANEL, justify="left", anchor="w",
                               wraplength=SIDE_W - 30)
        self.result.pack(fill="x", padx=12, pady=(0, 6))
        self.character = tk.Label(self.side, text="", font=self.fonts.sec,
                                  fg=T1, bg=BG_PANEL, justify="left",
                                  anchor="w", wraplength=SIDE_W - 30)
        self.character.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(self.side,
                 text=("Sign convention: τ as logged. Calibrate the analyser "
                       "zero against a known charge peak."),
                 font=self.fonts.tiny, fg=T3, bg=BG_PANEL,
                 wraplength=SIDE_W - 30, justify="left",
                 anchor="w").pack(fill="x", padx=12, pady=(0, 8))

        self.sec("Output")
        self.button("Save fit results", self.save)
        self.button("Add to Stage 4", self.add_to_comparison)

        strip = tk.Frame(self.main, bg=BG_BASE)
        self.ro = {}
        for k, lab, col in (("p1", "P₁′", T1),
                            ("p2", "P₂′", T1),
                            ("deg", "LINEAR DEGREE", T1),
                            ("chi", "REDUCED χ²", T1)):
            box, val = readout(strip, self.fonts, lab, col, width=13)
            box.pack(side="left", padx=(0, 8))
            self.ro[k] = val

        self.banner = Banner(self.main, self.fonts)
        self.plot = PlotPanel(self.main, height=3.6)
        self.plot.clear("Run Stage 3a, then press FIT STOKES PARAMETERS",
                        xlabel="analyser angle τ / degrees",
                        ylabel="integrated intensity")
        self.content_grid([(strip, 0, (0, 0)), (self.banner, 0, (8, 0)),
                           (self.plot, 1, (8, 0))])
        self.banner.grid_remove()

    def fit(self):
        st = self.state
        if not st.integrals:
            self.log("Nothing to fit — run Stage 3a first.", AMBER)
            return
        areas = [i.area for i in st.integrals]
        sigmas = [i.sigma for i in st.integrals]
        st.fit = A.fit_stokes(st.taus, areas, sigmas)
        f = st.fit
        if not f.ok:
            self.log(f"Stage 3b: {f.message}", RED)
            self.result.configure(text=f.message)
            return

        # Stage 3b INHERITS 3a's verdict rather than recomputing it. If the
        # tau curve mixed units, part of the modulation this fit just measured
        # is an amplifier setting. The numbers are still shown -- hiding them
        # would be worse -- but they are labelled for what they are.
        res = st.result
        suspect = bool(res is not None and res.suspect)
        colour = AMBER if suspect else T1

        self.ro["p1"].configure(text=f"{f.p1:+.4f}", fg=colour)
        self.ro["p2"].configure(text=f"{f.p2:+.4f}", fg=colour)
        self.ro["deg"].configure(text=f"{f.linear_degree:.4f}")
        self.ro["chi"].configure(
            text=f"{f.reduced_chi2:.2f}",
            fg=GREEN if 0.3 < f.reduced_chi2 < 3.0 else AMBER)
        self.result.configure(text=(
            f"P₁′ = {f.p1:+.4f} ± {f.p1_err:.4f}\n"
            f"P₂′ = {f.p2:+.4f} ± {f.p2_err:.4f}\n"
            f"I₀  = {f.i_total:.6g} ± {f.i_total_err:.3g}\n"
            f"angle = {f.linear_angle_deg:+.2f}°\n"
            f"χ²/dof = {f.chi2:.2f}/{f.dof} = {f.reduced_chi2:.2f}"))
        # The words already say which it is; tinting them adds nothing and
        # would spend the accent on something that is not an interface state.
        self.character.configure(
            text=("SUSPECT · " if suspect else "") + f.character.upper(),
            fg=AMBER if suspect else T1)
        self.log(f"Stage 3b: P₁′ = {f.p1:+.4f} ± {f.p1_err:.4f}, "
                 f"P₂′ = {f.p2:+.4f} ± {f.p2_err:.4f}, "
                 f"χ²/dof = {f.reduced_chi2:.2f} → {f.character}",
                 AMBER if suspect else GREEN)

        if suspect:
            self.banner.show(
                "suspect — fitted from a mixed-gain tau curve",
                f"{res.gain_summary()}\n"
                f"Part of the modulation these parameters describe is a change "
                f"of detector gain, not physics. Normalise in Stage 3a, or "
                f"treat P₁′/P₂′ as indicative only.")
            self.banner.grid()
        elif res is not None and res.mixed_gain and res.gain_normalised:
            self.banner.show(
                "gain normalised",
                f"This series spanned more than one gain "
                f"({res.gain_summary()}); Iₘ was divided by each scan's gain "
                f"before integrating, so the fit below is in common units.",
                GREEN)
            self.banner.grid()
        else:
            self.banner.grid_remove()
        if f.reduced_chi2 > 3.0:
            self.log("Reduced χ² is high: the errors may be underestimated, or "
                     "the 2θ window is clipping the peak at some τ.", AMBER)
        self.replot()
        st.notify("fitted")

    def replot(self):
        st = self.state
        f = st.fit
        ax = self.plot.axes(xlabel="analyser angle τ / degrees",
                            ylabel="integrated intensity",
                            title=f"Stage 3b · {st.slug()} · {st.polarisation}")
        t = np.array(st.taus, dtype=float)
        y = np.array([i.area for i in st.integrals], dtype=float)
        e = np.array([i.sigma for i in st.integrals], dtype=float)
        ax.errorbar(t, y, yerr=e, fmt="o", ms=5, color=DATA_PRIMARY,
                    ecolor=DATA_PRIMARY, elinewidth=1.2, capsize=3,
                    mfc=BG_CARD, mew=1.4, zorder=3, label="Stage 3a integrals")
        if f is not None and f.ok:
            grid = np.linspace(np.nanmin(t), np.nanmax(t), 400)
            ax.plot(grid, A.stokes_model(grid, f.i_total, f.p1, f.p2), lw=1.8,
                    color=DATA_FIT, zorder=2,
                    label=(f"fit: P₁′={f.p1:+.3f}±{f.p1_err:.3f}, "
                           f"P₂′={f.p2:+.3f}±{f.p2_err:.3f}"))
            ax.axhline(f.i_total / 2.0, color=T3, lw=0.8, ls="--", zorder=1,
                       label="I₀/2 (unpolarised level)")
        self.plot.legend(ax, loc="best")
        self.plot.draw()

    def save(self):
        st = self.state
        if st.fit is None:
            self.log("Nothing to save — fit first.", AMBER)
            return
        try:
            res = st.result
            p = A.save_stokes(st.slug(), st.polarisation or "all", st.fit,
                              st.series_out_dir(), fmt=st.panda_cfg.out_format,
                              suspect=bool(res is not None and res.suspect),
                              gain_note=(res.gain_summary() if res else ""))
            self.log(f"Stage 3b: saved {p}", GREEN)
        except OSError as exc:
            self.log(f"Save failed: {exc}", RED)

    def add_to_comparison(self):
        st = self.state
        if st.fit is None or st.series is None:
            self.log("Fit a series first (and pick it from the scan log).", AMBER)
            return
        result = A.SeriesResult(series=st.series, polarisation=st.polarisation,
                                reduced=st.reduced, integrals=st.integrals,
                                taus=st.taus, fit=st.fit,
                                gain_normalised=bool(st.normalise_gain))
        summary = A.summarise(result, st.panda_cfg)
        key = (summary.peak, summary.temperature_K, summary.polarisation)
        st.comparison = [s for s in st.comparison
                         if (s.peak, s.temperature_K, s.polarisation) != key]
        st.comparison.append(summary)
        self.log(f"Stage 4: added {summary.peak} {summary.temperature_K} K "
                 f"{summary.polarisation} ({summary.edge or '?'}, "
                 f"{summary.field_state}, gain {summary.gain_summary}).",
                 AMBER if summary.suspect else None)
        st.notify("comparison")

    def _on_state(self, what):
        if what in ("reduced", "integrated"):
            for k in self.ro:
                self.ro[k].configure(text="—")
            self.result.configure(text="not fitted")
            self.character.configure(text="")
            self.plot.clear("Press FIT STOKES PARAMETERS",
                            xlabel="analyser angle τ / degrees",
                            ylabel="integrated intensity")


# =============================================================================
# TAB 4  --  STAGE 4: PHYSICS READ-OUTS
# =============================================================================

class PhysicsTab(StageTab):
    STAGE = 4
    TITLE = "Stage 4 · Physics"
    SUBTITLE = ("Compare fitted series to surface the conclusions: on- vs "
                "off-resonance, field on vs off, Fe vs Co.")

    def build(self):
        self.sec("Comparison set")
        tk.Label(self.side,
                 text="Series arrive from Stage 3b, or all at once below.",
                 font=self.fonts.tiny, fg=T3, bg=BG_PANEL,
                 wraplength=SIDE_W - 30, justify="left",
                 anchor="w").pack(fill="x", padx=12, pady=(0, 8))
        self.button("Process all series", self.process_all, fill=True)
        self.button("Remove selected", self.remove_selected)
        self.button("Clear comparison", self.clear_all)

        self.sec("Grouping")
        self.group_var = tk.StringVar(value="resonance")
        for val, text in (("resonance", "On- vs off-resonance"),
                          ("field", "Field on vs off"),
                          ("peak", "Fe vs Co (by peak)"),
                          ("temperature", "By temperature")):
            tk.Radiobutton(self.side, text=text, value=val,
                           variable=self.group_var, font=self.fonts.ui,
                           fg=T1, bg=BG_PANEL, selectcolor=BG_INPUT,
                           activebackground=BG_PANEL, activeforeground=T1,
                           highlightthickness=0, bd=0, anchor="w",
                           command=self.replot).pack(fill="x", padx=12)

        self.sec("Output")
        self.button("Save comparison", self.save, GREEN)

        strip = tk.Frame(self.main, bg=BG_BASE)
        self.ro = {}
        for k, lab, col, wide in (("n", "SERIES COMPARED", T1, 11),
                                  ("charge", "CHARGE-LIKE", T1, 11),
                                  ("magnetic", "MAGNETIC-LIKE", T1, 11),
                                  ("edges", "EDGES SEEN", T1, 22)):
            box, val = readout(strip, self.fonts, lab, col, width=wide)
            box.pack(side="left", padx=(0, 8))
            self.ro[k] = val

        self.banner = Banner(self.main, self.fonts)
        self.plot = PlotPanel(self.main, height=3.2)
        self.plot.clear("Add fitted series from Stage 3b",
                        xlabel="P₁′", ylabel="")

        wrap, self.tree = make_tree(
            self.main,
            ["peak", "T/K", "pol", "edge", "resonance", "field", "gain",
             "P1'", "P2'", "chi2", "character"],
            [80, 50, 40, 70, 105, 92, 150, 112, 112, 58, 150], self.fonts,
            height=9)
        self.content_grid([(strip, 0, (0, 0)), (self.banner, 0, (8, 0)),
                           (self.plot, 1, (8, 8)), (wrap, 0, (0, 0))])
        self.banner.grid_remove()

    # ---- bulk ---------------------------------------------------------------
    def process_all(self):
        """
        Run Stage 1 -> 3a -> 3b over every series in the scan log, with the
        current 2theta window. Convenience for the end of a beamtime; the
        per-tab route stays the one you use while deciding the window.
        """
        st = self.state
        if not st.series_index:
            self.log("No series in the scan log.", AMBER)
            return
        n_ok = 0
        for series in st.series_index:
            for pol in series.polarisations:
                res = A.process_series(series, pol, st.logger_cfg, st.panda_cfg,
                                       window=st.window, method=st.method,
                                       normalise_gain=st.normalise_gain)
                if not res.reduced:
                    continue
                for e in res.errors[:1]:
                    self.log(e, RED)
                for wmsg in res.warnings:
                    self.log("%s %s: %s" % (series.label, pol, wmsg), AMBER)
                summary = A.summarise(res, st.panda_cfg)
                key = (summary.peak, summary.temperature_K, summary.polarisation)
                st.comparison = [s for s in st.comparison
                                 if (s.peak, s.temperature_K, s.polarisation) != key]
                st.comparison.append(summary)
                n_ok += 1
        self.log(f"Stage 4: processed {n_ok} series/channel combination(s).",
                 GREEN if n_ok else AMBER)
        self.refresh()

    def remove_selected(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = [self.tree.index(i) for i in sel]
        self.state.comparison = [s for k, s in enumerate(self.state.comparison)
                                 if k not in idx]
        self.refresh()

    def clear_all(self):
        self.state.comparison = []
        self.refresh()

    # ---- display ------------------------------------------------------------
    def refresh(self):
        st = self.state
        self.tree.delete(*self.tree.get_children())
        for s in st.comparison:
            tag = ("ok" if "charge" in s.fit.character
                   else "warn" if "magnetic" in s.fit.character else "")
            gain_cell = s.gain_summary or "—"
            if s.suspect:
                gain_cell = "MIXED! " + gain_cell
                tag = "warn"
            elif s.gain_normalised and s.mixed_gain:
                gain_cell = "normalised: " + gain_cell
            self.tree.insert("", "end", values=(
                s.peak, s.temperature_K, s.polarisation, s.edge or "—",
                s.resonance, s.field_state, gain_cell,
                f"{s.fit.p1:+.4f}±{s.fit.p1_err:.4f}",
                f"{s.fit.p2:+.4f}±{s.fit.p2_err:.4f}",
                f"{s.fit.reduced_chi2:.2f}", s.fit.character), tags=(tag,))
        self.ro["n"].configure(text=str(len(st.comparison)))
        self.ro["charge"].configure(
            text=str(sum(1 for s in st.comparison if "charge" in s.fit.character)))
        self.ro["magnetic"].configure(
            text=str(sum(1 for s in st.comparison if "magnetic" in s.fit.character)))
        edges = sorted({s.edge for s in st.comparison if s.edge})
        self.ro["edges"].configure(text=", ".join(edges) if edges else "—")

        # A comparison that mixes gains without normalising is comparing
        # rulers, not intensities.
        suspects = [s for s in st.comparison if s.suspect]
        distinct = {s.gain_summary for s in st.comparison if s.gain_summary}
        if suspects:
            self.banner.show(
                "comparison includes mixed-gain series",
                "; ".join(f"{s.peak} {s.temperature_K}K {s.polarisation} "
                          f"({s.gain_summary})" for s in suspects)
                + "\nThese were not normalised. Intensity comparisons "
                  "involving them are not meaningful.")
            self.banner.grid()
        elif len(distinct) > 1:
            self.banner.show(
                "series compared at different gains",
                "  |  ".join(sorted(distinct))
                + "\nAll normalised, so this is fine -- but be aware the "
                  "raw counts were not on the same scale.", GREEN)
            self.banner.grid()
        else:
            self.banner.grid_remove()
        self.replot()

    def _group_key(self, s: A.SeriesSummary) -> str:
        mode = self.group_var.get()
        if mode == "resonance":
            return f"{s.resonance}" + (f" ({s.edge})" if s.edge else "")
        if mode == "field":
            return s.field_state
        if mode == "peak":
            return s.peak
        return f"{s.temperature_K} K"

    def replot(self):
        st = self.state
        if not st.comparison:
            self.plot.clear("Add fitted series from Stage 3b", xlabel="P₁′")
            return
        ax = self.plot.axes(xlabel="P₁′  (charge-like → positive, magnetic-like → negative)",
                            ylabel="", title="Stage 4 · comparison")
        groups: Dict[str, List[A.SeriesSummary]] = {}
        for s in st.comparison:
            groups.setdefault(self._group_key(s), []).append(s)

        palette = DATA_CAT
        y = 0
        ticks, labels = [], []
        for gi, (gname, members) in enumerate(sorted(groups.items())):
            colour = palette[gi % len(palette)]
            for s in members:
                ax.errorbar([s.fit.p1], [y], xerr=[s.fit.p1_err], fmt="o", ms=7,
                            color=colour, ecolor=colour, elinewidth=1.4,
                            capsize=4, mfc=BG_CARD, mew=1.6)
                ticks.append(y)
                labels.append(f"{s.peak} {s.temperature_K}K {s.polarisation}")
                y += 1
            ax.axhspan(y - len(members) - 0.5, y - 0.5, color=colour, alpha=0.06)
            ax.text(0.995, (y - len(members) / 2.0 - 0.5), f" {gname}",
                    transform=ax.get_yaxis_transform(), ha="right", va="center",
                    color=colour, fontsize=8, alpha=0.9)
            y += 0.6
        ax.axvline(0.0, color=T3, lw=1.0, ls="--")
        ax.set_yticks(ticks)
        ax.set_yticklabels(labels, fontsize=7.5, color=T2)
        ax.set_ylim(-1, max(y, 1))
        self.plot.draw()

    def save(self):
        st = self.state
        if not st.comparison:
            self.log("Nothing to save — add some fitted series first.", AMBER)
            return
        try:
            out = st.processed_root
            p = A.save_comparison(st.comparison, out, fmt=st.panda_cfg.out_format)
            self.log(f"Stage 4: saved {p}", GREEN)
        except OSError as exc:
            self.log(f"Save failed: {exc}", RED)

    def _on_state(self, what):
        if what == "comparison":
            self.refresh()


# =============================================================================
# HELPERS
# =============================================================================

def _fmt(v, dp=3):
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not math.isfinite(f):
        return "—"
    return f"{f:.{dp}f}"


def _fmt_g(v, sig=4):
    """
    Significant figures, not decimal places.

    Normalised intensities and their errors run from ~1e-4 to ~1e2 depending on
    the detector and the monitor level, so a fixed number of decimal places
    prints a perfectly good error bar as '0.000'.
    """
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not math.isfinite(f):
        return "—"
    return f"{f:.{sig}g}"


def _sortable(v):
    try:
        return (0, float(v))
    except (TypeError, ValueError):
        return (1, 0.0)


PANDA_TABS = [
    ("Stage 1 · Reduce", ReduceTab),
    ("Stage 3a · τ-curve", TauCurveTab),
    ("Stage 3b · Stokes", StokesTab),
    ("Stage 4 · Physics", PhysicsTab),
]
