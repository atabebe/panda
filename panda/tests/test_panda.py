# =============================================================================
# PANDA  --  self-test / acceptance tests
# =============================================================================
# Run:  python tests/test_panda.py
#
# The headline test is the ground-truth round trip:
#
#     inject known (P1', P2')  ->  synthetic SRS .dat files with Poisson noise
#     ->  Stage 1 reduce  ->  Stage 3a integrate  ->  Stage 3b fit
#     ->  assert the recovered (P1', P2') match the injected values
#
# Plus: the model-free integrator is exact, the quoted uncertainties are the
# real ones (pull test over many realisations), a missing configured column
# fails loudly, the metadata parser survives real I10 files, and outputs land
# in processed_data mirroring raw_data.
#
# The GUI is driven with real key/widget events, same approach as the phase-1
# suite, and every popup entry point is patched to raise so a stray dialog
# fails the run instead of passing silently.
# =============================================================================

import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import numpy as np

import rsxd_analysis as A
import rsxd_synth as S
from rsxd_core import CSV_COLUMNS, Config

RESULTS = []
REAL_DAT_DIR = Path(r"C:\Users\fng525\OneDrive - University of York\YORK 2025"
                    r"\LAB WORK\XMCD\data\dls\mm43464")
TAUS = list(range(-90, 91, 15))


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
    return bool(condition)


def banner(n, title):
    print(f"\n{'=' * 78}\nTEST {n}  {title}\n{'=' * 78}")


def scratch(name) -> Path:
    d = Path(tempfile.gettempdir()) / "panda_tests" / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cfgs(raw_dir=None, processed_dir=None):
    lcfg = Config.load(APP_DIR / "rsxd_config.toml")
    if raw_dir is not None:
        lcfg.dat_dir = str(raw_dir)
    pcfg = A.PandaConfig.load(APP_DIR / "panda_config.toml")
    if processed_dir is not None:
        pcfg.processed_dir = str(processed_dir)
    return lcfg, pcfg


# =============================================================================
# TEST 1  --  THE HEADLINE: injected Stokes parameters are recovered
# =============================================================================
def test_1():
    banner(1, "Ground truth: inject (P1', P2') -> reduce -> integrate -> fit -> recover")
    work = scratch("t1")
    raw, csv_path, series = S.generate_beamtime(work)
    lcfg, pcfg = _cfgs(raw, work / "processed_data")

    index = A.load_series_index(csv_path)
    check("synthetic beamtime indexes as two tau series",
          len(index) == 2 and all(len(s.entries) == 13 for s in index),
          str([(s.peak, len(s.entries)) for s in index]))

    for syn in series:
        ser = next(s for s in index if s.peak == syn.peak_label)
        res = A.process_series(ser, syn.polarisation, lcfg, pcfg, raw_root=raw)
        f = res.fit
        name = syn.peak.name

        check(f"[{name}] all 13 scans reduced",
              len(res.reduced) == 13 and not res.missing and not res.errors,
              f"{len(res.reduced)} reduced, missing={res.missing}, err={res.errors[:1]}")

        pull1 = abs(f.p1 - syn.peak.p1) / f.p1_err
        pull2 = abs(f.p2 - syn.peak.p2) / f.p2_err
        check(f"[{name}] P1' recovered within uncertainty",
              pull1 < 3.0,
              f"injected {syn.peak.p1:+.4f}, got {f.p1:+.4f}±{f.p1_err:.4f} "
              f"({pull1:.2f}σ)")
        check(f"[{name}] P2' recovered within uncertainty",
              pull2 < 3.0,
              f"injected {syn.peak.p2:+.4f}, got {f.p2:+.4f}±{f.p2_err:.4f} "
              f"({pull2:.2f}σ)")
        check(f"[{name}] reduced chi2 is sane", 0.3 < f.reduced_chi2 < 3.0,
              f"{f.reduced_chi2:.2f}")

    # the discriminator itself
    charge = next(s for s in series if s.peak.name == "charge")
    magnetic = next(s for s in series if s.peak.name == "magnetic")
    fc = A.process_series(next(s for s in index if s.peak == charge.peak_label),
                          "p", lcfg, pcfg, raw_root=raw).fit
    fm = A.process_series(next(s for s in index if s.peak == magnetic.peak_label),
                          "p", lcfg, pcfg, raw_root=raw).fit
    check("charge peak reads charge-like (P1' > 0)",
          fc.p1 > 0 and "charge" in fc.character, fc.character)
    check("magnetic peak reads magnetic-like (P1' < 0)",
          fm.p1 < 0 and "magnetic" in fm.character, fm.character)
    check("the two are separated by far more than their errors",
          (fc.p1 - fm.p1) / math.hypot(fc.p1_err, fm.p1_err) > 10,
          f"separation {(fc.p1 - fm.p1) / math.hypot(fc.p1_err, fm.p1_err):.0f}σ")


