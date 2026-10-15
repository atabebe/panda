# =============================================================================
# PANDA  --  analysis / physics layer          (phase 2 of the RSXD logger)
# Diamond Light Source, beamline I10 / RASOR
# =============================================================================
# Pure Python + numpy/scipy. NO tkinter, NO matplotlib, NO GUI imports.
# Same discipline as rsxd_core.py: everything here is a plain function or a
# dataclass, so it can be unit-tested headless and driven from a script.
#
# The four stages, in order:
#   Stage 1  parse an SRS .dat -> reduce to I = Im/I0 with Poisson errors
#   Stage 3a integrate the peak vs 2theta -> intensity at each analyser angle
#   Stage 3b fit I(tau) = (I0/2)[1 + P1'cos2tau + P2'sin2tau] -> Stokes params
#   Stage 4  compare series (on/off resonance, field on/off, Fe vs Co)
#
# (There is no "Stage 2": the tau-series index that would have been stage 2 is
# already produced by the phase-1 logger's cumulative CSV. Stage numbering is
# kept as the beamline uses it rather than renumbered to look tidy.)
# =============================================================================

from __future__ import annotations

import csv
import io
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# Reuse the logger's atomic writer rather than reimplementing it. One
# implementation means one set of crash semantics across both phases; a second
# copy would drift. rsxd_core imports no GUI code, so this stays headless.
from rsxd_core import (
    CSV_COLUMNS, CSV_OPTIONAL_COLUMNS, Config, DatChecker, GainEpoch,
    _atomic_write, effective_gain, fmt_gain, fmt_num, gain_epochs,
)

try:
    import tomllib                      # Python 3.11+
except ModuleNotFoundError:             # pragma: no cover
    import tomli as tomllib             # type: ignore


# =============================================================================
# VERSION STAMP
# =============================================================================
# Same convention as rsxd_core.VERSION: v<date>-<short-label>. Printed as the
# first log line at startup so a glance tells you what is running.
VERSION = "v2026-08-08-gain-tracking"


# =============================================================================
# ERRORS
# =============================================================================

class SRSParseError(ValueError):
    """The file is not a readable SRS scan."""


class ColumnMapError(ValueError):
    """
    A configured column name is not in the file.

    Deliberately its own exception with the available columns in the message:
    this is the error the operator will actually hit the first time a real
    theta-2theta scan arrives, and "KeyError: 'mcsh17'" would be useless.
    """


# =============================================================================
# STAGE 1a  --  SRS FILE PARSING
# =============================================================================
# Envelope, confirmed against 248 real I10 files:
#
#      &SRS
#      SRSRUN=64951,SRSDAT=20260710,SRSTIM=113017,
#      SRSSTN='BASE',...
#     <MetaDataAtStart>
#     cmd='scan energy 700 720 0.1 mcsh16 0.2 ...'
#     pgm_energy=700.9809
#     pol='pc'
#     ...
#     </MetaDataAtStart>
#      &END
#     energy demand demand_diff pgm_energy pgm_energy_diff mcsh16 mcsh17 ...
#     700.13651536 700.00000000 ... (numeric rows to EOF)
#
# Note the ordering: </MetaDataAtStart> comes BEFORE ` &END`, and the column
# header is the single line immediately after ` &END`.
# =============================================================================

_MARK_SRS = "&SRS"
_MARK_META_OPEN = "<MetaDataAtStart>"
_MARK_META_CLOSE = "</MetaDataAtStart>"
_MARK_END = "&END"


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def _maybe_number(value: str):
    """Return a float where the value is numeric, else the string."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


@dataclass
class SRSScan:
    """One raw SRS scan file, parsed but not yet reduced."""
    path: Path
    scan_number: Optional[int]
    header: Dict[str, str]              # the ' &SRS' preamble (SRSRUN, SRSDAT, ...)
    meta: Dict[str, object]             # <MetaDataAtStart> block, values coerced
    columns: List[str]
    data: np.ndarray                    # shape (n_points, n_columns)

    # ---- convenience accessors, each with a documented fallback chain -------
    def column(self, name: str) -> np.ndarray:
        if name not in self.columns:
            raise ColumnMapError(
                f"{self.path.name}: no column {name!r}. "
                f"This file has: {', '.join(self.columns)}"
            )
        return self.data[:, self.columns.index(name)]

    def meta_float(self, *names: str) -> Optional[float]:
        """First of `names` present in the metadata that parses as a number."""
        for n in names:
            v = self.meta.get(n)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
        return None

    @property
    def energy_eV(self) -> Optional[float]:
        # pgm_energy is the plane-grating monochromator readback: the actual
        # photon energy. Present in every real file seen.
        return self.meta_float("pgm_energy", "beamenergy", "energy")

    @property
    def polarisation(self) -> str:
        # 'pc'/'nc' = positive/negative circular; 'lh'/'lv' = linear horiz/vert.
        # These map onto the logger's p/n/LH/LV channels.
        v = self.meta.get("pol", "")
        return str(v) if v is not None else ""

    @property
    def field_T(self) -> Optional[float]:
        # demand_field is what was asked for, ips_field what the magnet reports.
        # Prefer the readback.
        return self.meta_float("ips_field", "demand_field")

    @property
    def temperature_K(self) -> Optional[float]:
        # NOTE: the prompt's suggested 'sample_temp' key does NOT exist in any
        # of the 248 real I10 files -- itc2/itc3 are the cryostat channels that
        # are actually written. sample_temp is kept first in the chain in case
        # a future GDA version starts emitting it.
        return self.meta_float("sample_temp", "itc2", "itc3", "sensor_temp")

    @property
    def command(self) -> str:
        return str(self.meta.get("cmd", ""))

    @property
    def scan_kind(self) -> str:
        """'energy', 'tth', 'hfmy', ... -- the axis named in the cmd line."""
        parts = self.command.split()
        if len(parts) >= 2 and parts[0] == "scan":
            return parts[1]
        return parts[1] if len(parts) >= 2 else ""


def parse_srs(path) -> SRSScan:
    """
    Read one SRS .dat file. Raises SRSParseError on anything that is not one.

    Tolerant of the things that actually vary between I10 files: leading
    whitespace on the marker lines, blank trailing lines, CRLF, and the odd
    non-UTF-8 byte in a comment field.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SRSParseError(f"cannot read {path}: {exc}") from exc

    lines = text.splitlines()
    if not any(ln.strip() == _MARK_SRS for ln in lines[:5]):
        raise SRSParseError(f"{path.name}: no ' &SRS' marker in the first 5 lines")

    def find(mark: str) -> Optional[int]:
        for i, ln in enumerate(lines):
            if ln.strip() == mark:
                return i
        return None

    i_end = find(_MARK_END)
    if i_end is None:
        raise SRSParseError(f"{path.name}: no ' &END' marker")

    i_meta0 = find(_MARK_META_OPEN)
    i_meta1 = find(_MARK_META_CLOSE)

    # ---- ' &SRS' preamble: comma-separated KEY=VALUE, possibly several per line
    header: Dict[str, str] = {}
    preamble_end = i_meta0 if i_meta0 is not None else i_end
    for ln in lines[1:preamble_end]:
        for item in ln.split(","):
            if "=" in item:
                k, v = item.split("=", 1)
                header[k.strip()] = _strip_quotes(v)

    # ---- <MetaDataAtStart> block: one KEY=VALUE per line, split on FIRST '='
    meta: Dict[str, object] = {}
    if i_meta0 is not None and i_meta1 is not None and i_meta1 > i_meta0:
        for ln in lines[i_meta0 + 1:i_meta1]:
            if "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            # The cmd line contains '=' inside a quoted string; splitting on the
            # FIRST '=' only is what keeps it intact.
            meta[k.strip()] = _maybe_number(_strip_quotes(v))

    # ---- column header is the single line after ' &END'
    if i_end + 1 >= len(lines):
        raise SRSParseError(f"{path.name}: nothing after ' &END'")
    columns = lines[i_end + 1].split()
    if not columns:
        raise SRSParseError(f"{path.name}: empty column header after ' &END'")

    # ---- numeric rows
    rows: List[List[float]] = []
    for ln in lines[i_end + 2:]:
        s = ln.strip()
        if not s:
            continue
        parts = s.split()
        if len(parts) != len(columns):
            # A short or long row is a truncated file (scan aborted mid-point).
            # Keep what came before rather than discarding the whole scan.
            break
        try:
            rows.append([float(p) for p in parts])
        except ValueError:
            break

    data = np.asarray(rows, dtype=float) if rows else np.empty((0, len(columns)))

    scan_number = None
    if "SRSRUN" in header:
        try:
            scan_number = int(float(header["SRSRUN"]))
        except (TypeError, ValueError):
            scan_number = None
    if scan_number is None:
        m = re.search(r"(\d+)(?!.*\d)", path.stem)
        if m:
            scan_number = int(m.group(1))

    return SRSScan(path=path, scan_number=scan_number, header=header,
                   meta=meta, columns=columns, data=data)


