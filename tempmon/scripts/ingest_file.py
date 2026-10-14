"""Replay a CSV/JSONL of readings: python scripts/ingest_file.py data/sample_readings.csv"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tempmon.runtime import build, setup_logging  # noqa: E402
from tempmon.timeutil import parse_timestamp  # noqa: E402


def rows(path):
    if path.endswith(".jsonl"):
        for line in open(path, encoding="utf-8"):
            if line.strip():
                yield json.loads(line)
    else:
        yield from csv.DictReader(open(path, encoding="utf-8"))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    setup_logging()
    proc = build()
    tally = {"ok": 0, "rejected": 0, "duplicate": 0}
    for p in rows(sys.argv[1]):
        # replay: evaluate "now" as the reading's own time so history isn't rejected as old/future
        try:
            now = parse_timestamp(p.get("timestamp"))
        except ValueError:
            now = None
        tally[proc.ingest(p, now=now).status] += 1
    print(tally)


if __name__ == "__main__":
    main()
