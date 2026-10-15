# RSXD Scan Logger — phase 1

Logs which beamline scan number corresponds to which measurement condition,
live during an RSXD beamtime at Diamond I10 / RASOR.

One workbook = one (peak, temperature) pair. Inside it, a grid of analyser
angle × polarisation channel, one scan number per cell.

---

## Run it

Double-click **`run_rsxd_logger.bat`**, or from a terminal in this folder:

```bash
python rsxd_logger.py
```

Needs Python 3.11+ and `openpyxl`. Nothing else — no server, no build step.
If `openpyxl` is missing:

```bash
pip install openpyxl
```

---

## Where files go

Everything lands in the **output directory** (set in the app or in the config;
default is an `output` folder next to `rsxd_config.toml`):

| File | What it is |
|---|---|
| `rsxd_scan_log.csv` | **The important one.** Cumulative long-format CSV, every scan of the whole beamtime, one row each. This is what the analysis scripts and phase 2 read. |
| `CoTau_300K_64717-64768.xlsx` | One workbook per (peak, temperature), in the template layout. For humans and for the record. |
| `rsxd_session.json` | Autosave. Written on **every keystroke**. Delete it only if you want to start the beamtime over. |

The `.xlsx` filename carries the min and max scan number in that workbook, so
it changes as you add scans. On save the app writes the new name and deletes
the old one — there is never a second file for the same peak+temperature
carrying a stale range.

Workbooks are written when you press **Ctrl+S**, when you switch peak or
temperature, and when you close the app. The CSV and the session file are
written continuously as you type.

---

## Keyboard

Designed so you never need the mouse during a run.

| Key | Does |
|---|---|
| `Tab` / `Enter` | Next polarisation channel; after LV, jumps to the next analyser angle |
| `Shift+Tab` | Back one cell |
| `↑` / `↓` | Move between the four channels |
| `Ctrl+←` / `Ctrl+→` | Previous / next analyser angle |
| `Ctrl+N` | Jump to the note field for the selected cell; `Enter` or `Esc` returns |
| `Alt+←` / `Alt+→` | Previous / next peak |
| `Alt+↑` / `Alt+↓` | Previous / next temperature |
| `Ctrl+F` | Sequential fill |
| `F5` | Verify all `.dat` files (press after each WinSCP sync) |
| `Ctrl+S` | Save the current workbook |

Type a number, Tab, type the next. Non-digits are ignored rather than beeped at.

### Cell colours

| Border | Meaning |
|---|---|
| dim blue | empty |
| green | scan logged, its `.dat` is on disk |
| amber | scan logged, `.dat` not on disk yet — **normal**, not an error |
| cyan | scan logged, `.dat` check is switched off |
| red | **duplicate scan number** somewhere in the session |
| violet number | this cell has a note |

The `FILLED n/4` row above the grid, and the `EMPTY THIS GRID` readout in the
header, show at a glance what is still missing.

Duplicates are checked across the *whole session* — every peak and every
temperature — because a repeated scan number is almost always a typo, and a
silent one.

---

## Editing the config

`rsxd_config.toml`, plain text, open it in Notepad. **Restart the app** after
editing. Everything that changes between experiments lives there:

* `analyser_angles_deg` — the grid columns
* `temperatures_K` — the temperature selector (ints or decimals, e.g. `4.2`)
* `[peaks]` — `"label in the app" = "string used in the filename"`
* `[polarisation_groups]` — group label = its row labels, in workbook row order
* `output_dir`, `dat_dir` — also settable in the app, which writes them back here
* `dat_filename_patterns` — how raw files are named; `{scan}` is the scan number

Windows paths must use **single quotes**: `dat_dir = 'C:\data\mm43464'`.

If you shorten the temperature or angle list, scans already logged at values
you removed are **not** deleted. They stay in the session and in the CSV, and
the app says so in the log at startup.

---

## The CSV schema is fixed

```
peak, temperature_K, analyser_angle_deg, polarisation, scan_number, note,
dat_found, timestamp
```

`dat_found` is `yes` / `no` / `unknown` (`unknown` = the check was off).
One row per logged scan. This is the interface to phase 2 — treat it as frozen.

The CSV is regenerated from the session on each save rather than blindly
appended, so correcting a typo actually removes the wrong value instead of
leaving it behind. It still only ever grows as the beamtime goes on.

---

## Structure

| File | Role |
|---|---|
| `rsxd_core.py` | All experiment logic, file I/O, xlsx and CSV writing. **Imports no GUI code at all.** Phase 2 imports this directly. |
| `rsxd_logger.py` | The tkinter UI. One class, `RSXDLoggerFrame`, which is an ordinary frame. |
| `tests/test_rsxd.py` | Acceptance tests. Drives the real app with real key events. `python tests/test_rsxd.py` |

To make this a tab in a larger app, no rewrite needed:

```python
from rsxd_logger import RSXDLoggerFrame

page = ttk.Frame(notebook)
notebook.add(page, text="Scan Log")
app = RSXDLoggerFrame(page)
app.pack(fill="both", expand=True)
app.install_shortcuts(root)
```

---

## If something goes wrong

* **The app will not start** — almost always `rsxd_config.toml`. The error
  message names the problem. Windows paths need single quotes.
* **Data directory unreachable** (VPN dropped, drive unmounted) — the `.dat`
  check switches itself off and says so. Logging carries on regardless.
* **The app crashed** — reopen it. Everything up to the last keystroke is in
  `rsxd_session.json`. Nothing is lost.
* **The session file got corrupted** — the app keeps it as
  `rsxd_session.corrupt.json` and starts empty rather than refusing to open.
