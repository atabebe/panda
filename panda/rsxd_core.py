# =============================================================================
# RSXD SCAN LOGGER  --  data layer
# Diamond Light Source, beamline I10 / RASOR
# =============================================================================
# Pure Python. NO tkinter, NO GUI imports anywhere in this file.
#
# Phase 2 (analysis + plotting UI) imports this module directly:
#     from rsxd_core import Config, Session, CSV_COLUMNS
# and reads the cumulative CSV whose schema is frozen below.
#
# Everything the GUI needs is a method on Config / Session. If you find
# yourself putting experiment logic in the GUI file, it belongs here instead.
# =============================================================================

from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import tomllib                      # Python 3.11+
except ModuleNotFoundError:             # pragma: no cover
    import tomli as tomllib             # type: ignore


# =============================================================================
# VERSION STAMP
# =============================================================================
# Single source of truth for "which build is this?". Printed as the first log
# line at startup so a glance at the console tells you what is running.
# Bump on every change. Convention: v<date>-<short-label>.
VERSION = "v2026-08-08-gain-tracking"


# =============================================================================
# CSV SCHEMA  --  the phase-1 / phase-2 contract
# =============================================================================
# This is the contract between phase 1 (logging) and phase 2 (analysis), and
# between this app and the user's own analysis scripts.
#
#   Treat it as frozen. Adding a column is a schema change and needs a
#   migration path (see HISTORY below); reordering or renaming one is not
#   allowed at all, because scripts outside this repo read it positionally.
#
#   peak                 peak label exactly as it appears in the config
#   temperature_K        canonical numeric string, e.g. "300"
#   analyser_angle_deg   canonical numeric string, e.g. "-45"
#   polarisation         one of the polarisation row labels, e.g. "p"
#   scan_number          integer as string, e.g. "64717"
#   note                 free text, tabs/newlines stripped, may be empty
#   gain                 detector transimpedance gain in V/A, as the operator
#                        recorded it, e.g. "1e+08". EMPTY = not recorded.
#                        Empty means UNKNOWN and must never be read as 1.0.
#   dat_found            "yes" | "no" | "unknown"   ("unknown" = check disabled)
#   timestamp            ISO 8601 local time, seconds resolution
#
# One row per logged scan. Sorted by peak, temperature, angle, polarisation --
# stable ordering means a diff of two exports is readable.
#
# ---- HISTORY ----------------------------------------------------------------
# v1  peak, temperature_K, analyser_angle_deg, polarisation, scan_number, note,
#     dat_found, timestamp
# v2  `gain` inserted after `note`.
#
#     WHY: the RASOR photodiode current passes through a transimpedance
#     amplifier whose feedback resistance is operator-selectable, and the
#     instrument writes NO record of it into the .dat file. The number in the
#     file is photocurrent x gain. Two scans taken at different gains are in
#     different units and look identical on the page -- a decade of gain and a
#     decade of physics are indistinguishable. This log is the only place that
#     record can exist.
#
#     MIGRATION: a v1 CSV has no `gain` column. Readers MUST accept that and
#     load those rows with gain = None (unknown), never 1.0. The next save
#     rewrites the file in v2. Everything else about a v1 file is unchanged, so
#     migration is purely additive and cannot lose data.
# =============================================================================
CSV_COLUMNS = [
    "peak",
    "temperature_K",
    "analyser_angle_deg",
    "polarisation",
    "scan_number",
    "note",
    "gain",
    "dat_found",
    "timestamp",
]

# Columns a reader may find missing in an older file, and the value to use when
# they are. Anything NOT in here is genuinely required.
CSV_OPTIONAL_COLUMNS = {"gain": None}

DAT_FOUND_YES = "yes"
DAT_FOUND_NO = "no"
DAT_FOUND_UNKNOWN = "unknown"

SESSION_FILENAME = "rsxd_session.json"
SESSION_VERSION = 1


# =============================================================================
# HELPERS
# =============================================================================

def fmt_num(x) -> str:
    """
    Canonical string form of a temperature or an angle.

    300.0 -> "300",  -7.5 -> "-7.5". Used for dict keys, CSV cells and
    filenames so that the same physical value always produces the same string
    no matter whether it arrived from TOML as an int or a float.
    """
    f = float(x)
    if f == int(f):
        return str(int(f))
    return f"{f:g}"


