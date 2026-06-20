#!/usr/bin/env python3
"""
cik_lookup.py — build a CIK -> company-name map from SEC's public file.

The EDGAR corpus rows carry only a numeric `cik`, not the company name, so the
extractor writes company=null. SEC publishes company_tickers.json (CIK, ticker,
title) for every filer with a ticker; this script downloads it and writes a
compact {normalized_cik: {"name": ..., "ticker": ...}} map to ./data.

    python3 src/cik_lookup.py                       # -> data/cik_names.json

Then enrich at extraction time:
    python3 src/extract_promises.py --cik-map data/cik_names.json \
        --source parquet:data/edgar-corpus/year_2020/train --out output/promises_2020.jsonl

SEC requires a descriptive User-Agent; override the default with --user-agent.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

URL = "https://www.sec.gov/files/company_tickers.json"


def norm_cik(cik) -> str:
    """Normalize any CIK form ('0000320193', '320193', 320193) to '320193'."""
    s = str(cik).strip()
    return str(int(s)) if s.isdigit() else s


def load_cik_map(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/cik_names.json")
    ap.add_argument("--user-agent",
                    default="promiseKeepers research erik.wilhelm@gmail.com",
                    help="SEC requires a descriptive UA with contact info")
    args = ap.parse_args()

    req = urllib.request.Request(URL, headers={"User-Agent": args.user_agent})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.load(resp)
    except Exception as e:
        sys.exit(f"Could not download {URL}: {e}\n"
                 "SEC blocks requests without a descriptive --user-agent.")

    out = {}
    for row in raw.values():
        out[norm_cik(row["cik_str"])] = {
            "name": row.get("title"),
            "ticker": row.get("ticker"),
        }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    print(f"wrote {len(out)} CIK->name entries -> {args.out}")


if __name__ == "__main__":
    main()