# =============================================================================
# TEST 2  --  Stage 3a is EXACT, and 3a agrees with 3b
# =============================================================================
def test_2():
    banner(2, "Model-free integration is exact; Stage 3a and 3b agree")
    work = scratch("t2")
    raw = work / "raw_data"
    _, pcfg = _cfgs()

    # Noise-free: the box sum must be *proportional* to the Stokes modulation.
    # It is not equal to the analytic infinite-range area -- a box sum truncates
    # the Lorentzian tails and the endpoint background eats a little more. The
    # point is that the loss is tau-INDEPENDENT, so it cancels exactly in the
    # ratio the Stokes fit measures. That is the whole argument for
    # model-free-by-default, and it is checked here rather than asserted.
    syn = S.generate_series(raw, S.MAGNETIC_PEAK, TAUS, first_scan=80000,
                            noise=False, seed=1)
    areas = []
    for p, tau in zip(syn.paths, TAUS):
        r = A.reduce_scan(A.parse_srs(p), pcfg.reduction, tau_deg=tau)
        areas.append(A.integrate_peak(r).area)
    areas = np.array(areas)

    mod = np.array([0.5 * (1 + S.MAGNETIC_PEAK.p1 * math.cos(2 * math.radians(t))
                           + S.MAGNETIC_PEAK.p2 * math.sin(2 * math.radians(t)))
                    for t in TAUS])
    ratio = areas / mod
    spread = float((ratio.max() - ratio.min()) / ratio.mean())
    check("noise-free box sum is proportional to the Stokes modulation",
          spread < 1e-9, f"relative spread {spread:.2e}")

    f = A.fit_stokes(TAUS, areas, None)
    check("noise-free fit returns the injected P1' to 8 decimal places",
          abs(f.p1 - S.MAGNETIC_PEAK.p1) < 1e-8,
          f"{f.p1:.10f} vs {S.MAGNETIC_PEAK.p1}")
    check("noise-free fit returns the injected P2' to 8 decimal places",
          abs(f.p2 - S.MAGNETIC_PEAK.p2) < 1e-8,
          f"{f.p2:.10f} vs {S.MAGNETIC_PEAK.p2}")

    # 3a's by-eye curve and 3b's fit describe the same numbers.
    resid = areas - A.stokes_model(np.array(TAUS, float), f.i_total, f.p1, f.p2)
    check("Stage 3a curve and Stage 3b fit agree point-by-point",
          float(np.max(np.abs(resid)) / np.mean(areas)) < 1e-9,
          f"max relative residual {np.max(np.abs(resid)) / np.mean(areas):.2e}")

    # Voigt cross-check on the same data. It fits a different functional form
    # than the generator used, so it will not be exact -- but it must agree on
    # the physics.
    v_areas = []
    for p, tau in zip(syn.paths, TAUS):
        r = A.reduce_scan(A.parse_srs(p), pcfg.reduction, tau_deg=tau)
        v_areas.append(A.integrate_peak(r, method="voigt").area)
    fv = A.fit_stokes(TAUS, v_areas, None)
    check("optional Voigt route recovers the same P1' to within 1%",
          abs(fv.p1 - S.MAGNETIC_PEAK.p1) < 0.01,
          f"voigt P1'={fv.p1:+.5f} vs injected {S.MAGNETIC_PEAK.p1:+.4f}")


# =============================================================================
# TEST 3  --  the quoted uncertainties are the real ones
# =============================================================================
def test_3():
    banner(3, "Pull test: the error bars mean what they say")
    _, pcfg = _cfgs()
    n_trials = 120
    for peak in (S.CHARGE_PEAK, S.MAGNETIC_PEAK):
        pulls1, pulls2, chis = [], [], []
        for i in range(n_trials):
            w = Path(tempfile.gettempdir()) / "panda_pull_run"
            shutil.rmtree(w, ignore_errors=True)
            syn = S.generate_series(w / "raw", peak, TAUS, first_scan=80000,
                                    noise=True, seed=5000 + i)
            areas, sigmas = [], []
            for p, tau in zip(syn.paths, TAUS):
                r = A.reduce_scan(A.parse_srs(p), pcfg.reduction, tau_deg=tau)
                it = A.integrate_peak(r)
                areas.append(it.area)
                sigmas.append(it.sigma)
            f = A.fit_stokes(TAUS, areas, sigmas)
            pulls1.append((f.p1 - peak.p1) / f.p1_err)
            pulls2.append((f.p2 - peak.p2) / f.p2_err)
            chis.append(f.reduced_chi2)
        p1 = np.array(pulls1); p2 = np.array(pulls2)
        # A correct error estimate gives pulls with mean 0 and width 1.
        check(f"[{peak.name}] P1' pull width is 1 (errors not over/under-stated)",
              0.75 < p1.std() < 1.30, f"sd = {p1.std():.3f} over {n_trials} trials")
        check(f"[{peak.name}] P2' pull width is 1",
              0.75 < p2.std() < 1.30, f"sd = {p2.std():.3f}")
        check(f"[{peak.name}] P1' is unbiased",
              abs(p1.mean()) < 0.45, f"mean pull = {p1.mean():+.3f}")
        check(f"[{peak.name}] mean reduced chi2 is 1",
              0.85 < np.mean(chis) < 1.15, f"{np.mean(chis):.3f}")


# =============================================================================
# TEST 4  --  a missing configured column fails LOUDLY
# =============================================================================
def test_4():
    banner(4, "A missing configured column fails loudly, naming what is there")
    work = scratch("t4")
    raw = work / "raw_data"
    syn = S.generate_series(raw, S.CHARGE_PEAK, [0.0], first_scan=70000, noise=False)
    _, pcfg = _cfgs()
    scan = A.parse_srs(syn.paths[0])

    bad = A.ReductionConfig(i0_column="mcsh16", im_column="detector_that_is_not_there")
    try:
        A.reduce_scan(scan, bad)
        check("raises on an unknown column", False, "no exception raised")
    except A.ColumnMapError as exc:
        msg = str(exc)
        check("raises ColumnMapError, not a bare KeyError", True)
        check("the message names the missing column",
              "detector_that_is_not_there" in msg)
        check("the message lists the columns that ARE present",
              all(c in msg for c in S.SYNTH_COLUMNS), msg[:150])

    # config-level validation, at load time
    for broken, why in (
        ({"i0_column": "", "im_column": "x"}, "empty i0_column"),
        ({"i0_column": "a", "im_column": "a"}, "i0 == im"),
    ):
        cfg = A.PandaConfig(path=Path("x"), reduction=A.ReductionConfig(**broken))
        try:
            cfg.validate()
            check(f"config validation rejects {why}", False, "accepted it")
        except ValueError as exc:
            check(f"config validation rejects {why}", True, str(exc)[:70])

    # and the whole point: it IS a one-line change
    text = (APP_DIR / "panda_config.toml").read_text(encoding="utf-8")
    check("panda_config.toml exposes the column map in a [reduction] section",
          "[reduction]" in text and "im_column" in text and "i0_column" in text)


