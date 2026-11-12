"""Annotate the client requirements matrix with the Campfire fit map.

Usage:
    python3 scripts/annotate_matrix.py <matrix.xlsx> <out.xlsx>

Reads docs/campfire_fit_map.csv, appends Campfire Fit / FO Module / Demo Phase /
Approach columns to the 'Requirements Matrix' sheet (existing formulas are left
untouched), adds a 'Campfire Fit Summary' sheet, and prints the summary.
"""
import csv
from copy import copy
import sys
from collections import Counter, OrderedDict
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

ROOT = Path(__file__).resolve().parent.parent
FIT_MAP = ROOT / "docs" / "campfire_fit_map.csv"
FITS = ["Native", "Configure", "Hybrid", "Build", "Integrate", "Process"]
PHASES = ["P1", "P2", "P3", "—"]
FILLS = {
    "Native": "C6EFCE", "Configure": "DDEBF7", "Hybrid": "FFF2CC",
    "Build": "F8CBAD", "Integrate": "E2EFDA", "Process": "EDEDED",
}


def load_map():
    with FIT_MAP.open(newline="") as f:
        return OrderedDict((r["req_id"], r) for r in csv.DictReader(f))


def main(src, dst):
    fit = load_map()
    wb = openpyxl.load_workbook(src)
    ws = wb["Requirements Matrix"]
    header_row = next(r for r in range(1, 10) if ws.cell(r, 1).value == "Req ID")
    first_new = ws.max_column + 1
    new_cols = ["Campfire Fit", "FO Module", "Demo Phase", "Campfire / FO Approach"]
    hdr_style = ws.cell(header_row, 1)
    for i, name in enumerate(new_cols):
        c = ws.cell(header_row, first_new + i, name)
        c.font = Font(bold=True, color=hdr_style.font.color)
        c.fill = copy(hdr_style.fill)
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for i, w in enumerate([13, 24, 11, 70]):
        ws.column_dimensions[openpyxl.utils.get_column_letter(first_new + i)].width = w

    rows, missing = [], []
    for r in range(header_row + 1, ws.max_row + 1):
        rid = ws.cell(r, 1).value
        if not rid:
            continue
        m = fit.get(rid)
        if not m:
            missing.append(rid)
            continue
        vals = [m["campfire_fit"], m["fo_module"], m["demo_phase"], m["approach"]]
        for i, v in enumerate(vals):
            c = ws.cell(r, first_new + i, v)
            c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(r, first_new).fill = PatternFill("solid", fgColor=FILLS[m["campfire_fit"]])
        rows.append({
            **m,
            "priority": ws.cell(r, 6).value,
            "coverage": ws.cell(r, 8).value,
            "target": ws.cell(r, 10).value,
        })
    extra = set(fit) - {r["req_id"] for r in rows}
    if missing or extra:
        sys.exit(f"Fit map mismatch. Missing: {missing}  Unknown: {sorted(extra)}")

    # Summary sheet
    if "Campfire Fit Summary" in wb.sheetnames:
        del wb["Campfire Fit Summary"]
    s = wb.create_sheet("Campfire Fit Summary")
    bold = Font(bold=True)
    s.append(["Campfire fit by FO module"]); s["A1"].font = Font(bold=True, size=13)
    s.append(["Module"] + FITS + ["Total"])
    modules = sorted({r["fo_module"] for r in rows}, key=lambda x: (x != "Campfire Core", x))
    for mod in modules:
        c = Counter(r["campfire_fit"] for r in rows if r["fo_module"] == mod)
        s.append([mod] + [c.get(f, 0) for f in FITS] + [sum(c.values())])
    tot = Counter(r["campfire_fit"] for r in rows)
    s.append(["TOTAL"] + [tot.get(f, 0) for f in FITS] + [len(rows)])
    s.append([])
    s.append(["Demo phase by fit"])
    s.append(["Phase"] + FITS + ["Total"])
    for p in PHASES:
        c = Counter(r["campfire_fit"] for r in rows if r["demo_phase"] == p)
        s.append([p] + [c.get(f, 0) for f in FITS] + [sum(c.values())])
    s.append([])
    s.append(["Must-have requirements by fit"])
    s.append(["Fit", "Count"])
    must = Counter(r["campfire_fit"] for r in rows if r["priority"] == "Must")
    for f in FITS:
        s.append([f, must.get(f, 0)])
    for row in s.iter_rows():
        if row[0].value in ("Module", "Phase", "Fit", "TOTAL"):
            for cell in row:
                cell.font = bold
    s.column_dimensions["A"].width = 28
    wb.save(dst)

    # Console summary
    print(f"{len(rows)} requirements mapped\n")
    print(f"{'Module':<26}" + "".join(f"{f:>10}" for f in FITS) + f"{'Total':>8}")
    for mod in modules:
        c = Counter(r["campfire_fit"] for r in rows if r["fo_module"] == mod)
        print(f"{mod:<26}" + "".join(f"{c.get(f,0):>10}" for f in FITS) + f"{sum(c.values()):>8}")
    print(f"{'TOTAL':<26}" + "".join(f"{tot.get(f,0):>10}" for f in FITS) + f"{len(rows):>8}\n")
    print("By phase:", {p: sum(1 for r in rows if r["demo_phase"] == p) for p in PHASES})
    print("Must-haves by fit:", dict(must))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
