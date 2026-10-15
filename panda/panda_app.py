# =============================================================================
# PANDA  --  application shell
# Diamond Light Source, beamline I10 / RASOR
# =============================================================================
# Run:  python panda_app.py        (or double-click run_panda.bat)
#
# One window, five flat tabs:
#
#     Scan Log   -- phase 1, the beamtime logger, unchanged
#     Stage 1    -- reduce
#     Stage 3a   -- tau curve
#     Stage 3b   -- Stokes fit
#     Stage 4    -- physics read-outs
#
# The Scan Log tab is the phase-1 RSXDLoggerFrame, imported and embedded
# exactly as its docstring said it could be -- no edit to any phase-1 file.
#
# Local tkinter + matplotlib on one machine. No web server, no remote access,
# no authentication, no build step.
# =============================================================================

from __future__ import annotations

import sys
import traceback
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk

import rsxd_analysis as A
import rsxd_panda as P
from rsxd_core import VERSION as CORE_VERSION, Config
from rsxd_logger import (
    AMBER, BG_BASE, BG_CARD, BG_PANEL, BORDER, CYAN, GREEN, RED, T1, T2, T3,
    Fonts, RSXDLoggerFrame, Tooltip, section_header, shorten_path,
)

APP_DIR = Path(__file__).resolve().parent
LOGGER_CONFIG = APP_DIR / "rsxd_config.toml"
PANDA_CONFIG = APP_DIR / "panda_config.toml"

VERSION = "v2026-08-08-gain-tracking"