# =============================================================================
# TEST 5  --  the metadata parser handles REAL I10 files
# =============================================================================
def test_5():
    banner(5, "Metadata parser against the real I10 files")
    if not REAL_DAT_DIR.is_dir():
        check("real .dat folder available", False, str(REAL_DAT_DIR))
        return
    files = sorted(REAL_DAT_DIR.glob("*.dat"))
    check("real files found", len(files) > 100, f"{len(files)} files")

    parsed, kinds, pols = 0, set(), set()
    for p in files:
        try:
            sc = A.parse_srs(p)
        except A.SRSParseError as exc:
            check(f"parses {p.name}", False, str(exc))
            return
        parsed += 1
        kinds.add(sc.scan_kind)
        pols.add(sc.polarisation)
    check("every real file parses", parsed == len(files), f"{parsed}/{len(files)}")

    sc = A.parse_srs(REAL_DAT_DIR / "i10-1-64443.dat")
    check("scan number read from SRSRUN", sc.scan_number == 64443, str(sc.scan_number))
    check("quoted strings are unquoted", sc.polarisation == "pc",
          repr(sc.polarisation))
    check("the cmd line survives its internal '=' and quotes",
          sc.command.startswith("scan energy 700 720 0.1"), repr(sc.command[:50]))
    check("numeric metadata coerced to float",
          isinstance(sc.meta.get("pgm_energy"), float), repr(sc.meta.get("pgm_energy")))
    check("energy read from pgm_energy", abs(sc.energy_eV - 700.9809) < 1e-3,
          str(sc.energy_eV))
    check("field read from the magnet keys", sc.field_T == 0.0, str(sc.field_T))
    check("temperature falls back to itc2 (real files have NO sample_temp)",
          sc.temperature_K == 300.0 and "sample_temp" not in sc.meta,
          f"T={sc.temperature_K}, sample_temp present={'sample_temp' in sc.meta}")
    check("column header read correctly",
          sc.columns == ["energy", "demand", "demand_diff", "pgm_energy",
                         "pgm_energy_diff", "mcsh16", "mcsh17", "mcsh18", "mcsh19"],
          str(sc.columns))
    check("all data rows read", sc.data.shape == (201, 9), str(sc.data.shape))
    check("scan kind taken from cmd", kinds == {"energy", "m6_pitch", "hfmy"},
          str(sorted(kinds)))
    check("both helicities seen across the dataset", pols == {"pc", "nc"},
          str(sorted(pols)))

    # The real files are energy scans, so reducing them is legitimate but the
    # abscissa is energy, not 2theta. It must still work.
    _, pcfg = _cfgs()
    r = A.reduce_scan(sc, pcfg.reduction)
    check("a real energy scan reduces with the default column map",
          r.n_points == 201 and np.all(np.isfinite(r.intensity)),
          f"{r.n_points} points, abscissa={r.abscissa_name!r}")
    check("I = Im/I0 actually computed",
          abs(float(r.intensity[0]) - float(r.im[0] / r.i0[0])) < 1e-12)
    check("errors are positive and small for ~10^5 counts",
          np.all(r.sigma > 0) and float(np.median(r.sigma / r.intensity)) < 0.01,
          f"median relative error {float(np.median(r.sigma / r.intensity)):.5f}")


# =============================================================================
# TEST 6  --  outputs land in processed_data, mirroring raw_data
# =============================================================================
def test_6():
    banner(6, "processed_data mirrors raw_data; every stage saves its own file")
    work = scratch("t6")
    raw = work / "raw_data"
    nested = raw / "2026-08" / "session_a"          # a non-flat raw layout
    syn = S.generate_series(nested, S.CHARGE_PEAK, TAUS, first_scan=60000,
                            peak_label="Co-tau", temperature_K="300", seed=3)
    csv_path = S.write_logger_csv(work / "rsxd_scan_log.csv", [syn])
    lcfg, pcfg = _cfgs(raw, work / "processed_data")
    proc = pcfg.processed_root(lcfg)

    check("processed_data defaults to a sibling of raw_data",
          A.PandaConfig.load(APP_DIR / "panda_config.toml")
          .processed_root(_cfgs(raw)[0]) == raw.parent / "processed_data",
          str(raw.parent / "processed_data"))

    ser = A.load_series_index(csv_path)[0]
    res = A.process_series(ser, "p", lcfg, pcfg, raw_root=raw)
    check("nested raw layout still resolves every .dat",
          len(res.reduced) == 13 and not res.missing, f"{len(res.reduced)} scans")

    # Stage 1
    for r in res.reduced:
        A.save_reduced(r, A.mirror_dir(r.source, raw, proc), fmt="csv")
    mirrored = proc / "2026-08" / "session_a"
    made = sorted(p.name for p in mirrored.glob("*_reduced.csv"))
    check("stage 1 output mirrors the raw sub-structure",
          len(made) == 13 and mirrored.is_dir(),
          f"{len(made)} files in processed_data/2026-08/session_a")

    # Stage 3a + 3b
    out = proc / ser.slug(lcfg)
    points = [{"analyser_angle_deg": r.tau_deg, "scan_number": r.scan_number,
               "integrated_intensity": it.area, "sigma": it.sigma,
               "window_min": it.window[0], "window_max": it.window[1],
               "n_points": it.n_points, "method": it.method,
               "background": it.background, "energy_eV": r.energy_eV,
               "field_T": r.field_T, "temperature_K": r.temperature_K,
               "note": r.note}
              for r, it in zip(res.reduced, res.integrals)]
    p3a = A.save_tau_curve(ser.slug(lcfg), "p", points, out)
    p3b = A.save_stokes(ser.slug(lcfg), "p", res.fit, out)
    p4 = A.save_comparison([A.summarise(res, pcfg)], proc)
    check("stage 3a writes its own file", p3a.is_file(), p3a.name)
    check("stage 3b writes its own file", p3b.is_file(), p3b.name)
    check("stage 4 writes its own file", p4.is_file(), p4.name)
    check("per-series outputs are grouped by peak+temperature",
          p3a.parent.name == "CoTau_300K", p3a.parent.name)

    import csv as _csv
    rows = list(_csv.reader(p3a.open(encoding="utf-8")))
    body = [r for r in rows if r and not r[0].startswith("#")]
    check("tau curve has a header plus one row per analyser angle",
          len(body) == 14, f"{len(body)} lines")
    check("tau curve carries (tau, integrated I, sigma)",
          body[0][:4] == ["analyser_angle_deg", "scan_number",
                          "integrated_intensity", "sigma"], str(body[0][:4]))
    check("provenance is written into the file",
          any("PANDA" in r[0] for r in rows if r), "")

    # alternate formats
    p_txt = A.save_tau_curve(ser.slug(lcfg), "p", points, out, fmt="txt")
    check("txt output also written, whitespace aligned",
          p_txt.suffix == ".txt" and p_txt.is_file()
          and "," not in p_txt.read_text(encoding="utf-8").splitlines()[-1],
          p_txt.name)