def _atomic_write(path: Path, text: str, encoding: str = "utf-8") -> None:
    """
    Write a file so that it is either the old content or the new content,
    never a half-written mess. Matters because this runs during a beamtime
    on a machine that may lose power or be shut mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _clean_note(text: str) -> str:
    """Notes live in a CSV cell and an xlsx cell. Kill anything that breaks either."""
    if not text:
        return ""
    return " ".join(str(text).replace("\t", " ").replace("\r", " ").replace("\n", " ").split())


# =============================================================================
# CONFIG
# =============================================================================

@dataclass
class Config:
    """
    Everything that changes between experiments. Loaded from a plain-text TOML
    file at startup. Nothing in this dataclass is hardcoded elsewhere.
    """
    path: Path

    angles: List[str]                       # canonical strings, grid column order
    temperatures: List[str]                 # canonical strings, selector order
    peaks: Dict[str, str]                   # label -> filename string, ordered
    pol_groups: Dict[str, List[str]]        # group label -> row labels, ordered

    output_dir: Path
    dat_dir: str                            # raw string; may be "" or invalid
    dat_patterns: List[str]
    csv_filename: str

    # ---- derived ------------------------------------------------------------
    @property
    def pol_labels(self) -> List[str]:
        """Flat row order: p, n, LH, LV -- exactly the workbook row order."""
        out: List[str] = []
        for labels in self.pol_groups.values():
            out.extend(labels)
        return out

    def group_of(self, pol_label: str) -> str:
        for group, labels in self.pol_groups.items():
            if pol_label in labels:
                return group
        return ""

    def peak_file_string(self, peak_label: str) -> str:
        return self.peaks.get(peak_label, peak_label)

    # ---- load ---------------------------------------------------------------
    @classmethod
    def load(cls, path) -> "Config":
        path = Path(path)
        with open(path, "rb") as f:
            raw = tomllib.load(f)

        grid = raw.get("grid", {})
        angles = [fmt_num(a) for a in grid.get("analyser_angles_deg", [])]
        temps = [fmt_num(t) for t in grid.get("temperatures_K", [])]

        peaks = {str(k): str(v) for k, v in raw.get("peaks", {}).items()}
        pol_groups = {
            str(k): [str(x) for x in v]
            for k, v in raw.get("polarisation_groups", {}).items()
        }

        paths = raw.get("paths", {})
        out_raw = str(paths.get("output_dir", "") or "").strip()
        output_dir = Path(out_raw) if out_raw else (path.parent / "output")

        dat_dir = str(paths.get("dat_dir", "") or "").strip()
        dat_patterns = [str(p) for p in paths.get("dat_filename_patterns", ["i10-{scan}.dat"])]

        csv_filename = str(raw.get("csv", {}).get("filename", "rsxd_scan_log.csv"))

        cfg = cls(
            path=path,
            angles=angles,
            temperatures=temps,
            peaks=peaks,
            pol_groups=pol_groups,
            output_dir=output_dir,
            dat_dir=dat_dir,
            dat_patterns=dat_patterns,
            csv_filename=csv_filename,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Fail loudly at startup rather than silently mid-beamtime."""
        if not self.angles:
            raise ValueError("config: grid.analyser_angles_deg is empty")
        if not self.temperatures:
            raise ValueError("config: grid.temperatures_K is empty")
        if not self.peaks:
            raise ValueError("config: [peaks] is empty")
        if not self.pol_groups:
            raise ValueError("config: [polarisation_groups] is empty")
        if not any("{scan}" in p for p in self.dat_patterns):
            raise ValueError("config: no dat_filename_patterns entry contains {scan}")
        for label in list(self.peaks) + self.pol_labels:
            if "\x1f" in label:
                raise ValueError(f"config: label {label!r} contains a reserved character")

    # ---- persist directory choices back into the config file ----------------
    def set_paths(self, output_dir: Optional[str] = None,
                  dat_dir: Optional[str] = None) -> None:
        """
        Rewrite just the output_dir / dat_dir lines in the TOML file, in place.

        Deliberately a targeted line edit rather than a full re-serialise: it
        preserves every comment and every hand-made formatting choice in the
        file, which matters because the user edits this file by hand.
        """
        if output_dir is not None:
            self.output_dir = Path(output_dir)
        if dat_dir is not None:
            self.dat_dir = dat_dir

        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return

        def _sub(t: str, key: str, value: str) -> str:
            # TOML literal string: single quotes, no backslash escaping.
            safe = str(value).replace("'", "")
            line = f"{key} = '{safe}'"
            pattern = re.compile(rf"^(?P<indent>[ \t]*){re.escape(key)}\s*=.*$", re.M)
            if pattern.search(t):
                return pattern.sub(lambda m: m.group("indent") + line, t, count=1)
            return t

        if output_dir is not None:
            text = _sub(text, "output_dir", str(self.output_dir))
        if dat_dir is not None:
            text = _sub(text, "dat_dir", str(self.dat_dir))

        _atomic_write(self.path, text)


# =============================================================================
# .dat PRESENCE CHECK
# =============================================================================

