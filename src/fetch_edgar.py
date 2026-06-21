#!/usr/bin/env python3
"""
fetch_edgar.py — pull annual-report text (20-F / 10-K) straight from SEC EDGAR
for specific companies, e.g. Swiss / EU multinationals that file with the SEC.

Many large non-US issuers (Novartis, UBS, STMicroelectronics, Logitech, SAP,
ASML, …) file their annual report as Form 20-F and also report XBRL facts to
data.sec.gov. Once their filing text is here, the existing
extract -> check -> visualize pipeline works unchanged. Output rows are
compatible with `extract_promises.py --source jsonl:` (one `section_full` field).

    python3 src/fetch_edgar.py --tickers NVS UBS STM TEL LOGI GRMN CB SAP ASML SNY \
        --form 20-F --count 2 --out data/foreign_filings.jsonl

    # then:
    python3 src/extract_promises.py --source jsonl:data/foreign_filings.jsonl \
        --sections section_full --out output/promises_foreign.jsonl --min-score 6

Tickers are resolved via data/cik_names.json (run src/cik_lookup.py first), or
pass CIK numbers directly with --cik. SEC needs a descriptive User-Agent + <=10 req/s.
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.request

UA = "promiseKeepers research erik.wilhelm@gmail.com"


def get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Encoding": "gzip, deflate"})
    resp = urllib.request.urlopen(req, timeout=40)
    data = resp.read()
    if resp.info().get("Content-Encoding") == "gzip":
        import gzip
        data = gzip.decompress(data)
    return data


def strip_html(raw: bytes) -> str:
    t = raw.decode("utf-8", "replace")
    t = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = htmllib.unescape(t)
    t = re.sub(r"[ \t ]+", " ", t)
    t = re.sub(r"\s*\n\s*", "\n", t)
    return t.strip()


def resolve_tickers(tickers, cikmap_path):
    if not os.path.exists(cikmap_path):
        sys.exit(f"{cikmap_path} not found — run `python3 src/cik_lookup.py` first, "
                 "or pass CIKs with --cik.")
    with open(cikmap_path, encoding="utf-8") as fh:
        cikmap = json.load(fh)
    by_ticker = {}
    for cik, info in cikmap.items():
        tk = (info.get("ticker") or "").upper()
        if tk:
            by_ticker.setdefault(tk, cik)
    out = {}
    for t in tickers:
        cik = by_ticker.get(t.upper())
        if cik:
            out[cik] = cikmap[cik].get("name")
        else:
            print(f"  !! ticker {t} not in cik map — skipping")
    return out


def filings_for(cik: str, form: str, count: int):
    pad = f"CIK{int(cik):010d}"
    sub = json.loads(get(f"https://data.sec.gov/submissions/{pad}.json"))
    name = sub.get("name")
    rec = sub.get("filings", {}).get("recent", {})
    forms = rec.get("form", [])
    out = []
    for i, f in enumerate(forms):
        if f != form:
            continue
        out.append({
            "accession": rec["accessionNumber"][i].replace("-", ""),
            "doc": rec["primaryDocument"][i],
            "report_date": rec.get("reportDate", [""] * len(forms))[i],
            "filing_date": rec.get("filingDate", [""] * len(forms))[i],
        })
        if len(out) >= count:
            break
    return name, out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", nargs="*", default=[],
                    help="ticker symbols (resolved via cik map)")
    ap.add_argument("--cik", nargs="*", default=[], help="CIK numbers directly")
    ap.add_argument("--form", default="20-F", help="filing form (default: 20-F)")
    ap.add_argument("--count", type=int, default=2, help="recent filings per company")
    ap.add_argument("--cik-map", default="data/cik_names.json")
    ap.add_argument("--out", default="data/foreign_filings.jsonl")
    ap.add_argument("--throttle", type=float, default=0.2)
    args = ap.parse_args()

    targets = resolve_tickers(args.tickers, args.cik_map) if args.tickers else {}
    for c in args.cik:
        targets.setdefault(str(int(c)), None)
    if not targets:
        sys.exit("Nothing to fetch — pass --tickers and/or --cik.")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    rows = 0
    with open(args.out, "w", encoding="utf-8") as fh:
        for cik, name in targets.items():
            try:
                sub_name, filings = filings_for(cik, args.form, args.count)
            except Exception as e:
                print(f"  !! {cik}: submissions failed ({e})")
                continue
            name = name or sub_name
            if not filings:
                print(f"  -- {name or cik}: no {args.form} filings found")
                continue
            for f in filings:
                url = (f"https://www.sec.gov/Archives/edgar/data/"
                       f"{int(cik)}/{f['accession']}/{f['doc']}")
                try:
                    text = strip_html(get(url))
                except Exception as e:
                    print(f"  !! {name} {f['report_date']}: download failed ({e})")
                    continue
                year = (f["report_date"] or f["filing_date"] or "")[:4]
                fh.write(json.dumps({
                    "cik": cik, "company": name, "year": year,
                    "form": args.form, "source_url": url,
                    "section_full": text,
                }, ensure_ascii=False) + "\n")
                rows += 1
                print(f"  ok {name}  FY{year}  ({len(text):,} chars)")
                time.sleep(args.throttle)

    print(f"\nwrote {rows} filing(s) -> {args.out}")
    print("next:  python3 src/extract_promises.py --source jsonl:%s "
          "--sections section_full --out output/promises_foreign.jsonl --min-score 6"
          % args.out)


if __name__ == "__main__":
    main()