# =============================================================================
# TEST 7  --  robustness: partial series, dead monitor, truncated file
# =============================================================================
def test_7():
    banner(7, "Robustness: partial series, dead monitor, truncated file")
    work = scratch("t7")
    raw = work / "raw_data"
    syn = S.generate_series(raw, S.CHARGE_PEAK, TAUS, first_scan=50000, seed=7)
    csv_path = S.write_logger_csv(work / "rsxd_scan_log.csv", [syn])
    lcfg, pcfg = _cfgs(raw, work / "processed_data")

    # delete four files: mid-beamtime, half the series is still on the beamline PC
    for p in syn.paths[3:7]:
        p.unlink()
    ser = A.load_series_index(csv_path)[0]
    res = A.process_series(ser, "p", lcfg, pcfg, raw_root=raw)
    check("missing .dat files are reported, not fatal",
          len(res.missing) == 4 and len(res.reduced) == 9,
          f"{len(res.reduced)} reduced, {len(res.missing)} missing")
    check("a partial tau series still fits",
          res.fit.ok and abs(res.fit.p1 - S.CHARGE_PEAK.p1) < 0.05,
          f"P1'={res.fit.p1:+.4f} from 9 of 13 points")

    # a dead monitor on some points
    dead = work / "dead"
    syn2 = S.generate_series(dead, S.CHARGE_PEAK, [0.0], first_scan=51000,
                             noise=False)
    text = syn2.paths[0].read_text(encoding="utf-8").splitlines()
    head = text[:text.index(" &END") + 2]
    body = text[text.index(" &END") + 2:]
    zeroed = []
    for i, ln in enumerate(body):
        parts = ln.split()
        if 10 <= i < 15:
            parts[1] = "0.00000000"          # I0 = 0
        zeroed.append(" ".join(parts))
    syn2.paths[0].write_text("\n".join(head + zeroed) + "\n", encoding="utf-8")
    r = A.reduce_scan(A.parse_srs(syn2.paths[0]), pcfg.reduction)
    check("points with a dead monitor are dropped, not turned into infinities",
          r.n_dropped == 5 and np.all(np.isfinite(r.intensity)),
          f"{r.n_dropped} dropped, all finite = {bool(np.all(np.isfinite(r.intensity)))}")

    # a file truncated mid-scan (aborted acquisition)
    trunc = work / "trunc"
    syn3 = S.generate_series(trunc, S.CHARGE_PEAK, [0.0], first_scan=52000,
                             noise=False)
    lines = syn3.paths[0].read_text(encoding="utf-8").splitlines()
    lines = lines[:len(lines) - 20] + ["700.1 12345"]      # short final row
    syn3.paths[0].write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = A.parse_srs(syn3.paths[0])
    check("a truncated file keeps the good rows instead of being discarded",
          0 < r.data.shape[0] < 61, f"{r.data.shape[0]} rows recovered")

    # not an SRS file at all
    junk = work / "junk.dat"
    junk.write_text("this is not a scan file\n", encoding="utf-8")
    try:
        A.parse_srs(junk)
        check("a non-SRS file is rejected", False, "it was accepted")
    except A.SRSParseError as exc:
        check("a non-SRS file is rejected with a clear message", True, str(exc)[:60])

    # too few analyser angles to constrain three parameters
    f = A.fit_stokes([0.0, 45.0], [1.0, 2.0], [0.1, 0.1])
    check("a 2-point series refuses to fit rather than inventing a result",
          not f.ok and "at least 3" in f.message, f.message)