class DatChecker:
    """
    Answers "is the raw file for scan N on disk yet?".

    Deliberately unexcitable. If the directory is unset, missing, or otherwise
    unreadable the checker reports itself disabled and every answer is
    DAT_FOUND_UNKNOWN. It never raises, never blocks and never pops anything up
    -- "not found" is the normal state for the first hour after a scan.
    """

    def __init__(self, dat_dir: str, patterns: List[str]):
        self.dat_dir = dat_dir or ""
        self.patterns = list(patterns)

    @property
    def enabled(self) -> bool:
        try:
            return bool(self.dat_dir) and os.path.isdir(self.dat_dir)
        except OSError:
            return False

    def filenames_for(self, scan: int) -> List[str]:
        return [p.replace("{scan}", str(scan)) for p in self.patterns]

    def check(self, scan: Optional[int]) -> str:
        """-> DAT_FOUND_YES / DAT_FOUND_NO / DAT_FOUND_UNKNOWN. Never raises."""
        if scan is None or not self.enabled:
            return DAT_FOUND_UNKNOWN
        try:
            for name in self.filenames_for(scan):
                if os.path.isfile(os.path.join(self.dat_dir, name)):
                    return DAT_FOUND_YES
            return DAT_FOUND_NO
        except OSError:
            return DAT_FOUND_UNKNOWN

    def snapshot(self) -> Optional[set]:
        """
        One directory listing, lowercased, for bulk re-checks. Much faster than
        stat-ing several hundred candidate names one at a time.
        Returns None if the check is disabled.
        """
        if not self.enabled:
            return None
        try:
            with os.scandir(self.dat_dir) as it:
                return {e.name.lower() for e in it if e.is_file()}
        except OSError:
            return None

    def check_against(self, snapshot: Optional[set], scan: Optional[int]) -> str:
        if scan is None or snapshot is None:
            return DAT_FOUND_UNKNOWN
        for name in self.filenames_for(scan):
            if name.lower() in snapshot:
                return DAT_FOUND_YES
        return DAT_FOUND_NO


# =============================================================================
# ENTRIES
# =============================================================================

CellKey = Tuple[str, str, str, str]        # (peak, temperature, angle, polarisation)


def fmt_gain(gain: Optional[float]) -> str:
    """
    Canonical string form of a gain. None -> "" (unknown), never "1".

    Written in exponent form because that is how the operator reads it off the
    amplifier: 1e+08, not 100000000.
    """
    if gain is None:
        return ""
    try:
        g = float(gain)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(g):
        return ""
    return f"{g:g}"


