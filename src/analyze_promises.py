#!/usr/bin/env python3
"""
analyze_promises.py — parse extractor output, trace promises across companies,
and study how they change over time (longitudinal).

Reads one or more .jsonl/.db files (globs and directories are expanded) produced
by extract_promises.py. Each promise carries a filing `year`, so pointing this at
a multi-year extraction gives a longitudinal view. Stdlib only.

Every subcommand both prints a report AND writes a file under output/analysis/
(override with --csv PATH, change dir with --out-dir, disable with --no-save).

    # one year
    python3 src/analyze_promises.py output/promises_2020.jsonl summary

    # many years at once (longitudinal): glob or list several files / a directory
    python3 src/analyze_promises.py "output/promises_*.jsonl" trend
    python3 src/analyze_promises.py output/promises_2016_2020.jsonl trend --metric ghg_emissions

    # one theme across companies, sorted by deadline (writes metric_ghg_emissions.csv)
    python3 src/analyze_promises.py output/promises_2016_2020.jsonl metric ghg_emissions

    # one company across every year it filed
    python3 src/analyze_promises.py output/promises_2016_2020.jsonl company "delta air lines"
"""
from __future__ import annotations

import argparse
import csv
import glob as globmod
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict

FIELDS = ["company", "ticker", "cik", "year", "section", "metric_category",
          "deadline_year", "deadline_phrase", "score", "modality",
          "targets", "text", "promise_id"]


# --- loading ---------------------------------------------------------------

def expand_paths(spec: str) -> list:
    out = []
    for p in spec.split(","):
        p = p.strip()
        if not p:
            continue
        if os.path.isdir(p):
            out += sorted(globmod.glob(os.path.join(p, "*.jsonl")))
            out += sorted(globmod.glob(os.path.join(p, "*.db")))
        elif any(c in p for c in "*?["):
            out += sorted(globmod.glob(p))
        else:
            out.append(p)
    return out


def load_one(path: str) -> list:
    if path.endswith((".db", ".sqlite", ".sqlite3")):
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("SELECT * FROM promises")]
        con.close()
        for r in rows:
            for k in ("cues", "targets"):
                if isinstance(r.get(k), str):
                    try:
                        r[k] = json.loads(r[k])
                    except Exception:
                        pass
        return rows
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def load(spec) -> list:
    paths = expand_paths(spec)
    if not paths:
        sys.exit(f"no files matched: {spec}")
    rows = []
    for p in paths:
        rows += load_one(p)
    print(f"loaded {len(rows)} promises from {len(paths)} file(s): "
          f"{', '.join(os.path.basename(p) for p in paths)}\n")
    return rows


# --- helpers ---------------------------------------------------------------

def fmt_targets(t) -> str:
    return ", ".join(t) if isinstance(t, list) else (t or "")


def who(r) -> str:
    name = r.get("company") or f"CIK {r.get('cik')}"
    tk = r.get("ticker")
    return f"{name} ({tk})" if tk else name


def matches_company(r, q: str) -> bool:
    q = q.lower()
    return any(q in str(r.get(f) or "").lower() for f in ("company", "ticker", "cik"))


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_") or "all"


def print_rows(rows, show_company=True):
    for r in rows:
        head = f"  [{r.get('score')}] " + (f"{who(r)}  " if show_company else "")
        dl = r.get("deadline_year") or "—"
        metric = r.get("metric_category") or "—"
        tg = fmt_targets(r.get("targets"))
        tg = f"  targets={tg}" if tg else ""
        print(f"{head}{r.get('year')}/{r.get('section')}  metric={metric}  "
              f"deadline={dl}{tg}")
        print(f"      {r.get('text')}")


def sort_rows(rows, key: str):
    if key == "deadline":
        return sorted(rows, key=lambda r: (r.get("deadline_year") or "9999",
                                           -int(r.get("score") or 0)))
    if key == "company":
        return sorted(rows, key=lambda r: (str(r.get("company") or "~").lower(),
                                           -int(r.get("score") or 0)))
    return sorted(rows, key=lambda r: -int(r.get("score") or 0))