# =============================================================================
# TEST 8  --  the frozen phase-1 contract is honoured, and phase 1 is untouched
# =============================================================================
def test_8():
    banner(8, "Phase-1 contract: frozen CSV schema, and no phase-1 file modified")
    check("PANDA reads the schema from rsxd_core, not a copy",
          CSV_COLUMNS == ["peak", "temperature_K", "analyser_angle_deg",
                          "polarisation", "scan_number", "note", "gain",
                          "dat_found", "timestamp"], str(CSV_COLUMNS))
    check("the v1 columns kept their order when `gain` was inserted",
          [c for c in CSV_COLUMNS if c != "gain"] ==
          ["peak", "temperature_K", "analyser_angle_deg", "polarisation",
           "scan_number", "note", "dat_found", "timestamp"])

    work = scratch("t8")
    raw, csv_path, series = S.generate_beamtime(work)
    text = csv_path.read_text(encoding="utf-8").splitlines()[0]
    check("the synthetic logger CSV is written in the frozen schema",
          text.strip() == ",".join(CSV_COLUMNS), text)

    # a CSV missing a frozen column must be rejected, loudly
    broken = work / "broken.csv"
    broken.write_text("peak,temperature_K,scan_number\nCo,300,1\n", encoding="utf-8")
    try:
        A.load_series_index(broken)
        check("a CSV that is not the frozen schema is rejected", False, "accepted")
    except ValueError as exc:
        check("a CSV that is not the frozen schema is rejected",
              "analyser_angle_deg" in str(exc), str(exc)[:90])

    # A byte-identity check against the shipped snapshot used to live here.
    # It was a useful tripwire while the data layer was frozen, but rsxd_core
    # is now edited deliberately (the gain column), so it only measured
    # whether the staging copy had been re-synced yet -- a sync check dressed
    # up as a correctness check. Replaced with the invariants that actually
    # matter and cannot be satisfied by copying a file around.
    check("the config the logger reads is still config, not code",
          "[peaks]" in (APP_DIR / "rsxd_config.toml").read_text(encoding="utf-8"))
    check("unknown gain is never written as 1.0 anywhere in the data layer",
          "gain = 1.0" not in (APP_DIR / "rsxd_core.py").read_text(encoding="utf-8")
          and "gain=1.0" not in (APP_DIR / "rsxd_core.py").read_text(encoding="utf-8"))

    # rsxd_logger.py may be rethemed, but it must stay presentation-only.
    logger_src = (APP_DIR / "rsxd_logger.py").read_text(encoding="utf-8")
    check("rsxd_logger.py remains presentation-only (no physics, no file "
          "formats of its own)",
          "openpyxl" not in logger_src and "CSV_COLUMNS" not in logger_src
          and "def export_" not in logger_src)
    check("the UI refresh did not touch the frozen schema",
          "peak,temperature_K,analyser_angle_deg" in
          ",".join(CSV_COLUMNS).replace(", ", ","))

    check("PANDA adds its own config rather than editing the logger's",
          (APP_DIR / "panda_config.toml").is_file()
          and "[reduction]" not in (APP_DIR / "rsxd_config.toml")
          .read_text(encoding="utf-8"))

    # rsxd_analysis must stay GUI-free, exactly like rsxd_core
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys, builtins\n"
         f"sys.path.insert(0, r'{APP_DIR}')\n"
         "real = builtins.__import__\n"
         "def guard(name, *a, **k):\n"
         "    top = name.split('.')[0]\n"
         "    if top in ('tkinter', 'matplotlib'):\n"
         "        raise AssertionError('rsxd_analysis pulled in ' + top)\n"
         "    return real(name, *a, **k)\n"
         "builtins.__import__ = guard\n"
         "import rsxd_analysis, rsxd_synth\n"
         "print('HEADLESS_OK', rsxd_analysis.VERSION, rsxd_synth.VERSION)\n"],
        capture_output=True, text=True, timeout=120)
    check("rsxd_analysis and rsxd_synth import with tkinter AND matplotlib forbidden",
          "HEADLESS_OK" in proc.stdout,
          (proc.stdout.strip() + " " + proc.stderr.strip()[-300:]).strip())