def parse_gain(text) -> Optional[float]:
    """
    Operator input -> gain, or None for blank.

    Accepts 1e8, 1E+08, 100000000, 1.0e8. Raises ValueError on anything else --
    a gain that was mistyped must not be silently coerced, because a wrong gain
    is worse than no gain: no gain is flagged as unknown, a wrong one is not.
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    try:
        g = float(s)
    except (TypeError, ValueError):
        raise ValueError(f"not a gain: {text!r}  (expected e.g. 1e8)")
    if not math.isfinite(g) or g <= 0:
        raise ValueError(f"gain must be a positive number, got {text!r}")
    return g


@dataclass
class Entry:
    scan: int
    note: str = ""
    dat_found: str = DAT_FOUND_UNKNOWN
    timestamp: str = ""
    # Detector transimpedance gain in V/A at the time this scan was taken.
    # None means NOT RECORDED. It is never defaulted to 1.0 anywhere.
    gain: Optional[float] = None

    def as_row(self, key: CellKey) -> Dict[str, str]:
        peak, temp, angle, pol = key
        return {
            "peak": peak,
            "temperature_K": temp,
            "analyser_angle_deg": angle,
            "polarisation": pol,
            "scan_number": str(self.scan),
            "note": _clean_note(self.note),
            "gain": fmt_gain(self.gain),
            "dat_found": self.dat_found,
            "timestamp": self.timestamp,
        }


# =============================================================================
# GAIN EPOCHS
# =============================================================================

@dataclass
class GainEpoch:
    """
    A run of consecutive scan numbers sharing one gain.

    gain is None for the UNKNOWN epoch -- scans taken before any gain was
    recorded. Unknown stays unknown: it is never filled in by assumption.
    """
    first_scan: int
    last_scan: int
    gain: Optional[float]

    @property
    def known(self) -> bool:
        return self.gain is not None

    @property
    def label(self) -> str:
        if not self.known:
            return f"unknown   {self.first_scan}-{self.last_scan}"
        return f"{self.first_scan}-{self.last_scan}   {fmt_gain(self.gain)}"

    def contains(self, scan: int) -> bool:
        return self.first_scan <= scan <= self.last_scan


def gain_epochs(entries) -> List[GainEpoch]:
    """
    Resolve logged entries into ordered gain epochs.

    Gain is piecewise-constant and FORWARD-PROPAGATING: a value recorded on
    scan N applies to scan N and to every later scan until the next recorded
    value. That is how the instrument behaves -- the operator turns a dial and
    it stays turned -- so it is how the log has to be read.

    `entries` is any iterable of objects with `.scan` and `.gain` (Entry, or
    the analysis-side SeriesEntry). Returns epochs sorted by scan number, with
    an UNKNOWN epoch first if any scans predate the first recorded gain.
    """
    rows = sorted(((int(e.scan), e.gain) for e in entries if int(e.scan) >= 0),
                  key=lambda t: t[0])
    if not rows:
        return []
    scans = [s for s, _ in rows]

    # Change points: a recorded gain that differs from the one in force.
    # Consecutive re-recordings of the same value are one epoch, not several.
    boundaries: List[Tuple[int, float]] = []
    running: Optional[float] = None
    for scan, gain in rows:
        if gain is None:
            continue
        if running is None or gain != running:
            boundaries.append((scan, gain))
            running = gain

    epochs: List[GainEpoch] = []
    if not boundaries:
        return [GainEpoch(scans[0], scans[-1], None)]

    # Scans before the first recorded gain are unknown -- not the first gain.
    first_known = boundaries[0][0]
    earlier = [s for s in scans if s < first_known]
    if earlier:
        epochs.append(GainEpoch(earlier[0], earlier[-1], None))

    for i, (start, gain) in enumerate(boundaries):
        stop = boundaries[i + 1][0] - 1 if i + 1 < len(boundaries) else scans[-1]
        inside = [s for s in scans if start <= s <= stop]
        if inside:
            epochs.append(GainEpoch(inside[0], inside[-1], gain))
    return epochs


def effective_gain(scan: int, epochs: Sequence[GainEpoch]) -> Optional[float]:
    """
    The gain in force for one scan number, or None if genuinely unknown.

    Resolution is by epoch START, not by the observed scan range. An epoch's
    first_scan/last_scan describe which LOGGED scans fall in it, which is what
    you want to display -- but a scan that was never logged, or logged later,
    still sat under whatever gain was last dialled in. Matching on the observed
    range would report "unknown" for a scan taken between two logged ones, when
    in fact its gain is perfectly well determined.

    Nothing is back-propagated: a scan before the first recorded gain stays
    unknown, because there is no evidence about it either way.
    """
    scan = int(scan)
    answer: Optional[float] = None
    for ep in epochs:
        if ep.first_scan <= scan:
            answer = ep.gain          # None for the unknown epoch, correctly
        else:
            break
    return answer


def boundary_scans(epochs: Sequence[GainEpoch]) -> set:
    """
    Scan numbers where the gain CHANGED -- the first scan of every epoch after
    the first. These are the scans worth marking in the grid: the operator
    turned the dial here, and everything from here on is in new units.
    """
    return {ep.first_scan for ep in epochs[1:]} if len(epochs) > 1 else set()


# =============================================================================
# SESSION  --  the whole beamtime's state
# =============================================================================

class Session:
    """
    The in-memory model plus everything that writes to disk.

    Storage layout, all inside output_dir:
        rsxd_session.json   authoritative state; restored on startup
        rsxd_scan_log.csv   cumulative long-format CSV (the phase-2 interface)
        <Peak>_<T>K_<a>-<b>.xlsx    one workbook per (peak, temperature)

    The JSON is the source of truth. The CSV and the workbooks are derived from
    it and are rewritten wholesale, which is what guarantees that correcting a
    typo actually removes the wrong value everywhere instead of leaving it
    behind in an append-only trail.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.entries: Dict[CellKey, Entry] = {}
        self.ui_peak: str = next(iter(cfg.peaks), "")
        self.ui_temp: str = cfg.temperatures[0] if cfg.temperatures else ""
        self.checker = DatChecker(cfg.dat_dir, cfg.dat_patterns)

    # ---- paths --------------------------------------------------------------
    @property
    def output_dir(self) -> Path:
        return self.cfg.output_dir

    @property
    def session_path(self) -> Path:
        return self.output_dir / SESSION_FILENAME

    @property
    def csv_path(self) -> Path:
        return self.output_dir / self.cfg.csv_filename

    def rebind_paths(self, output_dir: Optional[str] = None,
                     dat_dir: Optional[str] = None) -> None:
        """Directories changed in the GUI. Persist them and rebuild the checker."""
        self.cfg.set_paths(output_dir=output_dir, dat_dir=dat_dir)
        self.checker = DatChecker(self.cfg.dat_dir, self.cfg.dat_patterns)

    # ---- editing ------------------------------------------------------------
    def key(self, angle: str, pol: str,
            peak: Optional[str] = None, temp: Optional[str] = None) -> CellKey:
        return (peak or self.ui_peak, temp or self.ui_temp, angle, pol)

    def get(self, key: CellKey) -> Optional[Entry]:
        return self.entries.get(key)

    def set_scan(self, key: CellKey, scan_text,
                 gain: Optional[float] = None) -> Optional[Entry]:
        """
        Set (or clear) the scan number in one cell.

        Empty / blank text clears the cell but KEEPS nothing behind -- an empty
        cell is a normal, expected state, not an error. Returns the resulting
        Entry, or None if the cell was cleared. Raises ValueError only if the
        text is genuinely not a scan number.
        """
        text = str(scan_text).strip()
        if not text:
            existing = self.entries.pop(key, None)
            # Clearing a mistyped number must not take the note with it.
            # "beam dump, repeated" is exactly the note you write BEFORE you
            # know the replacement scan number.
            if existing is not None and existing.note:
                self.entries[key] = Entry(scan=-1, note=existing.note,
                                          dat_found=DAT_FOUND_UNKNOWN,
                                          timestamp=existing.timestamp,
                                          gain=existing.gain)
            return None
        if not text.isdigit():
            raise ValueError(f"not a scan number: {text!r}")
        scan = int(text)

        prev = self.entries.get(key)
        note = prev.note if prev else ""
        # An explicit gain wins; otherwise keep whatever this cell already had.
        # Neither path ever invents a value -- None stays None.
        if gain is None and prev is not None:
            gain = prev.gain
        entry = Entry(
            scan=scan,
            note=note,
            dat_found=self.checker.check(scan),
            timestamp=datetime.now().isoformat(timespec="seconds"),
            gain=gain,
        )
        self.entries[key] = entry
        return entry

    def set_note(self, key: CellKey, note: str) -> None:
        """
        Notes attach to a cell, not to a scan number, and survive the cell
        being empty -- "beam dump, repeated" is worth keeping even before the
        replacement scan number is known.
        """
        note = _clean_note(note)
        entry = self.entries.get(key)
        if entry is None:
            if not note:
                return
            entry = Entry(scan=-1, note=note, dat_found=DAT_FOUND_UNKNOWN,
                          timestamp=datetime.now().isoformat(timespec="seconds"),
                          gain=None)
            self.entries[key] = entry
        else:
            entry.note = note

    def clear(self, key: CellKey) -> None:
        self.entries.pop(key, None)

    def _real(self, entry: Optional[Entry]) -> bool:
        """A note-only placeholder has scan == -1 and is not a logged scan."""
        return entry is not None and entry.scan >= 0

    # ---- queries the GUI needs ---------------------------------------------
    def cells_for(self, peak: str, temp: str) -> Dict[Tuple[str, str], Entry]:
        """{(angle, pol): Entry} for one workbook."""
        return {
            (k[2], k[3]): v
            for k, v in self.entries.items()
            if k[0] == peak and k[1] == temp
        }

    def scans_for(self, peak: str, temp: str) -> List[int]:
        return sorted(e.scan for k, e in self.entries.items()
                      if k[0] == peak and k[1] == temp and self._real(e))

    def fill_counts(self, peak: str, temp: str) -> Dict[str, int]:
        """{angle: number of the 4 polarisation channels filled}."""
        counts = {a: 0 for a in self.cfg.angles}
        for k, e in self.entries.items():
            if k[0] == peak and k[1] == temp and self._real(e) and k[2] in counts:
                counts[k[2]] += 1
        return counts

    def empty_count(self, peak: str, temp: str) -> int:
        total = len(self.cfg.angles) * len(self.cfg.pol_labels)
        return total - sum(self.fill_counts(peak, temp).values())

    def epochs_for(self, peak: str, temp: str) -> List[GainEpoch]:
        """Gain epochs within ONE tau-series -- what the Scan Log tab shows."""
        rows = [e for k, e in self.entries.items()
                if k[0] == peak and k[1] == temp and self._real(e)]
        return gain_epochs(rows)

    def session_epochs(self) -> List[GainEpoch]:
        """
        Gain epochs across the WHOLE session.

        Gain propagates by scan number, not by workbook: the operator turns the
        dial once and it stays turned across peaks and temperatures. Resolving
        a single scan's gain therefore has to look at every logged scan, not
        just the ones in its own grid.
        """
        return gain_epochs([e for e in self.entries.values() if self._real(e)])

    def gain_of(self, scan: int) -> Optional[float]:
        return effective_gain(scan, self.session_epochs())

    def unknown_gain_count(self, peak: str, temp: str) -> int:
        eps = self.session_epochs()
        return sum(1 for k, e in self.entries.items()
                   if k[0] == peak and k[1] == temp and self._real(e)
                   and effective_gain(e.scan, eps) is None)

    def duplicates(self) -> Dict[int, List[CellKey]]:
        """
        Every scan number that appears in more than one cell, ANYWHERE in the
        session -- across peaks and temperatures, not just the current grid.
        A repeated scan number is almost always a typo and is silent damage.
        """
        seen: Dict[int, List[CellKey]] = {}
        for k, e in self.entries.items():
            if self._real(e):
                seen.setdefault(e.scan, []).append(k)
        return {scan: keys for scan, keys in seen.items() if len(keys) > 1}

    def total_scans(self) -> int:
        return sum(1 for e in self.entries.values() if self._real(e))

    def missing_dat(self) -> List[Tuple[CellKey, Entry]]:
        return [(k, e) for k, e in self.entries.items()
                if self._real(e) and e.dat_found == DAT_FOUND_NO]

    # ---- sequential fill ----------------------------------------------------
    def plan_sequential(self, peak: str, temp: str, start_scan: int, mode: str,
                        angle: Optional[str] = None, pol: Optional[str] = None,
                        step: int = 1, overwrite: bool = False
                        ) -> List[Tuple[CellKey, int]]:
        """
        Work out what a sequential fill WOULD do, without doing it, so the GUI
        can show a preview and the user can back out.

        mode:
            "column"  one analyser angle, down the four polarisation channels
            "row"     one polarisation channel, across every analyser angle
            "block"   the whole grid, column by column (matches the workflow:
                      four channels at one angle, then move to the next angle)

        Order within a column is the config's polarisation row order; order of
        columns is the config's angle order. If the real measurement order
        differed, every filled cell stays editable afterwards.
        """
        angles = self.cfg.angles
        pols = self.cfg.pol_labels

        if mode == "column":
            targets = [(angle, p) for p in pols]
        elif mode == "row":
            targets = [(a, pol) for a in angles]
        elif mode == "block":
            targets = [(a, p) for a in angles for p in pols]
        else:
            raise ValueError(f"unknown fill mode {mode!r}")

        plan: List[Tuple[CellKey, int]] = []
        n = start_scan
        for a, p in targets:
            if a is None or p is None:
                continue
            k = (peak, temp, a, p)
            occupied = self._real(self.entries.get(k))
            if occupied and not overwrite:
                continue
            plan.append((k, n))
            n += step
        return plan

    def apply_sequential(self, plan: List[Tuple[CellKey, int]]) -> int:
        for k, n in plan:
            self.set_scan(k, str(n))
        return len(plan)

    # ---- .dat verification --------------------------------------------------
    def verify_all(self) -> Tuple[int, List[Tuple[CellKey, Entry]], bool]:
        """
        Re-check every logged scan against the data directory in one pass.
        Run this after each WinSCP sync.

        -> (n_checked, [(key, entry) still missing], check_was_enabled)
        """
        snap = self.checker.snapshot()
        if snap is None:
            for e in self.entries.values():
                e.dat_found = DAT_FOUND_UNKNOWN
            return (0, [], False)

        checked = 0
        missing: List[Tuple[CellKey, Entry]] = []
        for k, e in self.entries.items():
            if not self._real(e):
                continue
            e.dat_found = self.checker.check_against(snap, e.scan)
            checked += 1
            if e.dat_found == DAT_FOUND_NO:
                missing.append((k, e))
        missing.sort(key=lambda ke: ke[1].scan)
        return (checked, missing, True)

    # ---- persistence --------------------------------------------------------
    def save_session(self) -> None:
        """Autosave. Called on every edit. Atomic, so a crash cannot corrupt it."""
        payload = {
            "version": SESSION_VERSION,
            "app_version": VERSION,
            "saved": datetime.now().isoformat(timespec="seconds"),
            "ui": {"peak": self.ui_peak, "temperature": self.ui_temp},
            "entries": [
                dict(e.as_row(k), scan_number=str(e.scan)) for k, e in
                sorted(self.entries.items())
            ],
        }
        _atomic_write(self.session_path, json.dumps(payload, indent=1))

    def load_session(self) -> bool:
        """Restore exactly where we left off. Returns False if there is nothing."""
        p = self.session_path
        if not p.is_file():
            return False
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt session file must not stop the app from starting during
            # a beamtime. Keep it aside for forensics and carry on empty.
            try:
                shutil.copy2(p, p.with_suffix(".corrupt.json"))
            except OSError:
                pass
            return False

        self.entries.clear()
        for row in payload.get("entries", []):
            try:
                key: CellKey = (
                    str(row["peak"]),
                    str(row["temperature_K"]),
                    str(row["analyser_angle_deg"]),
                    str(row["polarisation"]),
                )
                # A session file written before gain tracking simply has no
                # "gain" key; .get() leaves it None, which is exactly right.
                self.entries[key] = Entry(
                    scan=int(row["scan_number"]),
                    note=str(row.get("note", "")),
                    dat_found=str(row.get("dat_found", DAT_FOUND_UNKNOWN)),
                    timestamp=str(row.get("timestamp", "")),
                    gain=parse_gain(row.get("gain")),
                )
            except (KeyError, ValueError, TypeError):
                continue

        ui = payload.get("ui", {})
        peak = str(ui.get("peak", ""))
        temp = str(ui.get("temperature", ""))
        if peak in self.cfg.peaks:
            self.ui_peak = peak
        if temp in self.cfg.temperatures:
            self.ui_temp = temp
        return True

    # ---- CSV ----------------------------------------------------------------
    def write_csv(self) -> Path:
        """
        Rewrite the cumulative long-format CSV from the current state.

        Rewritten rather than appended so that an edited or deleted scan
        actually disappears. From the outside the effect is the same -- the
        file grows as the beamtime goes on -- but it can never disagree with
        the session.
        """
        rows = [e.as_row(k) for k, e in self.entries.items() if self._real(e)]
        rows.sort(key=lambda r: (
            r["peak"],
            -float(r["temperature_K"]),
            float(r["analyser_angle_deg"]),
            self.cfg.pol_labels.index(r["polarisation"])
            if r["polarisation"] in self.cfg.pol_labels else 99,
        ))

        import io
        buf = io.StringIO(newline="")
        w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        _atomic_write(self.csv_path, buf.getvalue())
        return self.csv_path

    # ---- xlsx ---------------------------------------------------------------
    def workbook_name(self, peak: str, temp: str) -> Optional[str]:
        """
        peak_temperature_firstscan-lastscan.xlsx, e.g. CoTau_300K_64717-64768.xlsx
        First/last are the min and max scan numbers present in that workbook,
        so the name changes as scans are added. None if the grid is empty.
        """
        scans = self.scans_for(peak, temp)
        if not scans:
            return None
        return f"{self.cfg.peak_file_string(peak)}_{temp}K_{scans[0]}-{scans[-1]}.xlsx"

    def _stale_pattern(self, peak: str, temp: str) -> re.Pattern:
        return re.compile(
            rf"^{re.escape(self.cfg.peak_file_string(peak))}_{re.escape(temp)}K_\d+-\d+\.xlsx$",
            re.IGNORECASE,
        )

    def export_xlsx(self, peak: str, temp: str) -> Tuple[Optional[Path], List[str]]:
        """
        Write the workbook for one (peak, temperature) and leave EXACTLY ONE
        file in the output directory for that pair.

        The scan range in the filename moves as scans are added, so every save
        first writes the new name and then removes any older file for the same
        peak and temperature. Three files for one peak+temperature is the
        failure mode this is built to prevent.

        -> (path written or None if the grid is empty, [names removed])
        """
        name = self.workbook_name(peak, temp)
        out = self.output_dir
        out.mkdir(parents=True, exist_ok=True)

        pattern = self._stale_pattern(peak, temp)
        try:
            existing = [f.name for f in out.iterdir()
                        if f.is_file() and pattern.match(f.name)]
        except OSError:
            existing = []

        if name is None:
            return (None, [])

        target = out / name
        self._write_workbook(target, peak, temp)

        removed = []
        for other in existing:
            if other.lower() == name.lower():
                continue
            try:
                (out / other).unlink()
                removed.append(other)
            except OSError:
                pass
        return (target, removed)

    def export_all_xlsx(self) -> List[Tuple[Path, List[str]]]:
        pairs = sorted({(k[0], k[1]) for k, e in self.entries.items() if self._real(e)})
        results = []
        for peak, temp in pairs:
            path, removed = self.export_xlsx(peak, temp)
            if path is not None:
                results.append((path, removed))
        return results

    def _write_workbook(self, path: Path, peak: str, temp: str) -> None:
        """
        Sheet1 reproduces the supplied template layout exactly:

            row 1                     (empty)
            A2                        'Angles of polarization analyzer'
            C2..O2                    the analyser angles
            A3:A4 merged              first polarisation group label
            B3, B4                    its two row labels
            A5:A6 merged              second polarisation group label
            B5, B6                    its two row labels
            C3..O6                    the scan numbers

        The one correction against the template as drawn: the group labels were
        swapped there. p / n are CIRCULAR dichroism, LH / LV are LINEAR. The
        grouping comes from the config, so the labels follow whatever the
        config says.

        Notes go on a separate 'Notes' sheet -- never in the same cell as a
        scan number, and never disturbing the grid layout.
        """
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter

        angles = self.cfg.angles
        cells = self.cells_for(peak, temp)

        wb = Workbook()
        ws = wb.active
        ws.title = "Sheet1"

        hdr_font = Font(bold=True)
        centre = Alignment(horizontal="center", vertical="center")

        ws["A2"] = "Angles of polarization analyzer"
        ws["A2"].font = hdr_font
        for j, a in enumerate(angles):
            c = ws.cell(row=2, column=3 + j)
            c.value = float(a) if "." in a else int(a)
            c.font = hdr_font
            c.alignment = centre

        row = 3
        for group, labels in self.cfg.pol_groups.items():
            first_row = row
            for lab in labels:
                ws.cell(row=row, column=2, value=lab).alignment = centre
                for j, a in enumerate(angles):
                    e = cells.get((a, lab))
                    if e is not None and self._real(e):
                        c = ws.cell(row=row, column=3 + j, value=e.scan)
                        c.alignment = centre
                row += 1
            last_row = row - 1
            gc = ws.cell(row=first_row, column=1, value=group)
            gc.font = hdr_font
            gc.alignment = Alignment(horizontal="left", vertical="center")
            if last_row > first_row:
                ws.merge_cells(start_row=first_row, start_column=1,
                               end_row=last_row, end_column=1)

        # Column widths carried over from the template.
        for col, width in (("A", 25.90625), ("B", 3.6328125), ("C", 15.1796875),
                           ("D", 13.453125), ("E", 17.90625), ("F", 16.08984375),
                           ("G", 14.6328125), ("H", 12.81640625)):
            ws.column_dimensions[col].width = width

        # ── Notes sheet ──────────────────────────────────────────────────────
        nws = wb.create_sheet("Notes")
        nws["A1"] = "Peak"
        nws["B1"] = peak
        nws["A2"] = "Temperature (K)"
        nws["B2"] = float(temp) if "." in temp else int(temp)
        nws["A3"] = "Exported"
        nws["B3"] = datetime.now().isoformat(timespec="seconds")
        for r in (1, 2, 3):
            nws.cell(row=r, column=1).font = hdr_font

        # ---- gain epochs -----------------------------------------------
        # Without this the gain record dies the moment the workbook leaves the
        # app, and the workbook is the archival copy. Epochs are resolved over
        # the WHOLE session, because the amplifier setting propagates by scan
        # number and does not reset at a workbook boundary.
        session_eps = self.session_epochs()
        grid_eps = self.epochs_for(peak, temp)
        nws["A5"] = "Gain epochs (V/A)"
        nws["A5"].font = hdr_font
        r = 6
        if grid_eps:
            for ep in grid_eps:
                nws.cell(row=r, column=1,
                         value=f"{ep.first_scan}-{ep.last_scan}")
                nws.cell(row=r, column=2,
                         value=fmt_gain(ep.gain) if ep.known else "unknown")
                r += 1
        else:
            nws.cell(row=r, column=1, value="(no scans)")
            r += 1
        if sum(1 for ep in grid_eps if ep.known) > 1 or any(
                not ep.known for ep in grid_eps):
            nws.cell(row=r, column=1,
                     value="WARNING: this workbook spans more than one gain "
                           "setting, or contains scans of unknown gain. "
                           "Intensities are not directly comparable.")
            nws.cell(row=r, column=1).font = Font(bold=True)
            r += 1
        r += 1

        head = ["analyser_angle_deg", "polarisation", "scan_number", "gain",
                "note", "dat_found"]
        head_row = r
        for j, h in enumerate(head):
            c = nws.cell(row=head_row, column=1 + j, value=h)
            c.font = hdr_font
        r = head_row + 1
        # Every logged scan appears here, not just annotated ones: gain has to
        # be recoverable for each scan individually, not only where a human
        # happened to leave a note.
        for a in angles:
            for lab in self.cfg.pol_labels:
                e = cells.get((a, lab))
                if e is None or not (self._real(e) or e.note):
                    continue
                g = effective_gain(e.scan, session_eps) if self._real(e) else None
                nws.cell(row=r, column=1, value=float(a) if "." in a else int(a))
                nws.cell(row=r, column=2, value=lab)
                nws.cell(row=r, column=3, value=e.scan if self._real(e) else None)
                nws.cell(row=r, column=4, value=fmt_gain(g) or "unknown")
                nws.cell(row=r, column=5, value=e.note)
                nws.cell(row=r, column=6, value=e.dat_found)
                r += 1
        for col, width in (("A", 20), ("B", 12), ("C", 14), ("D", 12),
                           ("E", 60), ("F", 11)):
            nws.column_dimensions[col].width = width

        path.parent.mkdir(parents=True, exist_ok=True)
        # openpyxl cannot write to a file handle we control atomically, so save
        # beside the target and swap in. Prevents a half-written workbook if the
        # machine dies mid-save.
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".xlsx")
        os.close(fd)
        try:
            wb.save(tmp)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- the one call the GUI makes after every edit ------------------------
    def autosave(self) -> None:
        self.save_session()
        self.write_csv()