def out_path(args, default_name: str) -> str | None:
    if args.no_save:
        return None
    if args.csv:
        return args.csv
    os.makedirs(args.out_dir, exist_ok=True)
    return os.path.join(args.out_dir, default_name)


def save_rows(rows, path):
    if not path or not rows:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            row = dict(r)
            row["targets"] = fmt_targets(row.get("targets"))
            w.writerow(row)
    print(f"\n-> wrote {len(rows)} rows to {path}")


def save_matrix(path, corner, col_keys, mat):
    """mat: dict[row_label] -> dict[col_key] -> count. Adds row/col totals."""
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cols = list(col_keys)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([corner] + cols + ["TOTAL"])
        coltot = Counter()
        for rk in mat:
            counts = mat[rk]
            rowvals = [counts.get(c, 0) for c in cols]
            for c in cols:
                coltot[c] += counts.get(c, 0)
            w.writerow([rk] + rowvals + [sum(rowvals)])
        w.writerow(["TOTAL"] + [coltot[c] for c in cols] + [sum(coltot.values())])
    print(f"\n-> wrote matrix to {path}")


def print_matrix(corner, col_keys, mat, top=30):
    cols = list(col_keys)
    w0 = max([len(str(corner))] + [len(str(r)) for r in mat]) if mat else len(corner)
    w0 = min(w0, 42)
    header = f"{corner:<{w0}} " + " ".join(f"{str(c):>6}" for c in cols) + f" {'TOTAL':>6}"
    print(header)
    print("-" * len(header))
    ordered = sorted(mat.items(), key=lambda kv: -sum(kv[1].values()))[:top]
    for rk, counts in ordered:
        rowvals = [counts.get(c, 0) for c in cols]
        label = (str(rk)[:w0 - 1] + "…") if len(str(rk)) > w0 else str(rk)
        print(f"{label:<{w0}} " + " ".join(f"{v:>6}" for v in rowvals)
              + f" {sum(rowvals):>6}")


# --- subcommands -----------------------------------------------------------

def cmd_summary(rows, args):
    companies = {(r.get("company") or f"CIK {r.get('cik')}") for r in rows}
    print(f"promises: {len(rows)}    companies/filers: {len(companies)}    "
          f"named: {sum(1 for r in rows if r.get('company'))}")
    print("\nby filing year:")
    for y, n in sorted(Counter(str(r.get('year') or '—') for r in rows).items()):
        print(f"  {n:6d}  {y}")
    print("\nby metric:")
    for m, n in Counter(r.get("metric_category") or "—" for r in rows).most_common():
        print(f"  {n:6d}  {m}")
    print(f"\ntop {args.top} companies by promise count:")
    for name, n in Counter(who(r) for r in rows if r.get("company")).most_common(args.top):
        print(f"  {n:6d}  {name}")
    save_rows(rows, out_path(args, "summary_rows.csv"))


def cmd_company(rows, args):
    sel = sort_rows([r for r in rows if matches_company(r, args.query)], "deadline")
    if not sel:
        print(f"no promises match company '{args.query}'")
        return
    names = sorted({who(r) for r in sel})
    print(f"{len(sel)} promises across {len(names)} matched filer(s): "
          f"{', '.join(names[:10])}{' …' if len(names) > 10 else ''}\n")
    for yr in sorted({str(r.get('year')) for r in sel}):
        print(f"== {yr} ==")
        print_rows([r for r in sel if str(r.get('year')) == yr])
        print()
    save_rows(sel, out_path(args, f"company_{slug(args.query)}.csv"))


def cmd_metric(rows, args):
    sel = sort_rows([r for r in rows if r.get("metric_category") == args.name],
                    "deadline")
    if not sel:
        avail = sorted({r.get("metric_category") for r in rows if r.get("metric_category")})
        print(f"no promises with metric '{args.name}'. available: {', '.join(avail)}")
        return
    print(f"{len(sel)} '{args.name}' promises across "
          f"{len({who(r) for r in sel})} companies (by deadline):\n")
    print_rows(sel)
    save_rows(sel, out_path(args, f"metric_{slug(args.name)}.csv"))


