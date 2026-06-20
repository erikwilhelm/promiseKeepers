#!/usr/bin/env python3
"""
download_corpus.py — fetch the EDGAR corpus into ./data as Parquet.

The `eloukas/edgar-corpus` dataset ships a loading *script* (edgar-corpus.py),
and recent `datasets` releases dropped `trust_remote_code`, so
`--source hf:eloukas/edgar-corpus` no longer works. The Hub still auto-converts
every dataset to Parquet under the `refs/convert/parquet` revision, so we just
download those files directly and feed them to the extractor with
`--source parquet:`.

    pip install huggingface_hub pyarrow

    # one year, train split -> data/edgar-corpus/year_2020/train/*.parquet
    python3 src/download_corpus.py --year 2020

    # five years for a longitudinal study (range is inclusive)
    python3 src/download_corpus.py --year 2016-2020

    # grab just 1 file per year for a quick test
    python3 src/download_corpus.py --year 2016-2020 --max-files 1

    # then extract:
    python3 src/extract_promises.py \
        --source parquet:data/edgar-corpus/year_2020/train \
        --out output/promises_2020.jsonl --min-score 5
"""
from __future__ import annotations

import argparse
import sys

REPO = "eloukas/edgar-corpus"
PARQUET_REV = "refs/convert/parquet"


def parse_years(spec: str) -> list:
    """'2020' -> [2020]; '2016-2020' -> [2016..2020]."""
    spec = spec.strip()
    if "-" in spec:
        a, b = spec.split("-", 1)
        lo, hi = int(a), int(b)
        return list(range(min(lo, hi), max(lo, hi) + 1))
    return [int(spec)]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", required=True,
                    help="corpus year '2020' or inclusive range '2016-2020'")
    ap.add_argument("--split", default="train",
                    choices=["train", "validation", "test"])
    ap.add_argument("--repo", default=REPO, help=f"HF dataset id (default: {REPO})")
    ap.add_argument("--dest", default="data/edgar-corpus",
                    help="local download root (default: data/edgar-corpus)")
    ap.add_argument("--max-files", type=int, default=0,
                    help="download at most N parquet shards per year (0 = all)")
    args = ap.parse_args()

    try:
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError:
        sys.exit("Install the downloader first:  pip install huggingface_hub")

    years = parse_years(args.year)
    api = HfApi()
    try:
        all_files = api.list_repo_files(
            args.repo, repo_type="dataset", revision=PARQUET_REV)
    except Exception as e:
        sys.exit(f"Could not list {args.repo}@{PARQUET_REV}: {e}")

    grabbed = 0
    for year in years:
        prefix = f"year_{year}/{args.split}/"
        shards = sorted(f for f in all_files
                        if f.startswith(prefix) and f.endswith(".parquet"))
        if not shards:
            print(f"!! no shards for year_{year}/{args.split} — skipping")
            continue
        if args.max_files:
            shards = shards[:args.max_files]
        print(f"year_{year}/{args.split}: {len(shards)} shard(s) -> {args.dest}/")
        for i, fn in enumerate(shards, 1):
            hf_hub_download(args.repo, fn, repo_type="dataset",
                            revision=PARQUET_REV, local_dir=args.dest)
            print(f"  [{i}/{len(shards)}] {fn}")
            grabbed += 1

    if not grabbed:
        sys.exit("Nothing downloaded. Check --year/--split (configs look like 'year_2020').")

    scope = args.dest if len(years) > 1 else f"{args.dest}/year_{years[0]}/{args.split}"
    tag = f"{years[0]}_{years[-1]}" if len(years) > 1 else str(years[0])
    print("\nDone. Now extract (one combined file, each promise tagged by year):")
    print(f"  python3 src/extract_promises.py \\")
    print(f"      --cik-map data/cik_names.json \\")
    print(f"      --source parquet:{scope} \\")
    print(f"      --out output/promises_{tag}.jsonl --min-score 5")


if __name__ == "__main__":
    main()