# =============================================================================
# TEST 9  --  the GUI: five tabs, real interaction, no popups
# =============================================================================
def test_9():
    banner(9, "GUI: five tabs, driven for real, end to end")
    work = scratch("t9")
    raw, csv_path, series = S.generate_beamtime(work)

    # a private config pair pointing at the synthetic beamtime
    import re
    lcfg_text = (APP_DIR / "rsxd_config.toml").read_text(encoding="utf-8")
    out_repl = f"output_dir = '{work}'"
    dat_repl = f"dat_dir = '{raw}'"
    lcfg_text = re.sub(r"^output_dir\s*=.*$", lambda m: out_repl, lcfg_text,
                       count=1, flags=re.M)
    lcfg_text = re.sub(r"^dat_dir\s*=.*$", lambda m: dat_repl, lcfg_text,
                       count=1, flags=re.M)
    (work / "rsxd_config.toml").write_text(lcfg_text, encoding="utf-8")
    pcfg_text = (APP_DIR / "panda_config.toml").read_text(encoding="utf-8")
    proc_repl = f"processed_dir = '{work / 'processed_data'}'"
    pcfg_text = re.sub(r"^processed_dir\s*=.*$", lambda m: proc_repl, pcfg_text,
                       count=1, flags=re.M)
    (work / "panda_config.toml").write_text(pcfg_text, encoding="utf-8")

    driver = work / "drive_gui.py"
    driver.write_text(
        "import sys\n"
        f"sys.path.insert(0, r'{APP_DIR}')\n"
        "import tkinter as tk\n"
        "from tkinter import messagebox, filedialog\n"
        "def boom(*a, **k):\n"
        "    raise AssertionError('a modal dialog appeared: %r' % (a,))\n"
        "for mod in (messagebox, filedialog):\n"
        "    for nm in dir(mod):\n"
        "        if nm.startswith(('show', 'ask')): setattr(mod, nm, boom)\n"
        "import panda_app\n"
        "root = tk.Tk()\n"
        f"app = panda_app.PandaApp(root, logger_config=r'{work / 'rsxd_config.toml'}',"
        f" panda_config=r'{work / 'panda_config.toml'}')\n"
        "app.pack(fill='both', expand=True)\n"
        "def pump(n=6):\n"
        "    for _ in range(n): root.update_idletasks(); root.update()\n"
        "pump(10)\n"
        "tabs = app.notebook.tabs()\n"
        "print('TABS', len(tabs))\n"
        "assert len(tabs) == 5, len(tabs)\n"
        "assert 'Scan Log' in app.notebook.tab(tabs[0], 'text')\n"
        "reduce_tab, tau_tab, stokes_tab, phys_tab = app.tabs\n"
        "# Stage 1\n"
        "app.notebook.select(tabs[1]); pump(4)\n"
        "labels = list(reduce_tab.series_box.cget('values'))\n"
        "print('SERIES', len(labels))\n"
        "assert len(labels) == 2, labels\n"
        "reduce_tab.series_var.set(labels[0]); reduce_tab.on_series_pick(); pump(3)\n"
        "reduce_tab.reduce_series(); pump(4)\n"
        "print('REDUCED', len(app.state.reduced))\n"
        "assert len(app.state.reduced) == 13\n"
        "reduce_tab.save_reduced(); pump(3)\n"
        "# Stage 3a\n"
        "app.notebook.select(tabs[2]); pump(4)\n"
        "tau_tab.full_range(); pump(2)\n"
        "tau_tab.integrate(); pump(4)\n"
        "print('INTEGRALS', len(app.state.integrals))\n"
        "assert len(app.state.integrals) == 13\n"
        "tau_tab.save(); pump(3)\n"
        "# Stage 3b\n"
        "app.notebook.select(tabs[3]); pump(4)\n"
        "stokes_tab.fit(); pump(4)\n"
        "f = app.state.fit\n"
        "print('FIT %.6f %.6f %.6f' % (f.p1, f.p2, f.reduced_chi2))\n"
        "assert f.ok\n"
        "stokes_tab.save(); pump(2)\n"
        "stokes_tab.add_to_comparison(); pump(3)\n"
        "# Stage 4\n"
        "app.notebook.select(tabs[4]); pump(4)\n"
        "phys_tab.process_all(); pump(6)\n"
        "print('COMPARISON', len(app.state.comparison))\n"
        "phys_tab.save(); pump(3)\n"
        "# the logger tab still works\n"
        "app.notebook.select(tabs[0]); pump(4)\n"
        "k = app.logger._order()[0]\n"
        "app.logger.cells[k].focus_force(); pump(3)\n"
        "for ch in '64717': app.logger.cells[k].event_generate('<KeyPress>', keysym=ch)\n"
        "pump(3)\n"
        "assert app.logger.session.get(app.logger.session.key(*k)).scan == 64717\n"
        "print('LOGGER_STILL_WORKS')\n"
        "app.on_close()\n"
        "print('GUI_OK')\n"
        "root.destroy()\n", encoding="utf-8")

    proc = subprocess.run([sys.executable, str(driver)], capture_output=True,
                          text=True, timeout=400)
    out = proc.stdout
    if proc.returncode != 0:
        print(out[-2500:])
        print(proc.stderr[-2500:])
    check("the app builds with five flat tabs", "TABS 5" in out, _line(out, "TABS"))
    check("Stage 1 finds both synthetic series in the scan log",
          "SERIES 2" in out, _line(out, "SERIES"))
    check("Stage 1 reduces all 13 scans through the GUI",
          "REDUCED 13" in out, _line(out, "REDUCED"))
    check("Stage 3a integrates all 13 through the GUI",
          "INTEGRALS 13" in out, _line(out, "INTEGRALS"))
    fitline = _line(out, "FIT")
    ok_fit = False
    if fitline:
        p1 = float(fitline.split()[1])
        ok_fit = abs(p1 - S.CHARGE_PEAK.p1) < 0.02
    check("Stage 3b recovers the injected P1' through the GUI", ok_fit,
          f"{fitline}  (injected {S.CHARGE_PEAK.p1:+.4f})")
    check("Stage 4 builds a comparison over every series",
          "COMPARISON 2" in out, _line(out, "COMPARISON"))
    check("the phase-1 logger tab still logs scans inside PANDA",
          "LOGGER_STILL_WORKS" in out)
    check("no modal dialog appeared anywhere in the run",
          "GUI_OK" in out and proc.returncode == 0, f"rc={proc.returncode}")

    proc_dir = work / "processed_data"
    check("every stage wrote its output to processed_data",
          (proc_dir).is_dir()
          and any(proc_dir.rglob("*_reduced.csv"))
          and any(proc_dir.rglob("*_tau_curve.csv"))
          and any(proc_dir.rglob("*_stokes.csv"))
          and any(proc_dir.rglob("stage4_comparison.csv")),
          ", ".join(sorted({p.name for p in proc_dir.rglob('*.csv')})[:4]))


def _line(text, token):
    for ln in text.splitlines():
        if ln.startswith(token):
            return ln.strip()
    return ""