class PandaApp(tk.Frame):
    """The whole application: phase-1 logger plus PANDA's four analysis tabs."""

    def __init__(self, parent, logger_config=LOGGER_CONFIG,
                 panda_config=PANDA_CONFIG):
        super().__init__(parent, bg=BG_BASE)
        self.fonts = Fonts()

        # Fail loudly at startup, before any window is built, exactly as the
        # logger does -- a bad config should say so, not misbehave later.
        self.logger_cfg = Config.load(logger_config)
        self.panda_cfg = A.PandaConfig.load(panda_config)

        self._build_header()
        self._build_log()          # built before the tabs: they log while building

        self.state = P.PandaState(logger_cfg=self.logger_cfg,
                                  panda_cfg=self.panda_cfg,
                                  log=self.log)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(side="top", fill="both", expand=True, padx=10, pady=(8, 0))
        self._style_notebook()

        # ── Tab 0: phase 1, embedded untouched ──────────────────────────────
        logger_page = tk.Frame(self.notebook, bg=BG_BASE)
        self.notebook.add(logger_page, text="  Scan Log  ")
        # show_header=False: this window already has a title bar.
        # log_sink: one log for the whole app, not two stacked panels.
        self.logger = RSXDLoggerFrame(logger_page, config_path=logger_config,
                                      show_header=False,
                                      log_sink=self._logger_log)
        self.logger.pack(fill="both", expand=True)

        # ── Tabs 1-4: PANDA ─────────────────────────────────────────────────
        self.tabs = []
        for title, cls in P.PANDA_TABS:
            page = tk.Frame(self.notebook, bg=BG_BASE)
            self.notebook.add(page, text=f"  {title}  ")
            tab = cls(page, self.state)
            tab.pack(fill="both", expand=True)
            self.tabs.append(tab)

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_change)
        self._highlight_stage(0)

        self.log(f"PANDA {VERSION}")
        self.log(f"  analysis {A.VERSION}   gui {P.VERSION}   core {CORE_VERSION}")
        self.log(f"  logger config  {self.logger_cfg.path}")
        self.log(f"  panda config   {self.panda_cfg.path}")
        self._report_paths()

    # ------------------------------------------------------------------------
    def _style_notebook(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TNotebook", background=BG_BASE, borderwidth=0,
                        tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background=BG_PANEL, foreground=T2,
                        padding=(14, 7), borderwidth=0, font=self.fonts.sec)
        style.map("TNotebook.Tab",
                  background=[("selected", BG_CARD)],
                  foreground=[("selected", CYAN)],
                  expand=[("selected", (0, 0, 0, 0))])

    def _build_header(self):
        """
        The ONE title bar for the whole window.

        The Scan Log tab used to draw a second one immediately below this,
        which together with the two log panels cost about a quarter of the
        vertical space and said nothing the tab strip did not already say.
        RSXDLoggerFrame is now constructed with show_header=False.
        """
        hdr = tk.Frame(self, bg=BG_PANEL, height=64)
        hdr.pack(side="top", fill="x")
        hdr.pack_propagate(False)
        tk.Frame(self, bg=BORDER, height=1).pack(side="top", fill="x")

        logo = tk.Frame(hdr, bg=BG_PANEL)
        logo.pack(side="left", padx=(16, 18), pady=10)
        tk.Label(logo, text="PANDA", font=self.fonts.h1, fg=T1,
                 bg=BG_PANEL, anchor="w").pack(anchor="w")
        tk.Label(logo, text="RSXD logging + polarisation analysis · I10 / RASOR",
                 font=self.fonts.tiny, fg=T2, bg=BG_PANEL,
                 anchor="w").pack(anchor="w")

        tk.Frame(hdr, bg=BORDER, width=1).pack(side="left", fill="y", pady=14)

        # ── stage rail ──────────────────────────────────────────────────────
        # Monochrome. The rail shows WHERE YOU ARE in the pipeline, which is
        # exactly one bit of state, so exactly one thing is highlighted: the
        # current stage in T1 over a 2px accent underline. Giving each stage
        # its own hue made four permanent colours that encoded nothing.
        self._rail = {}
        flow = tk.Frame(hdr, bg=BG_PANEL)
        flow.pack(side="left", padx=4)
        steps = [("1", "REDUCE"), ("3a", "τ-CURVE"), ("3b", "STOKES"),
                 ("4", "PHYSICS")]
        for i, (n, name) in enumerate(steps):
            if i:
                tk.Label(flow, text="→", font=self.fonts.ui, fg=T3,
                         bg=BG_PANEL).pack(side="left", padx=6)
            cell = tk.Frame(flow, bg=BG_PANEL)
            cell.pack(side="left")
            num = tk.Label(cell, text=n, font=self.fonts.mono_sm, fg=T3,
                           bg=BG_PANEL)
            num.pack()
            lab = tk.Label(cell, text=name, font=self.fonts.sec, fg=T2,
                           bg=BG_PANEL)
            lab.pack()
            rule = tk.Frame(cell, bg=BG_PANEL, height=2)
            rule.pack(fill="x", pady=(2, 0))
            self._rail[i + 1] = (num, lab, rule)

        # ── paths ───────────────────────────────────────────────────────────
        # Ellipsised from the left so the folder name survives; full path in a
        # tooltip so nothing is actually lost.
        paths = tk.Frame(hdr, bg=BG_PANEL)
        paths.pack(side="right", padx=16)
        self.path_labels = {}
        for key, caption in (("raw", "raw_data"), ("proc", "processed_data")):
            row = tk.Frame(paths, bg=BG_PANEL)
            row.pack(anchor="e")
            tk.Label(row, text=caption, font=self.fonts.tiny, fg=T3,
                     bg=BG_PANEL).pack(side="left", padx=(0, 8))
            v = tk.Label(row, text="", font=self.fonts.mono_sm, fg=T2,
                         bg=BG_PANEL, anchor="e")
            v.pack(side="left")
            self.path_labels[key] = v
        Tooltip(self.path_labels["raw"], lambda: str(self.state.raw_root))
        Tooltip(self.path_labels["proc"], lambda: str(self.state.processed_root))

    def _highlight_stage(self, stage):
        """One stage lit at a time -- the rail's only job."""
        for i, (num, lab, rule) in self._rail.items():
            active = (i == stage)
            num.configure(fg=T2 if active else T3)
            lab.configure(fg=T1 if active else T2)
            rule.configure(bg=CYAN if active else BG_PANEL)

    def _build_log(self):
        panel = tk.Frame(self, bg=BG_PANEL, highlightthickness=1,
                         highlightbackground=BORDER)
        panel.pack(side="bottom", fill="x", padx=10, pady=10)

        top = tk.Frame(panel, bg=BG_PANEL)
        top.pack(fill="x", padx=12, pady=(8, 0))
        h, _ = section_header(top, "Log", self.fonts)
        h.pack(side="left")
        tk.Label(top, text="Stage 1 → 3a → 3b → 4   ·   each stage saves its own output",
                 font=self.fonts.tiny, fg=T3, bg=BG_PANEL).pack(side="right")
        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x", padx=12, pady=(4, 6))

        wrap = tk.Frame(panel, bg=BG_CARD)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self.log_text = tk.Text(wrap, height=7, bg=BG_CARD, fg=T2,
                                font=self.fonts.mono_sm, relief="flat", bd=0,
                                highlightthickness=0, wrap="none",
                                state="disabled")
        self.log_text.pack(side="left", fill="both", expand=True)
        sb = tk.Scrollbar(wrap, command=self.log_text.yview, bg=BG_CARD,
                          troughcolor=BG_PANEL, relief="flat", bd=0, width=8)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set)
        for name, colour in (("cyan", CYAN), ("green", GREEN), ("amber", AMBER),
                             ("red", RED), ("dim", T3), ("src", T3)):
            self.log_text.tag_configure(name, foreground=colour)

    def _logger_log(self, msg, colour=None):
        """Scan Log tab messages, tagged with their source in the shared log."""
        self.log(msg, colour, source="scan-log")

    def log(self, msg, colour=None, source="panda"):
        tag = {CYAN: "cyan", GREEN: "green", AMBER: "amber", RED: "red",
               T3: "dim"}.get(colour, "")
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp}  ", "dim")
        self.log_text.insert("end", f"{source:<8s}  ", "src")
        self.log_text.insert("end", f"{msg}\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    _last_paths = None

    def _report_paths(self):
        raw = self.state.raw_root
        proc = self.state.processed_root
        # Only narrate a genuine change: this runs on every tab switch, and a
        # log that repeats itself trains you to stop reading it.
        signature = (str(raw), str(proc))
        quiet = (signature == self._last_paths)
        self._last_paths = signature
        self.path_labels["raw"].configure(text=shorten_path(raw))
        self.path_labels["proc"].configure(text=shorten_path(proc))
        if quiet:
            return
        if raw is None:
            self.log("raw_data is not set. Set dat_dir in rsxd_config.toml "
                     "(the Scan Log tab can do it), or raw_dir in "
                     "panda_config.toml.", AMBER)
        elif not Path(raw).is_dir():
            self.log(f"raw_data {raw} is not reachable. PANDA will report every "
                     f".dat as missing until it is.", AMBER)
        else:
            self.log(f"raw_data       {raw}", GREEN)
        self.log(f"processed_data {proc}"
                 f"{'' if Path(proc).exists() else '  (will be created on first save)'}")

    def _on_tab_change(self, _event=None):
        """Keep PANDA's view of the logger's paths current when you switch tabs."""
        try:
            idx = self.notebook.index(self.notebook.select())
        except tk.TclError:
            return
        # Rail highlight follows the tab: tab 0 is the Scan Log (no stage).
        self._highlight_stage(idx)
        if idx == 0:
            return
        # The Scan Log tab can change dat_dir/output_dir at runtime; PANDA's
        # config object is the same one, so just re-read the derived paths.
        self._report_paths()

    def on_close(self):
        try:
            self.logger.on_close()
        except Exception:                                  # noqa: BLE001
            traceback.print_exc()


def main():
    print(f"PANDA {VERSION}   analysis {A.VERSION}   gui {P.VERSION}   "
          f"core {CORE_VERSION}")

    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    root.title("PANDA  —  RSXD logging + polarisation analysis  —  I10 / RASOR")
    root.configure(bg=BG_BASE)
    root.minsize(1100, 760)

    try:
        app = PandaApp(root)
    except Exception as exc:                               # noqa: BLE001
        traceback.print_exc()
        from tkinter import messagebox
        messagebox.showerror(
            "PANDA — startup failed",
            f"{exc}\n\nThis is almost always a config problem.\n"
            f"  {LOGGER_CONFIG}\n  {PANDA_CONFIG}")
        raise SystemExit(1)

    app.pack(fill="both", expand=True)
    app.logger.install_shortcuts(root)

    root.update_idletasks()
    w = min(max(app.winfo_reqwidth() + 20, 1400), root.winfo_screenwidth() - 16)
    h = min(max(app.winfo_reqheight() + 20, 950), root.winfo_screenheight() - 80)
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
