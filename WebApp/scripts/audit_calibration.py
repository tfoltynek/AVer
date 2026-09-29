"""Reproduce unigram calibration from the privately supplied study CSV.

Usage: python scripts/audit_calibration.py /path/to/data_full.csv
Only aggregate counts are printed; source answers never leave the CSV.
The subgroup filters are those in Hitzinger's pA_pN_computing.ipynb.
"""

import argparse
import csv
import hashlib
import json
from io import StringIO
from pathlib import Path


def summarize(rows, *, pos, exact):
    counts = {"author": {"correct": 0, "total": 0}, "non_author": {"correct": 0, "total": 0}}
    for row in rows:
        if row["selector"] not in {"ml", "unigram"}:
            continue
        if pos not in row["word_types"].split("+"):
            continue
        if not 1 < float(row["ml_pos"]) <= 40:
            continue
        group = counts["author" if row["test_type"] == "authorML" else "non_author"]
        if exact:
            answer = row["answered"]
            correct = bool(answer.strip()) and answer.strip().lower() == row["content"].strip().lower()
        else:
            correct = float(row["similarity"]) >= 0.7172
        group["total"] += 1
        group["correct"] += int(correct)
    for group in counts.values():
        if not group["total"]:
            raise ValueError(f"No observations for {pos}: cannot estimate probability")
        group["probability"] = group["correct"] / group["total"]
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    args = parser.parse_args()
    raw = args.csv_path.read_bytes()
    # Use StringIO to preserve quoted multiline answers and source sentences.
    rows = list(csv.DictReader(StringIO(raw.decode("utf-8-sig"))))
    result = {"sha256": hashlib.sha256(raw).hexdigest(), "rows": len(rows), "groups": {}}
    for pos in ("NOUN", "ADJ"):
        result["groups"][pos] = {
            "exact_match": summarize(rows, pos=pos, exact=True),
            "notebook_similarity": summarize(rows, pos=pos, exact=False),
        }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