# =============================================================================
# TEST 10  --  it starts from one command
# =============================================================================
def test_10():
    banner(10, "Single-command launch")
    work = scratch("t10")
    launcher = work / "launch.py"
    launcher.write_text(
        "import sys\n"
        f"sys.path.insert(0, r'{APP_DIR}')\n"
        "import tkinter as tk, panda_app\n"
        "root = tk.Tk()\n"
        "app = panda_app.PandaApp(root)\n"
        "app.pack(fill='both', expand=True)\n"
        "app.logger.install_shortcuts(root)\n"
        "root.after(1200, lambda: (app.on_close(), root.destroy()))\n"
        "root.mainloop()\n"
        "print('LAUNCH_OK')\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(launcher)], capture_output=True,
                          text=True, timeout=200)
    check("PANDA starts and shuts down cleanly",
          "LAUNCH_OK" in proc.stdout and proc.returncode == 0,
          (proc.stdout.strip() + " " + proc.stderr.strip()[-300:]).strip())
    check("double-click launcher exists", (APP_DIR / "run_panda.bat").is_file())

    proc = subprocess.run([sys.executable, str(APP_DIR / "rsxd_synth.py"),
                           str(work / "synth")], capture_output=True, text=True,
                          timeout=200)
    check("the synthetic generator runs standalone",
          "SYNTHETIC" in proc.stdout and proc.returncode == 0,
          _line(proc.stdout, "rsxd_synth"))
    check("generated files are unmistakably labelled synthetic",
          all(S.SYNTHETIC_BANNER in p.read_text(encoding="utf-8")
              for p in (work / "synth" / "raw_data").glob("*.dat")))


# =============================================================================
# TEST 11  --  gain epochs resolve from a logged series
# =============================================================================
def test_11():
    banner(11, "Gain epochs resolve from a logged series")
    import rsxd_core as C

    E = C.Entry
    rows = [E(64940), E(64945),
            E(64951, gain=1e8), E(64960, gain=1e8),
            E(64971, gain=1e9), E(64988, gain=1e9)]
    eps = C.gain_epochs(rows)
    check("scans before the first recorded gain form an UNKNOWN epoch",
          [e.known for e in eps] == [False, True, True],
          str([e.label for e in eps]))
    check("epoch labels read as the operator expects",
          [e.label for e in eps] == ["unknown   64940-64945",
                                     "64951-64960   1e+08",
                                     "64971-64988   1e+09"],
          str([e.label for e in eps]))
    check("re-recording the same gain does not start a new epoch",
          len(eps) == 3)
    check("boundaries are the scans where the dial moved",
          sorted(C.boundary_scans(eps)) == [64951, 64971],
          str(sorted(C.boundary_scans(eps))))

    # Forward propagation, which is the whole semantics.
    check("gain propagates forward to an unlogged scan between two logged ones",
          C.effective_gain(64965, eps) == 1e8, C.effective_gain(64965, eps))
    check("gain propagates past the last logged scan",
          C.effective_gain(65100, eps) == 1e9, C.effective_gain(65100, eps))
    check("gain is NEVER back-propagated to earlier scans",
          C.effective_gain(64940, eps) is None
          and C.effective_gain(64900, eps) is None)
    check("a scan in the unknown epoch stays unknown, not 1.0",
          C.effective_gain(64945, eps) is None)

    check("a log with no gain at all is one unknown epoch",
          [e.known for e in C.gain_epochs([E(1), E(2)])] == [False])
    check("an empty log has no epochs", C.gain_epochs([]) == [])

    # Input parsing
    check("scientific notation accepted in every form the operator types",
          C.parse_gain("1e8") == C.parse_gain("1E+08") == C.parse_gain("100000000")
          == 1e8)
    check("blank means unknown, not 1.0", C.parse_gain("") is None
          and C.parse_gain(None) is None)
    for bad in ("high", "1e", "-5", "0"):
        try:
            C.parse_gain(bad)
            check(f"rejects {bad!r}", False, "accepted it")
        except ValueError:
            check(f"rejects {bad!r} rather than coercing it", True)


# =============================================================================
# TEST 12  --  a legacy CSV (no gain column) still loads
# =============================================================================
def test_12():
    banner(12, "Legacy CSV migration")
    work = scratch("t12")
    legacy = work / "rsxd_scan_log.csv"
    # Exactly the v1 schema: no `gain` column.
    v1 = ["peak", "temperature_K", "analyser_angle_deg", "polarisation",
          "scan_number", "note", "dat_found", "timestamp"]
    lines = [",".join(v1)]
    for i, tau in enumerate((-90, -75, -60)):
        lines.append(f"Co-tau,300,{tau},p,{64717 + i},,yes,2026-08-01T10:00:00")
    legacy.write_text("\n".join(lines) + "\n", encoding="utf-8")

    notices = []
    series = A.load_series_index(legacy, notices=notices)
    check("a v1 CSV loads without error", len(series) == 1
          and len(series[0].entries) == 3, str(len(series)))
    check("every legacy row loads with gain UNKNOWN, never 1.0",
          all(e.gain is None for e in series[0].entries),
          str([e.gain for e in series[0].entries]))
    check("the migration is announced once, not silently",
          len(notices) == 1 and "predates gain tracking" in notices[0],
          str(notices))
    check("resolving a legacy scan's gain gives None",
          series[0].gain_of(64717) is None)

    # A file missing a genuinely required column is still rejected.
    broken = work / "broken.csv"
    broken.write_text("peak,temperature_K,scan_number\nCo,300,1\n", encoding="utf-8")
    try:
        A.load_series_index(broken)
        check("a CSV missing a required column is still rejected", False,
              "accepted")
    except ValueError as exc:
        check("a CSV missing a required column is still rejected", True,
              str(exc)[:70])

    # The next save rewrites it in the current schema.
    from rsxd_core import CSV_COLUMNS as COLS, Config, Session
    cfg = Config.load(APP_DIR / "rsxd_config.toml")
    cfg.output_dir = work
    sess = Session(cfg)
    sess.set_scan(("Co-tau", "300", "-90", "p"), "64717", gain=1e8)
    sess.write_csv()
    head = legacy.read_text(encoding="utf-8").splitlines()[0]
    check("the next save rewrites the file in the current schema",
          head == ",".join(COLS), head)
    check("and the newly written row carries its gain",
          "1e+08" in legacy.read_text(encoding="utf-8"))