# =============================================================================
# PANDA CONFIG  (panda_config.toml)
# =============================================================================

@dataclass
class ReductionConfig:
    abscissa_column: str = ""
    i0_column: str = "mcsh16"
    im_column: str = "mcsh17"
    min_i0: float = 1.0
    counts_per_unit: float = 1.0


@dataclass
class Edge:
    name: str
    energy_eV: float


@dataclass
class PandaConfig:
    path: Path
    reduction: ReductionConfig
    method: str = "trapezoid"
    background: str = "linear"
    window_min: float = 0.0
    window_max: float = 0.0
    raw_dir: str = ""
    processed_dir: str = ""
    out_format: str = "csv"
    edges: List[Edge] = field(default_factory=list)
    resonance_tol_eV: float = 2.0
    field_tol_T: float = 0.005

    @classmethod
    def load(cls, path) -> "PandaConfig":
        path = Path(path)
        with open(path, "rb") as f:
            raw = tomllib.load(f)

        r = raw.get("reduction", {})
        red = ReductionConfig(
            abscissa_column=str(r.get("abscissa_column", "") or ""),
            i0_column=str(r.get("i0_column", "mcsh16")),
            im_column=str(r.get("im_column", "mcsh17")),
            min_i0=float(r.get("min_i0", 1.0)),
            counts_per_unit=float(r.get("counts_per_unit", 1.0)),
        )
        integ = raw.get("integration", {})
        paths = raw.get("paths", {})
        out = raw.get("output", {})
        phys = raw.get("physics", {})

        cfg = cls(
            path=path,
            reduction=red,
            method=str(integ.get("method", "trapezoid")).lower(),
            background=str(integ.get("background", "linear")).lower(),
            window_min=float(integ.get("window_min", 0.0)),
            window_max=float(integ.get("window_max", 0.0)),
            raw_dir=str(paths.get("raw_dir", "") or "").strip(),
            processed_dir=str(paths.get("processed_dir", "") or "").strip(),
            out_format=str(out.get("format", "csv")).lower(),
            edges=[Edge(name=str(e.get("name", "?")),
                        energy_eV=float(e.get("energy_eV", 0.0)))
                   for e in phys.get("edges", [])],
            resonance_tol_eV=float(phys.get("resonance_tol_eV", 2.0)),
            field_tol_T=float(phys.get("field_tol_T", 0.005)),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Fail loudly at startup, exactly as rsxd_core.Config does."""
        if not self.reduction.i0_column:
            raise ValueError("panda_config: [reduction] i0_column is empty")
        if not self.reduction.im_column:
            raise ValueError("panda_config: [reduction] im_column is empty")
        if self.reduction.i0_column == self.reduction.im_column:
            raise ValueError(
                "panda_config: i0_column and im_column are the same column "
                f"({self.reduction.i0_column!r}); I = Im/I0 would be identically 1"
            )
        if self.method not in ("trapezoid", "voigt"):
            raise ValueError(f"panda_config: unknown integration method {self.method!r}")
        if self.background not in ("linear", "none"):
            raise ValueError(f"panda_config: unknown background {self.background!r}")
        if self.out_format not in ("csv", "txt", "dat"):
            raise ValueError(f"panda_config: unknown output format {self.out_format!r}")
        if self.reduction.min_i0 <= 0:
            raise ValueError("panda_config: min_i0 must be > 0")

    # ---- path resolution ----------------------------------------------------
    def raw_root(self, logger_cfg: Config) -> Optional[Path]:
        """raw_data. Defaults to the logger's dat_dir -- one source of truth."""
        if self.raw_dir:
            return Path(self.raw_dir)
        return Path(logger_cfg.dat_dir) if logger_cfg.dat_dir else None

    def processed_root(self, logger_cfg: Config) -> Path:
        """processed_data. Defaults to a sibling of raw_data."""
        if self.processed_dir:
            return Path(self.processed_dir)
        raw = self.raw_root(logger_cfg)
        if raw is not None:
            return raw.parent / "processed_data"
        return Path(logger_cfg.output_dir) / "processed_data"


# =============================================================================
# STAGE 1b  --  REDUCTION:  I = Im / I0
# =============================================================================

@dataclass
class ReducedScan:
    """One scan, normalised, with counting errors."""
    scan_number: Optional[int]
    source: Path
    abscissa_name: str
    two_theta: np.ndarray               # the scanned axis (2theta for a diffraction scan)
    i0: np.ndarray
    im: np.ndarray
    intensity: np.ndarray               # I = Im / I0
    sigma: np.ndarray                    # 1 sigma on I
    # metadata carried alongside, never merged into the numbers
    tau_deg: Optional[float] = None
    energy_eV: Optional[float] = None
    field_T: Optional[float] = None
    temperature_K: Optional[float] = None
    polarisation: str = ""
    peak: str = ""
    series_temperature_K: str = ""
    note: str = ""
    scan_kind: str = ""
    n_dropped: int = 0                  # points killed by the min_i0 guard
    # Detector transimpedance gain in force for this scan, resolved from the
    # logger's epochs. None means UNKNOWN and is never defaulted to 1.0.
    gain: Optional[float] = None
    gain_normalised: bool = False       # was Im divided by `gain` below?

    @property
    def n_points(self) -> int:
        return int(self.two_theta.size)


def reduce_scan(scan: SRSScan, red: ReductionConfig,
                normalise_gain: Optional[float] = None, **meta) -> ReducedScan:
    """
    Normalise one parsed scan.

    WHY I = Im / I0
    ---------------
    The measured detector counts Im are proportional to the incident flux,
    which is not constant: the storage-ring current decays between top-ups,
    the monochromator and mirror throughput vary strongly with photon energy,
    and the beam moves slightly on the sample as optics drift. A tau-series is
    collected over tens of minutes to hours, so by the last analyser angle the
    incident flux can differ from the first by several percent. That drift is
    smooth and monotonic in time, and tau is usually stepped monotonically, so
    it masquerades exactly as a slow polarisation dependence -- the thing we
    are trying to measure. Dividing by the simultaneously-recorded monitor I0
    removes it to first order.

    ERRORS
    ------
    The scaler channels are photon counts, so each is Poisson: sigma = sqrt(N).
    For I = Im/I0 with independent Im and I0,
        (sigma_I / I)^2 = (sigma_Im / Im)^2 + (sigma_I0 / I0)^2
                        = 1/Im + 1/I0
    so sigma_I = I * sqrt(1/Im + 1/I0). I0 is typically ~10^5-10^6 counts, so
    in practice the detector term dominates -- but carrying both costs nothing
    and stays correct if the monitor is ever weak.

    GAIN
    ----
    The detector photocurrent passes through a transimpedance amplifier before
    it is digitised, so the recorded Im is (photocurrent x gain). `gain` is the
    DETECTOR (Im) gain, so it multiplies I directly -- it does NOT cancel in
    the Im/I0 ratio. Passing normalise_gain divides Im by that number first,
    putting scans taken at different amplifier settings back into common units.

    If the MONITOR (I0) channel has its own separately-switched amplifier, that
    is a second gain which this code does not track and which would not cancel
    either. Nothing here should be read as implying the ratio takes care of it.

    The counting error is computed from the RAW counts regardless of
    normalisation: dividing by a known constant rescales the value and its
    error identically, so the relative error is unchanged. Deriving sigma from
    the scaled numbers instead would silently invent precision.
    """
    if scan.data.size == 0:
        raise SRSParseError(f"{scan.path.name}: no data rows")

    # Abscissa: an explicit name, or "the first column" -- which is what every
    # real I10 scan file uses for its scanned axis.
    if red.abscissa_column:
        x = scan.column(red.abscissa_column)
        xname = red.abscissa_column
    else:
        x = scan.data[:, 0]
        xname = scan.columns[0]

    i0 = scan.column(red.i0_column)     # raises ColumnMapError, listing columns
    im = scan.column(red.im_column)

    gain = red.counts_per_unit if red.counts_per_unit > 0 else 1.0
    n_i0 = np.clip(i0 * gain, 0.0, None)
    n_im = np.clip(im * gain, 0.0, None)

    # Drop points where the monitor is dead. Dividing by ~0 produces enormous
    # values that would dominate any subsequent integral.
    good = i0 > red.min_i0
    n_dropped = int((~good).sum())

    x, i0, im = x[good], i0[good], im[good]
    n_i0, n_im = n_i0[good], n_im[good]

    divisor = 1.0
    if normalise_gain is not None:
        if not (normalise_gain > 0):
            raise ValueError(f"cannot normalise by gain {normalise_gain!r}")
        divisor = float(normalise_gain)

    with np.errstate(divide="ignore", invalid="ignore"):
        intensity = (im / divisor) / i0
        # rel uses the RAW counts -- see the docstring.
        rel = np.sqrt(np.where(n_im > 0, 1.0 / n_im, 0.0)
                      + np.where(n_i0 > 0, 1.0 / n_i0, 0.0))
        sigma = np.abs(intensity) * rel
    # A zero-count detector point has a real value (0) but sqrt(0) understates
    # its error; the conventional floor is 1 count.
    zero_im = n_im <= 0
    if zero_im.any():
        sigma[zero_im] = np.where(i0[zero_im] > 0,
                                  1.0 / (i0[zero_im] * divisor), 0.0)

    return ReducedScan(
        scan_number=scan.scan_number,
        source=scan.path,
        abscissa_name=xname,
        two_theta=np.asarray(x, dtype=float),
        i0=np.asarray(i0, dtype=float),
        im=np.asarray(im, dtype=float),
        intensity=np.asarray(intensity, dtype=float),
        sigma=np.asarray(sigma, dtype=float),
        energy_eV=scan.energy_eV,
        field_T=scan.field_T,
        temperature_K=scan.temperature_K,
        polarisation=scan.polarisation,
        scan_kind=scan.scan_kind,
        n_dropped=n_dropped,
        gain_normalised=normalise_gain is not None,
        **meta,
    )


# =============================================================================
# STAGE 3a  --  PEAK INTEGRATION  (the established RASOR method)
# =============================================================================

@dataclass
class PeakIntegral:
    area: float
    sigma: float
    window: Tuple[float, float]
    n_points: int
    background: str
    method: str
    centre: Optional[float] = None      # Voigt only
    fwhm: Optional[float] = None        # Voigt only
    fit_ok: bool = True
    message: str = ""


def _window_mask(x: np.ndarray, window: Optional[Tuple[float, float]]) -> np.ndarray:
    if window is None:
        return np.ones_like(x, dtype=bool)
    lo, hi = min(window), max(window)
    if lo == hi:
        return np.ones_like(x, dtype=bool)
    return (x >= lo) & (x <= hi)


def integrate_peak(reduced: ReducedScan,
                   window: Optional[Tuple[float, float]] = None,
                   method: str = "trapezoid",
                   background: str = "linear") -> PeakIntegral:
    """
    Integrated intensity of the reflection in one scan.

    WHY MODEL-FREE BY DEFAULT
    -------------------------
    The obvious alternative is to fit a peak shape and integrate the fit. That
    is more precise *if the shape is right*, and actively misleading if it is
    not. A RASOR reflection is a convolution of the instrumental resolution
    with the sample's mosaic and, for a magnetic satellite, possibly unresolved
    structure -- it is not reliably Voigt. Worse, a shape mismatch does not
    just add noise: it biases the recovered area, and the bias changes with
    peak height. Since peak height is precisely what varies across a tau
    series, a shape-induced bias is systematically tau-dependent and is
    therefore indistinguishable from real polarisation dichroism.

    A trapezoidal sum over a fixed 2theta window makes no shape assumption and
    treats every tau identically, so whatever it gets wrong, it gets wrong the
    same way at every point and largely cancels in the tau dependence. That is
    why the established method is a box sum, and why it is the default here.
    The Voigt option exists as a cross-check -- if the two disagree in shape,
    something is wrong with the window or the background, not with the physics.

    BACKGROUND
    ----------
    Linear between the two window endpoints: the fluorescent/diffuse background
    under a soft-X-ray reflection is smooth on the scale of a rocking curve, so
    two points are enough, and using exactly the endpoints keeps it operator-
    reproducible with no extra parameters.

    ERRORS
    ------
    The whole operation -- trapezoid weights, minus a linear background pinned
    to two of the same measured points -- is LINEAR in the measured intensities:
        area = sum_i c_i * I_i
    so the error is exact, with no approximation beyond the per-point sigmas:
        sigma_area = sqrt( sum_i c_i^2 * sigma_i^2 )
    Building the coefficient vector c explicitly (rather than integrating and
    then bolting an error estimate on) is what makes the background's own
    counting error propagate correctly.
    """
    x, y, s = reduced.two_theta, reduced.intensity, reduced.sigma
    order = np.argsort(x)
    x, y, s = x[order], y[order], s[order]

    mask = _window_mask(x, window)
    if mask.sum() < 3:
        return PeakIntegral(area=float("nan"), sigma=float("nan"),
                            window=(float(x.min()), float(x.max())) if x.size else (0, 0),
                            n_points=int(mask.sum()), background=background,
                            method=method, fit_ok=False,
                            message="fewer than 3 points inside the 2theta window")

    xw, yw, sw = x[mask], y[mask], s[mask]
    win = (float(xw[0]), float(xw[-1]))

    if method == "voigt":
        return _integrate_voigt(xw, yw, sw, win, background)

    # ---- trapezoid weights -------------------------------------------------
    n = xw.size
    w = np.zeros(n)
    dx = np.diff(xw)
    w[:-1] += dx / 2.0
    w[1:] += dx / 2.0

    c = w.copy()
    if background == "linear":
        # bkg(x) = y_a + (y_b - y_a) * t,   t = (x - x_a)/(x_b - x_a)
        # integral of bkg = y_a * sum(w*(1-t)) + y_b * sum(w*t)
        span = xw[-1] - xw[0]
        t = (xw - xw[0]) / span if span != 0 else np.zeros(n)
        c[0] -= float(np.sum(w * (1.0 - t)))
        c[-1] -= float(np.sum(w * t))

    area = float(np.dot(c, yw))
    sigma = float(np.sqrt(np.dot(c * c, sw * sw)))
    return PeakIntegral(area=area, sigma=sigma, window=win, n_points=int(n),
                        background=background, method="trapezoid")


def _voigt(x, amplitude, centre, sigma_g, gamma_l, slope, offset):
    """Pseudo-Voigt-free true Voigt via the Faddeeva function, plus a linear background."""
    from scipy.special import wofz
    sigma_g = abs(sigma_g) + 1e-12
    gamma_l = abs(gamma_l)
    z = ((x - centre) + 1j * gamma_l) / (sigma_g * math.sqrt(2.0))
    profile = np.real(wofz(z)) / (sigma_g * math.sqrt(2.0 * math.pi))
    return amplitude * profile + slope * (x - centre) + offset


def _integrate_voigt(xw, yw, sw, win, background) -> PeakIntegral:
    """
    Optional cross-check: fit a Voigt + linear background and report the
    analytic area of the Voigt component. Never the default -- see the note in
    integrate_peak().
    """
    import warnings

    from scipy.optimize import OptimizeWarning
    from scipy.optimize import curve_fit

    lo, hi = float(np.min(yw)), float(np.max(yw))
    span = float(xw[-1] - xw[0]) or 1.0
    p0 = [
        max(hi - lo, 1e-12) * span / 4.0,           # amplitude (= area of profile)
        float(xw[int(np.argmax(yw))]),              # centre
        span / 10.0,                                # gaussian sigma
        span / 20.0,                                # lorentzian gamma
        0.0,                                        # background slope
        lo,                                         # background offset
    ]
    try:
        sig = np.where(sw > 0, sw, np.nanmedian(sw[sw > 0]) if np.any(sw > 0) else 1.0)
        with warnings.catch_warnings():
            # A singular covariance is already surfaced as sigma = nan below;
            # the warning would only spam the console during a beamtime.
            warnings.simplefilter("ignore", OptimizeWarning)
            popt, pcov = curve_fit(_voigt, xw, yw, p0=p0, sigma=sig,
                                   absolute_sigma=True, maxfev=20000)
    except Exception as exc:                        # noqa: BLE001
        return PeakIntegral(area=float("nan"), sigma=float("nan"), window=win,
                            n_points=int(xw.size), background=background,
                            method="voigt", fit_ok=False,
                            message=f"Voigt fit did not converge: {exc}")

    # The profile is normalised, so `amplitude` IS the area under the peak.
    area = float(popt[0])
    var = float(pcov[0, 0]) if np.all(np.isfinite(pcov)) else float("nan")
    sigma = math.sqrt(var) if var == var and var >= 0 else float("nan")

    sigma_g, gamma_l = abs(popt[2]), abs(popt[3])
    # Olivero & Longbothum approximation for the Voigt FWHM.
    fg = 2.0 * math.sqrt(2.0 * math.log(2.0)) * sigma_g
    fl = 2.0 * gamma_l
    fwhm = 0.5346 * fl + math.sqrt(0.2166 * fl * fl + fg * fg)

    return PeakIntegral(area=area, sigma=sigma, window=win, n_points=int(xw.size),
                        background=background, method="voigt",
                        centre=float(popt[1]), fwhm=float(fwhm))


# =============================================================================
# STAGE 3b  --  STOKES FIT
# =============================================================================

def stokes_model(tau_deg, i_total, p1, p2):
    """
    Intensity transmitted by a linear polarisation analyser rotated to tau.

        I(tau) = (I_total / 2) * [ 1 + P1' cos(2 tau) + P2' sin(2 tau) ]

    WHERE THIS COMES FROM
    ---------------------
    The polarisation state of the scattered beam is described by the Stokes
    parameters (P1', P2', P3') on the Poincare sphere: P1' and P2' the two
    linear components (at 0/90 deg and at +-45 deg to the scattering plane),
    P3' the circular one. The RASOR analyser is a Bragg reflection set near
    45 deg, which transmits essentially only the polarisation component lying
    in its own scattering plane. Rotating the analyser about the beam by tau
    projects the beam's linear polarisation onto that plane, and Malus's law
    gives the cos(2 tau) / sin(2 tau) modulation above.

    The period is 180 deg, not 360: linear polarisation is a headless vector,
    so tau and tau+180 are the same measurement. This is why the fit uses
    2*tau, and why a 13-point series from -90 to +90 covers exactly one full
    period -- a good design, because it means P1' and P2' are orthogonal over
    the sampled set and the fit is well conditioned.

    P3' (circular) does not appear: a linear analyser is blind to it. Circular
    dichroism is instead accessed by comparing the p and n incident helicities,
    which is what the logger's two circular rows are for.

    THE DISCRIMINATOR
    -----------------
    Charge (Thomson) scattering does not rotate the polarisation: sigma in ->
    sigma out, so the outgoing beam stays where the incident beam was and P1'
    sits at one extreme. Resonant magnetic (E1) scattering carries a
    (e_out x e_in) . m term, which rotates sigma -> pi, so a purely magnetic
    reflection comes out with P1' at the OPPOSITE extreme. The sign of P1' is
    therefore the charge-vs-magnetic discriminator, and a mixed reflection
    lands in between with the interference showing up in P2'.

    SIGN CONVENTION -- READ THIS
    ----------------------------
    The absolute sign of P1' depends on where the analyser's mechanical zero
    sits relative to the scattering plane, which is an instrument alignment
    fact, not a physical one. PANDA reports P1' in the convention "tau as
    written in the logger", and does not attempt to guess the offset. Calibrate
    it once per beamtime against a known charge peak; after that the sign is
    meaningful, and before that only the *relative* sign between two peaks is.
    """
    tau = np.radians(np.asarray(tau_deg, dtype=float))
    return (i_total / 2.0) * (1.0 + p1 * np.cos(2.0 * tau) + p2 * np.sin(2.0 * tau))


@dataclass
class StokesFit:
    i_total: float
    p1: float
    p2: float
    i_total_err: float
    p1_err: float
    p2_err: float
    covariance: np.ndarray
    chi2: float
    reduced_chi2: float
    n_points: int
    dof: int
    ok: bool = True
    message: str = ""

    @property
    def linear_degree(self) -> float:
        """sqrt(P1'^2 + P2'^2) -- how linearly polarised the beam is overall."""
        return float(math.hypot(self.p1, self.p2))

    @property
    def linear_angle_deg(self) -> float:
        """Orientation of the linear polarisation, 0.5*atan2(P2', P1')."""
        return float(math.degrees(0.5 * math.atan2(self.p2, self.p1)))

    @property
    def character(self) -> str:
        """Plain-language read of the discriminator. Sign convention applies."""
        if not self.ok or not math.isfinite(self.p1):
            return "undetermined"
        if abs(self.p1) < 2.0 * max(self.p1_err, 1e-12):
            return "mixed / unpolarised (P1' consistent with zero)"
        return "charge-like (P1' > 0)" if self.p1 > 0 else "magnetic-like (P1' < 0)"


def fit_stokes(tau_deg: Sequence[float],
               intensity: Sequence[float],
               sigma: Optional[Sequence[float]] = None) -> StokesFit:
    """
    Fit I(tau) = (I_total/2)[1 + P1' cos2tau + P2' sin2tau] to the Stage-3a
    integrated intensities.

    Uses scipy curve_fit, seeded from the exact linear least-squares solution.
    The model is nonlinear in (I_total, P1, P2) but LINEAR in
    (A, B, C) = (I_total/2, A*P1, A*P2), so that linear solve is closed-form,
    unique, and free -- which makes the starting point exact rather than a
    guess, and stops the nonlinear fit from wandering when the modulation is
    weak or the series is short.
    """
    tau = np.asarray(tau_deg, dtype=float)
    y = np.asarray(intensity, dtype=float)
    s = None if sigma is None else np.asarray(sigma, dtype=float)

    good = np.isfinite(tau) & np.isfinite(y)
    if s is not None:
        good &= np.isfinite(s)
    tau, y = tau[good], y[good]
    if s is not None:
        s = s[good]
        # A zero error would give that point infinite weight.
        pos = s[s > 0]
        s = np.where(s > 0, s, (np.median(pos) if pos.size else 1.0))

    n = tau.size
    dof = n - 3
    if n < 3:
        return StokesFit(*(float("nan"),) * 6, covariance=np.full((3, 3), np.nan),
                         chi2=float("nan"), reduced_chi2=float("nan"),
                         n_points=n, dof=dof, ok=False,
                         message=f"need at least 3 analyser angles, got {n}")

    # ---- exact linear seed --------------------------------------------------
    t2 = 2.0 * np.radians(tau)
    M = np.column_stack([np.ones_like(t2), np.cos(t2), np.sin(t2)])
    if s is not None:
        W = 1.0 / s
        abc, *_ = np.linalg.lstsq(M * W[:, None], y * W, rcond=None)
    else:
        abc, *_ = np.linalg.lstsq(M, y, rcond=None)
    A, B, C = abc
    if abs(A) < 1e-30:
        return StokesFit(*(float("nan"),) * 6, covariance=np.full((3, 3), np.nan),
                         chi2=float("nan"), reduced_chi2=float("nan"),
                         n_points=n, dof=dof, ok=False,
                         message="mean intensity is zero; nothing to normalise against")
    p0 = [2.0 * A, B / A, C / A]

    import warnings

    from scipy.optimize import OptimizeWarning, curve_fit
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", OptimizeWarning)
            popt, pcov = curve_fit(stokes_model, tau, y, p0=p0,
                                   sigma=s, absolute_sigma=(s is not None),
                                   maxfev=20000)
    except Exception as exc:                        # noqa: BLE001
        return StokesFit(*(float("nan"),) * 6, covariance=np.full((3, 3), np.nan),
                         chi2=float("nan"), reduced_chi2=float("nan"),
                         n_points=n, dof=dof, ok=False,
                         message=f"Stokes fit did not converge: {exc}")

    resid = y - stokes_model(tau, *popt)
    if s is not None:
        chi2 = float(np.sum((resid / s) ** 2))
    else:
        chi2 = float(np.sum(resid ** 2))
    red = chi2 / dof if dof > 0 else float("nan")

    errs = np.sqrt(np.diag(pcov)) if np.all(np.isfinite(pcov)) else np.full(3, np.nan)
    return StokesFit(i_total=float(popt[0]), p1=float(popt[1]), p2=float(popt[2]),
                     i_total_err=float(errs[0]), p1_err=float(errs[1]),
                     p2_err=float(errs[2]), covariance=np.asarray(pcov),
                     chi2=chi2, reduced_chi2=red, n_points=n, dof=dof)


# =============================================================================
# THE tau-SERIES INDEX  --  read the phase-1 logger's cumulative CSV
# =============================================================================

@dataclass
class SeriesEntry:
    peak: str
    temperature_K: str
    tau_deg: str
    polarisation: str
    scan_number: int
    note: str = ""
    dat_found: str = ""
    timestamp: str = ""
    gain: Optional[float] = None        # None = not recorded, never 1.0

    # gain_epochs() in rsxd_core wants `.scan`; SeriesEntry calls it
    # scan_number because that is the CSV column name.
    @property
    def scan(self) -> int:
        return self.scan_number


@dataclass
class Series:
    """One (peak, temperature) pair = one workbook in the logger = one tau series."""
    peak: str
    temperature_K: str
    entries: List[SeriesEntry]
    # Epochs resolved over the WHOLE log, not just this series: the amplifier
    # setting propagates by scan number and does not reset per workbook.
    session_epochs: List[GainEpoch] = field(default_factory=list)

    def gain_of(self, scan: int) -> Optional[float]:
        return effective_gain(scan, self.session_epochs)

    def epochs_for(self, pol: Optional[str] = None) -> List[GainEpoch]:
        """
        The epochs actually spanned by this series (optionally one channel).

        Resolved by epoch START, matching effective_gain: a scan belongs to the
        last epoch that began at or before it.
        """
        rows = self.by_polarisation(pol) if pol else list(self.entries)
        seen: List[GainEpoch] = []
        for e in rows:
            match = None
            for ep in self.session_epochs:
                if ep.first_scan <= e.scan_number:
                    match = ep
                else:
                    break
            if match is not None and match not in seen:
                seen.append(match)
        return seen

    @property
    def key(self) -> Tuple[str, str]:
        return (self.peak, self.temperature_K)

    @property
    def label(self) -> str:
        return f"{self.peak}  {self.temperature_K} K"

    def slug(self, logger_cfg: Config) -> str:
        return f"{logger_cfg.peak_file_string(self.peak)}_{self.temperature_K}K"

    def by_polarisation(self, pol: str) -> List[SeriesEntry]:
        rows = [e for e in self.entries if e.polarisation == pol]
        rows.sort(key=lambda e: float(e.tau_deg))
        return rows

    @property
    def polarisations(self) -> List[str]:
        seen: List[str] = []
        for e in self.entries:
            if e.polarisation not in seen:
                seen.append(e.polarisation)
        return seen


def load_series_index(csv_path, notices: Optional[List[str]] = None) -> List[Series]:
    """
    Read the logger's cumulative CSV and group it into tau series.

    Reads the schema in rsxd_core.CSV_COLUMNS. Unknown extra columns are
    ignored rather than fatal, so a newer CSV still loads.

    LEGACY FILES: columns listed in CSV_OPTIONAL_COLUMNS may be absent -- a
    v1 log predates gain tracking. Those rows load with gain = None, which
    means UNKNOWN and propagates as unknown all the way through the analysis.
    They are never filled in with 1.0: a wrong gain is worse than no gain,
    because no gain gets flagged and a wrong one does not. Pass `notices` (a
    list) to be told when a legacy file was read, so the GUI can say so.
    """
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        return []
    with csv_path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        missing = [c for c in CSV_COLUMNS
                   if c not in fields and c not in CSV_OPTIONAL_COLUMNS]
        if missing:
            raise ValueError(
                f"{csv_path.name} is not a phase-1 scan log: missing column(s) "
                f"{', '.join(missing)}. Expected the schema: "
                f"{', '.join(CSV_COLUMNS)}"
            )
        legacy = [c for c in CSV_OPTIONAL_COLUMNS if c not in fields]
        if legacy and notices is not None:
            notices.append(
                f"{csv_path.name} predates gain tracking (no "
                f"{', '.join(legacy)} column). Those scans load with gain "
                f"UNKNOWN; the file is rewritten in the current schema on the "
                f"next save.")
        grouped: Dict[Tuple[str, str], List[SeriesEntry]] = {}
        for row in reader:
            try:
                scan = int(row["scan_number"])
            except (KeyError, TypeError, ValueError):
                continue
            raw_gain = row.get("gain")
            try:
                gain = float(raw_gain) if str(raw_gain or "").strip() else None
            except (TypeError, ValueError):
                gain = None
            e = SeriesEntry(
                peak=row.get("peak", ""),
                temperature_K=row.get("temperature_K", ""),
                tau_deg=row.get("analyser_angle_deg", ""),
                polarisation=row.get("polarisation", ""),
                scan_number=scan,
                note=row.get("note", ""),
                dat_found=row.get("dat_found", ""),
                timestamp=row.get("timestamp", ""),
                gain=gain,
            )
            grouped.setdefault((e.peak, e.temperature_K), []).append(e)

    out = [Series(peak=k[0], temperature_K=k[1], entries=v)
           for k, v in grouped.items()]
    out.sort(key=lambda s: (s.peak, -_as_float(s.temperature_K, 0.0)))

    # Gain propagates by SCAN NUMBER across the whole log, not per workbook:
    # the operator turns the dial once and it stays turned. So epochs are
    # resolved over every row in the file and then handed to each series.
    all_entries = [e for series in out for e in series.entries]
    epochs = gain_epochs(all_entries)
    for series in out:
        series.session_epochs = epochs
    return out


def _as_float(value, default=float("nan")) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class RawIndex:
    """
    filename -> path for everything under raw_data, at any depth.

    raw_data is not necessarily flat: WinSCP mirrors whatever structure the
    beamline PC had, so files often land in per-day or per-session subfolders.
    Walking the tree once and caching is far cheaper than probing each
    candidate path per scan, and it makes nesting depth irrelevant.
    """

    def __init__(self, root):
        self.root = Path(root)
        self._names: Dict[str, Path] = {}
        self.refresh()

    def refresh(self) -> "RawIndex":
        self._names = {}
        try:
            for dirpath, _dirnames, filenames in os.walk(self.root):
                for fn in filenames:
                    # First match wins, so a file at the top of raw_data beats a
                    # same-named copy buried deeper.
                    self._names.setdefault(fn.lower(), Path(dirpath) / fn)
        except OSError:
            pass
        return self

    def get(self, name: str) -> Optional[Path]:
        return self._names.get(name.lower())

    def __len__(self) -> int:
        return len(self._names)


_INDEX_CACHE: Dict[str, RawIndex] = {}


def raw_index(root, refresh: bool = False) -> Optional[RawIndex]:
    """Cached RawIndex per root. Call with refresh=True after a WinSCP sync."""
    if root is None:
        return None
    key = str(Path(root))
    if refresh or key not in _INDEX_CACHE:
        _INDEX_CACHE[key] = RawIndex(root)
    return _INDEX_CACHE[key]


def resolve_dat(scan_number: int, raw_root: Optional[Path],
                patterns: Sequence[str],
                index: Optional[RawIndex] = None) -> Optional[Path]:
    """
    Scan number -> .dat path, using exactly the same {scan} substitution and
    pattern list as the logger's DatChecker. Same rules, one convention.

    Tries the flat layout first (the common case, and free), then falls back
    to the recursive index.
    """
    if raw_root is None:
        return None
    checker = DatChecker(str(raw_root), list(patterns))
    names = checker.filenames_for(scan_number)

    for name in names:
        p = Path(raw_root) / name
        if p.is_file():
            return p

    idx = index if index is not None else raw_index(raw_root)
    if idx is None:
        return None
    for name in names:
        p = idx.get(name)
        if p is not None and p.is_file():
            return p
    return None


# =============================================================================
# OUTPUT  --  processed_data mirrors raw_data
# =============================================================================

def mirror_dir(raw_path: Optional[Path], raw_root: Optional[Path],
               processed_root: Path) -> Path:
    """
    Where a processed file for `raw_path` belongs.

    processed_data reproduces the sub-structure of raw_data, so a raw file at
        raw_data/2026-08/i10-1-64951.dat
    produces outputs in
        processed_data/2026-08/
    Anything that cannot be placed relative to raw_root lands at the top of
    processed_data rather than being dropped.
    """
    if raw_path is None or raw_root is None:
        return processed_root
    try:
        rel = Path(raw_path).resolve().parent.relative_to(Path(raw_root).resolve())
    except (ValueError, OSError):
        return processed_root
    return processed_root / rel


_EXT = {"csv": ".csv", "txt": ".txt", "dat": ".dat"}


def write_table(path: Path, columns: Sequence[str],
                rows: Sequence[Sequence], fmt: str = "csv",
                comments: Sequence[str] = ()) -> Path:
    """
    Write one output table, atomically (rsxd_core._atomic_write).

    csv       machine-readable, what the analysis scripts consume
    txt/dat   whitespace-aligned with '#' comment header, for gnuplot and
              other column-readers that dislike commas
    """
    fmt = (fmt or "csv").lower()
    path = path.with_suffix(_EXT.get(fmt, ".csv"))

    def cell(v):
        if v is None:
            return ""
        if isinstance(v, float):
            if not math.isfinite(v):
                return "nan"
            return f"{v:.8g}"
        return str(v)

    if fmt == "csv":
        buf = io.StringIO(newline="")
        w = csv.writer(buf, lineterminator="\n")
        for c in comments:
            w.writerow([f"# {c}"])
        w.writerow(list(columns))
        for r in rows:
            w.writerow([cell(v) for v in r])
        text = buf.getvalue()
    else:
        body = [[cell(v) for v in r] for r in rows]
        widths = [max(len(str(c)), *(len(b[i]) for b in body)) if body else len(str(c))
                  for i, c in enumerate(columns)]
        lines = [f"# {c}" for c in comments]
        lines.append("# " + "  ".join(str(c).ljust(w) for c, w in zip(columns, widths)))
        for b in body:
            lines.append("  " + "  ".join(v.ljust(w) for v, w in zip(b, widths)))
        text = "\n".join(lines) + "\n"

    _atomic_write(path, text)
    return path


def save_reduced(reduced: ReducedScan, out_dir: Path, fmt: str = "csv") -> Path:
    """Stage 1 output: one tidy table per scan."""
    stem = reduced.source.stem if reduced.source else f"scan_{reduced.scan_number}"
    path = out_dir / f"{stem}_reduced"
    comments = [
        f"PANDA {VERSION}  stage 1 (reduce)   written {datetime.now().isoformat(timespec='seconds')}",
        f"source            {reduced.source}",
        f"scan_number       {reduced.scan_number}",
        f"peak              {reduced.peak}",
        f"series_temp_K     {reduced.series_temperature_K}",
        f"analyser_tau_deg  {reduced.tau_deg}",
        f"polarisation      {reduced.polarisation}",
        f"energy_eV         {reduced.energy_eV}",
        f"field_T           {reduced.field_T}",
        f"temperature_K     {reduced.temperature_K}",
        f"detector_gain_VA  {fmt_gain(reduced.gain) or 'UNKNOWN (not recorded)'}",
        f"gain_normalised   {'yes -- Im divided by the gain above' if reduced.gain_normalised else 'no -- raw Im'}",
        f"scan_kind         {reduced.scan_kind}",
        f"abscissa          {reduced.abscissa_name}",
        f"points_dropped    {reduced.n_dropped} (I0 below the min_i0 guard)",
        "I = Im / I0 ; sigma from Poisson counting on both channels",
    ]
    rows = list(zip(reduced.two_theta, reduced.i0, reduced.im,
                    reduced.intensity, reduced.sigma))
    return write_table(path, ["two_theta", "i0", "im", "intensity", "sigma"],
                       rows, fmt=fmt, comments=comments)


def save_tau_curve(series_slug: str, pol: str, points: Sequence[dict],
                   out_dir: Path, fmt: str = "csv",
                   comments: Sequence[str] = (), gain_note: str = "") -> Path:
    """Stage 3a output: the (tau, integrated I, sigma) table."""
    path = out_dir / f"{series_slug}_{pol}_tau_curve"
    cols = ["analyser_angle_deg", "scan_number", "integrated_intensity", "sigma",
            "window_min", "window_max", "n_points", "method", "background",
            "energy_eV", "field_T", "temperature_K", "gain", "gain_normalised",
            "note"]
    rows = [[p.get(c) for c in cols] for p in points]
    head = [f"PANDA {VERSION}  stage 3a (tau curve)   "
            f"written {datetime.now().isoformat(timespec='seconds')}",
            f"series            {series_slug}",
            f"polarisation      {pol}",
            "integrated intensity of the reflection vs polarisation-analyser angle tau",
            f"gain              {gain_note or 'not reported'}",
            *comments]
    return write_table(path, cols, rows, fmt=fmt, comments=head)


def save_stokes(series_slug: str, pol: str, fit: StokesFit,
                out_dir: Path, fmt: str = "csv",
                comments: Sequence[str] = (),
                suspect: bool = False, gain_note: str = "") -> Path:
    """Stage 3b output: the fitted Stokes parameters."""
    path = out_dir / f"{series_slug}_{pol}_stokes"
    cols = ["parameter", "value", "sigma", "units"]
    rows = [
        ["gain_suspect", "yes" if suspect else "no", None,
         "mixed/unknown detector gain, not normalised"],
        ["I_total", fit.i_total, fit.i_total_err, "integrated intensity"],
        ["P1_prime", fit.p1, fit.p1_err, "dimensionless"],
        ["P2_prime", fit.p2, fit.p2_err, "dimensionless"],
        ["linear_degree", fit.linear_degree, None, "dimensionless"],
        ["linear_angle", fit.linear_angle_deg, None, "deg"],
        ["chi2", fit.chi2, None, ""],
        ["reduced_chi2", fit.reduced_chi2, None, ""],
        ["n_points", fit.n_points, None, ""],
        ["dof", fit.dof, None, ""],
    ]
    head = [f"PANDA {VERSION}  stage 3b (Stokes fit)   "
            f"written {datetime.now().isoformat(timespec='seconds')}",
            f"series            {series_slug}",
            f"polarisation      {pol}",
            "model  I(tau) = (I_total/2) * [1 + P1'*cos(2tau) + P2'*sin(2tau)]",
            f"character         {fit.character}",
            "sign convention: P1' is reported in the logger's tau frame; "
            "calibrate the analyser zero against a known charge peak",
            *comments]
    if not fit.ok:
        head.append(f"WARNING           {fit.message}")
    # The suspect flag goes in the FILE, not only on screen: a results file
    # that outlives the session must carry its own health warning.
    if suspect:
        head.append("SUSPECT           YES -- this tau series spans more than "
                    "one detector gain (or contains scans of unknown gain) and "
                    "was NOT normalised. Part of the tau modulation is an "
                    "amplifier setting, not physics. P1'/P2' below are not "
                    "trustworthy as they stand.")
    if gain_note:
        head.append(f"gain              {gain_note}")
    return write_table(path, cols, rows, fmt=fmt, comments=head)


# =============================================================================
# STAGE 4  --  PHYSICS READ-OUTS
# =============================================================================

@dataclass
class SeriesSummary:
    """One fitted series, reduced to the numbers Stage 4 compares."""
    peak: str
    temperature_K: str
    polarisation: str
    n_scans: int
    energy_eV: Optional[float]
    field_T: Optional[float]
    sample_temperature_K: Optional[float]
    fit: StokesFit
    edge: str = ""
    resonance: str = ""
    field_state: str = ""
    gain_summary: str = ""
    mixed_gain: bool = False
    gain_normalised: bool = False

    @property
    def suspect(self) -> bool:
        return self.mixed_gain and not self.gain_normalised


def classify(summary: SeriesSummary, cfg: PandaConfig) -> SeriesSummary:
    """
    Tag a series on/off resonance and field on/off.

    Labels only. Nothing is filtered, weighted or discarded on this basis --
    the operator decides what a comparison means; PANDA just makes the
    comparison legible.
    """
    e = summary.energy_eV
    if e is None or not math.isfinite(e):
        summary.edge, summary.resonance = "", "unknown"
    else:
        near = [ed for ed in cfg.edges if abs(e - ed.energy_eV) <= cfg.resonance_tol_eV]
        if near:
            best = min(near, key=lambda ed: abs(e - ed.energy_eV))
            summary.edge = best.name
            summary.resonance = ("off-resonance" if "pre" in best.name.lower()
                                 else "on-resonance")
        else:
            summary.edge = ""
            summary.resonance = "off-resonance"

    b = summary.field_T
    if b is None or not math.isfinite(b):
        summary.field_state = "unknown"
    else:
        summary.field_state = ("field off" if abs(b) <= cfg.field_tol_T
                               else f"field on ({b * 1000:.0f} mT)")
    return summary


def save_comparison(summaries: Sequence[SeriesSummary], out_dir: Path,
                    fmt: str = "csv", name: str = "stage4_comparison") -> Path:
    """Stage 4 output: one row per fitted series, ready to read across."""
    path = out_dir / name
    cols = ["peak", "temperature_K", "polarisation", "n_scans", "energy_eV", "edge",
            "resonance", "field_T", "field_state", "sample_temperature_K",
            "gain_epochs", "gain_normalised", "gain_suspect",
            "P1_prime", "P1_sigma", "P2_prime", "P2_sigma",
            "linear_degree", "linear_angle_deg", "I_total", "reduced_chi2",
            "character"]
    rows = []
    for s in summaries:
        rows.append([
            s.peak, s.temperature_K, s.polarisation, s.n_scans, s.energy_eV, s.edge,
            s.resonance, s.field_T, s.field_state, s.sample_temperature_K,
            s.gain_summary, "yes" if s.gain_normalised else "no",
            "yes" if s.suspect else "no",
            s.fit.p1, s.fit.p1_err, s.fit.p2, s.fit.p2_err,
            s.fit.linear_degree, s.fit.linear_angle_deg, s.fit.i_total,
            s.fit.reduced_chi2, s.fit.character,
        ])
    head = [f"PANDA {VERSION}  stage 4 (physics read-outs)   "
            f"written {datetime.now().isoformat(timespec='seconds')}",
            "one row per fitted tau series; compare across rows",
            "P1' sign is the charge-vs-magnetic discriminator "
            "(see rsxd_analysis.stokes_model)"]
    # A comparison that mixes gains without normalising is comparing rulers,
    # not intensities. Say so at the top of the file, not in a footnote.
    suspects = [s for s in summaries if s.suspect]
    if suspects:
        head.append(
            "WARNING: %d of %d series below span mixed or unknown detector "
            "gain and were not normalised (%s). Cross-series intensity "
            "comparisons involving them are not meaningful." % (
                len(suspects), len(summaries),
                "; ".join(f"{s.peak} {s.temperature_K}K {s.polarisation}"
                          for s in suspects)))
    gains = {s.gain_summary for s in summaries if s.gain_summary}
    if len(gains) > 1:
        head.append("NOTE: the series below were not all taken at the same "
                    "gain -- see the gain_epochs column.")
    return write_table(path, cols, rows, fmt=fmt, comments=head)


# =============================================================================
# END-TO-END CONVENIENCE  --  used by the GUI and by the self-test
# =============================================================================

@dataclass
class SeriesResult:
    series: Series
    polarisation: str
    reduced: List[ReducedScan]
    integrals: List[PeakIntegral]
    taus: List[float]
    fit: StokesFit
    missing: List[int] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    gain_normalised: bool = False

    # ---- gain state, the whole point of the guard ------------------------
    @property
    def gains(self) -> List[Optional[float]]:
        return [r.gain for r in self.reduced]

    @property
    def distinct_gains(self) -> List[float]:
        out: List[float] = []
        for g in self.gains:
            if g is not None and g not in out:
                out.append(g)
        return sorted(out)

    @property
    def has_unknown_gain(self) -> bool:
        return any(g is None for g in self.gains)

    @property
    def mixed_gain(self) -> bool:
        """
        True when this tau-series is not in one consistent set of units.

        This is the condition the whole feature exists to catch: a gain change
        partway through a tau-series multiplies part of the curve by a
        constant, and a step in the tau-curve is indistinguishable in SHAPE
        from real dichroism. Stage 3b would fit it and hand back a P1'/P2'
        that is partly an amplifier setting.
        """
        return len(self.distinct_gains) > 1 or self.has_unknown_gain

    def gain_scan_map(self) -> Dict[Optional[float], List[int]]:
        out: Dict[Optional[float], List[int]] = {}
        for r in self.reduced:
            out.setdefault(r.gain, []).append(r.scan_number)
        return out

    def gain_summary(self) -> str:
        """One line naming the offending scans, for a banner or a file header."""
        if not self.mixed_gain:
            g = self.distinct_gains[0] if self.distinct_gains else None
            return f"single gain {fmt_gain(g)} V/A" if g is not None else "no scans"
        parts = []
        for g, scans in sorted(self.gain_scan_map().items(),
                               key=lambda kv: (kv[0] is None, kv[0] or 0)):
            lo, hi = min(scans), max(scans)
            span = f"{lo}" if lo == hi else f"{lo}-{hi}"
            parts.append(f"{fmt_gain(g) or 'UNKNOWN'}: {span}")
        return "  |  ".join(parts)

    @property
    def suspect(self) -> bool:
        """Mixed gain and not normalised -> the fitted parameters are not clean."""
        return self.mixed_gain and not self.gain_normalised


def window_is_usable(reduced: ReducedScan,
                     window: Optional[Tuple[float, float]],
                     min_points: int = 3) -> bool:
    """
    Does this 2theta window actually land on this scan?

    Matters because different reflections sit at different 2theta. A window
    chosen for the charge peak at 30 deg contains nothing but background at a
    magnetic satellite at 22.5 deg, and integrating it would return a number
    that looks like data but is noise about zero -- which then fits to a
    meaningless P1'. Better to notice and fall back to the full range.
    """
    if window is None:
        return True
    x = reduced.two_theta
    if x.size == 0:
        return False
    lo, hi = min(window), max(window)
    return int(((x >= lo) & (x <= hi)).sum()) >= min_points


def process_series(series: Series, pol: str, logger_cfg: Config, cfg: PandaConfig,
                   window: Optional[Tuple[float, float]] = None,
                   method: Optional[str] = None,
                   raw_root: Optional[Path] = None,
                   normalise_gain: bool = False) -> SeriesResult:
    """
    Stage 1 -> 3a -> 3b for one polarisation channel of one tau series.

    Missing .dat files are reported, not fatal: during a beamtime half a series
    may still be sitting on the beamline PC waiting for the next WinSCP sync,
    and a partial tau curve is still worth looking at.
    """
    raw_root = raw_root if raw_root is not None else cfg.raw_root(logger_cfg)
    method = method or cfg.method
    # Rebuild the filename index once per series, not once per scan: files
    # arrive in batches during a beamtime, so a cached index from ten minutes
    # ago would miss whatever the last WinSCP sync brought in.
    index = raw_index(raw_root, refresh=True) if raw_root is not None else None

    reduced: List[ReducedScan] = []
    integrals: List[PeakIntegral] = []
    taus: List[float] = []
    missing: List[int] = []
    errors: List[str] = []
    warnings_: List[str] = []

    for entry in series.by_polarisation(pol):
        path = resolve_dat(entry.scan_number, raw_root, logger_cfg.dat_patterns,
                           index=index)
        if path is None:
            missing.append(entry.scan_number)
            continue
        gain = series.gain_of(entry.scan_number)
        # Normalisation is only possible where the gain is actually known.
        # Dividing by an assumed value would be worse than not dividing.
        divisor = gain if (normalise_gain and gain is not None) else None
        try:
            scan = parse_srs(path)
            r = reduce_scan(scan, cfg.reduction,
                            normalise_gain=divisor,
                            tau_deg=_as_float(entry.tau_deg),
                            peak=series.peak,
                            series_temperature_K=series.temperature_K,
                            note=entry.note,
                            gain=gain)
        except (SRSParseError, ColumnMapError) as exc:
            errors.append(str(exc))
            continue
        # Per-scan window sanity: a window chosen for one reflection can miss
        # another entirely, because different peaks sit at different 2theta.
        # Fall back to that scan's full range rather than integrating pure
        # background and reporting the result as signal.
        w = window
        if not window_is_usable(r, w):
            w = None
            if not warnings_:
                warnings_.append(
                    "the 2theta window %s does not overlap this series "
                    "(scan %s spans %.3f-%.3f); integrated over the full scan "
                    "range instead" % (window, r.scan_number,
                                       float(r.two_theta.min()),
                                       float(r.two_theta.max())))
        integral = integrate_peak(r, window=w, method=method,
                                  background=cfg.background)
        reduced.append(r)
        integrals.append(integral)
        taus.append(_as_float(entry.tau_deg))

    areas = [i.area for i in integrals]
    sigmas = [i.sigma for i in integrals]
    fit = fit_stokes(taus, areas, sigmas)
    result = SeriesResult(series=series, polarisation=pol, reduced=reduced,
                          integrals=integrals, taus=taus, fit=fit,
                          missing=missing, errors=errors, warnings=warnings_,
                          gain_normalised=bool(normalise_gain))
    if normalise_gain and result.has_unknown_gain:
        warnings_.append(
            "gain normalisation was requested but some scans have no recorded "
            "gain; those scans were left unscaled and the series is still "
            "mixed-unit")
    return result


def summarise(result: SeriesResult, cfg: PandaConfig) -> SeriesSummary:
    def mean_of(attr):
        vals = [getattr(r, attr) for r in result.reduced
                if getattr(r, attr) is not None and math.isfinite(getattr(r, attr))]
        return float(np.mean(vals)) if vals else None

    s = SeriesSummary(
        peak=result.series.peak,
        temperature_K=result.series.temperature_K,
        polarisation=result.polarisation,
        n_scans=len(result.reduced),
        energy_eV=mean_of("energy_eV"),
        field_T=mean_of("field_T"),
        sample_temperature_K=mean_of("temperature_K"),
        fit=result.fit,
        gain_summary=result.gain_summary(),
        mixed_gain=result.mixed_gain,
        gain_normalised=result.gain_normalised,
    )
    return classify(s, cfg)