def cmd_list(rows, args):
    sel = rows
    if args.company:
        sel = [r for r in sel if matches_company(r, args.company)]
    if args.metric:
        sel = [r for r in sel if r.get("metric_category") == args.metric]
    if args.deadline:
        sel = [r for r in sel if (r.get("deadline_year") or "") == args.deadline]
    if args.year:
        sel = [r for r in sel if str(r.get("year") or "") == args.year]
    if args.min_score:
        sel = [r for r in sel if int(r.get("score") or 0) >= args.min_score]
    sel = sort_rows(sel, args.sort)
    if args.limit:
        sel = sel[:args.limit]
    print(f"{len(sel)} promises:\n")
    print_rows(sel)
    save_rows(sel, out_path(args, "list.csv"))


def cmd_trend(rows, args):
    """Longitudinal: counts pivoted by filing year."""
    if args.min_score:
        rows = [r for r in rows if int(r.get("score") or 0) >= args.min_score]
    years = sorted({str(r.get("year")) for r in rows if r.get("year")})

    if args.company:
        rows = [r for r in rows if matches_company(r, args.company)]
        title = f"'{args.company}' promises by metric x year"
        mat = defaultdict(Counter)
        for r in rows:
            mat[r.get("metric_category") or "—"][str(r.get("year"))] += 1
        name = f"trend_company_{slug(args.company)}.csv"
        rowcorner = "metric"
    elif args.metric:
        rows = [r for r in rows if r.get("metric_category") == args.metric]
        title = f"'{args.metric}' promises by company x year"
        mat = defaultdict(Counter)
        for r in rows:
            mat[who(r)][str(r.get("year"))] += 1
        name = f"trend_{slug(args.metric)}_by_company.csv"
        rowcorner = "company"
    else:
        title = "promises by metric x filing year"
        mat = defaultdict(Counter)
        for r in rows:
            mat[r.get("metric_category") or "—"][str(r.get("year"))] += 1
        name = "trend_metric_by_year.csv"
        rowcorner = "metric"

    if not rows:
        print("no rows match the trend filters")
        return
    print(f"{title}  ({len(rows)} promises, years {years[0]}–{years[-1]})\n"
          if years else f"{title}  ({len(rows)} promises)\n")
    print_matrix(rowcorner, years, mat, top=args.top)
    save_matrix(out_path(args, name), rowcorner, years, mat)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path",
                    help="promises .jsonl/.db file, glob, directory, or "
                         "comma-separated list (for a multi-year longitudinal view)")
    ap.add_argument("--csv", help="exact output path (overrides the default name)")
    ap.add_argument("--out-dir", default="output/analysis",
                    help="directory for auto-saved output (default: output/analysis)")
    ap.add_argument("--no-save", action="store_true", help="print only, write nothing")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("summary", help="totals + breakdowns")
    s.add_argument("--top", type=int, default=15)
    s.set_defaults(fn=cmd_summary)

    c = sub.add_parser("company", help="all promises for one filer (over all years)")
    c.add_argument("query", help="company name substring, ticker, or CIK")
    c.set_defaults(fn=cmd_company)

    m = sub.add_parser("metric", help="one theme across all companies")
    m.add_argument("name", help="e.g. ghg_emissions, water, capex, diversity")
    m.set_defaults(fn=cmd_metric)

    t = sub.add_parser("trend", help="longitudinal: counts pivoted by filing year")
    t.add_argument("--metric", help="focus one metric: company x year matrix")
    t.add_argument("--company", help="focus one filer: metric x year matrix")
    t.add_argument("--min-score", type=int)
    t.add_argument("--top", type=int, default=30, help="max rows printed (CSV is full)")
    t.set_defaults(fn=cmd_trend)

    l = sub.add_parser("list", help="generic filtered list")
    l.add_argument("--company")
    l.add_argument("--metric")
    l.add_argument("--deadline")
    l.add_argument("--year")
    l.add_argument("--min-score", type=int)
    l.add_argument("--sort", choices=["score", "deadline", "company"], default="score")
    l.add_argument("--limit", type=int, default=0)
    l.set_defaults(fn=cmd_list)

    try:  # filing text contains chars (●, smart quotes) the Windows console lacks
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    args = ap.parse_args()
    rows = load(args.path)
    if not rows:
        sys.exit("no rows loaded")
    args.fn(rows, args)


if __name__ == "__main__":
    main()