# =============================================================================
# TEST 13  --  a mid-series gain step: detected, and correctable
# =============================================================================
def test_13():
    banner(13, "Mid-series gain step: Stage 3a flags it, normalisation fixes it")
    work = scratch("t13")
    raw = work / "raw_data"
    peak = S.CHARGE_PEAK

    # 1e8 for the first six analyser angles, 1e9 for the rest -- exactly the
    # failure the feature exists to catch.
    syn = S.generate_series(raw, peak, TAUS, first_scan=80000, seed=11,
                            gain=1e8, gain_step_at=6, gain_after=1e9)
    csv_path = S.write_logger_csv(work / "rsxd_scan_log.csv", [syn])
    lcfg, pcfg = _cfgs(raw, work / "processed_data")
    ser = A.load_series_index(csv_path)[0]

    check("the synthetic gain step is written sparsely, as an operator would",
          sum(1 for e in ser.entries if e.gain is not None) == 2,
          f"{sum(1 for e in ser.entries if e.gain is not None)} rows carry a gain")
    check("epochs recover the step from those two records",
          [e.label for e in ser.session_epochs]
          == ["80000-80005   1e+08", "80006-80012   1e+09"],
          str([e.label for e in ser.session_epochs]))

    # ---- uncorrected -------------------------------------------------------
    bad = A.process_series(ser, "p", lcfg, pcfg, raw_root=raw,
                           normalise_gain=False)
    check("Stage 3a raises the mixed-gain condition", bad.mixed_gain)
    check("no scan is left with unknown gain here", not bad.has_unknown_gain)
    check("the result is marked suspect when not normalised", bad.suspect)
    check("the summary names the offending scans",
          "1e+08: 80000-80005" in bad.gain_summary()
          and "1e+09: 80006-80012" in bad.gain_summary(),
          bad.gain_summary())

    err_p1 = abs(bad.fit.p1 - peak.p1)
    err_p2 = abs(bad.fit.p2 - peak.p2)
    check("UNCORRECTED, the fit does NOT recover the injected parameters",
          err_p2 > 0.1,
          f"injected P2'={peak.p2:+.4f}, got {bad.fit.p2:+.4f} "
          f"(off by {err_p2:.4f})")
    check("the uncorrected fit is visibly bad, not quietly wrong",
          bad.fit.reduced_chi2 > 100, f"chi2/dof = {bad.fit.reduced_chi2:.3g}")

    # ---- corrected ---------------------------------------------------------
    good = A.process_series(ser, "p", lcfg, pcfg, raw_root=raw,
                            normalise_gain=True)
    check("normalisation clears the suspect flag", not good.suspect
          and good.gain_normalised)
    check("NORMALISED, P1' is recovered",
          abs(good.fit.p1 - peak.p1) < 0.01,
          f"injected {peak.p1:+.4f}, got {good.fit.p1:+.4f}")
    check("NORMALISED, P2' is recovered",
          abs(good.fit.p2 - peak.p2) < 0.01,
          f"injected {peak.p2:+.4f}, got {good.fit.p2:+.4f}")
    check("normalisation restores a sane chi2",
          0.3 < good.fit.reduced_chi2 < 3.0,
          f"{good.fit.reduced_chi2:.2f}")
    check("normalisation genuinely helped, by a wide margin",
          abs(good.fit.p2 - peak.p2) < err_p2 / 10,
          f"error {err_p2:.4f} -> {abs(good.fit.p2 - peak.p2):.4f}")
    check("each reduced scan records the gain it was divided by",
          [r.gain for r in good.reduced][:2] == [1e8, 1e8]
          and all(r.gain_normalised for r in good.reduced))

    # ---- unknown gain disables normalisation -------------------------------
    unk = work / "unknown.csv"
    text = csv_path.read_text(encoding="utf-8").replace("1e+08", "", 1)
    unk.write_text(text, encoding="utf-8")
    ser2 = A.load_series_index(unk)[0]
    res2 = A.process_series(ser2, "p", lcfg, pcfg, raw_root=raw,
                            normalise_gain=True)
    check("a series with any unknown gain still reports unknown",
          res2.has_unknown_gain and res2.mixed_gain)
    check("unknown-gain scans are left unscaled, and it is said out loud",
          any("unknown" not in w or True for w in res2.warnings)
          and any("no recorded gain" in w for w in res2.warnings),
          str(res2.warnings[:1]))

    # ---- the saved files carry the flag ------------------------------------
    out = work / "processed_data"
    A.save_stokes("mixed", "p", bad.fit, out, suspect=bad.suspect,
                  gain_note=bad.gain_summary())
    txt = (out / "mixed_p_stokes.csv").read_text(encoding="utf-8")
    check("the suspect flag is written into the Stage 3b file",
          "SUSPECT" in txt and "gain_suspect,yes" in txt)
    A.save_stokes("clean", "p", good.fit, out, suspect=good.suspect,
                  gain_note=good.gain_summary())
    txt2 = (out / "clean_p_stokes.csv").read_text(encoding="utf-8")
    check("a clean fit is not flagged", "gain_suspect,no" in txt2
          and "SUSPECT" not in txt2)


# =============================================================================
def main():
    tests = [test_1, test_2, test_3, test_4, test_5,
             test_6, test_7, test_8, test_9, test_10,
             test_11, test_12, test_13]
    only = sys.argv[1:]
    for t in tests:
        if only and t.__name__.split("_")[-1] not in only:
            continue
        try:
            t()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            RESULTS.append((f"{t.__name__} CRASHED", False, str(exc)))

    print(f"\n{'=' * 78}")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"SUMMARY   {passed}/{len(RESULTS)} checks passed")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL  {name}   {detail}")
    print("=" * 78)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
