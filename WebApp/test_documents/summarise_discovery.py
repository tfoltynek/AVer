#!/usr/bin/env python3
"""Tally what `record_muni.py` recorded and check it against the code.

Reads every recording in a directory — what `discover` writes, or the
`*_response.json` fixtures — and answers the two questions the mapping
depends on:

1. Which `category` and `origin_category` values did the service send, and
   does `core.utils.muni_category` place every one of them?
2. Does `predictions is null` mark exactly the randomly removed words? That
   equivalence is what migration 0029 relies on for the rows ingested before
   the service reported a category.

Usage: uv run python test_documents/summarise_discovery.py <dir> [<dir>…]

Point it at a discovery output directory or at `test_documents/data`.
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.utils.muni_category import UNMAPPED, method_from_category  # noqa: E402


def words_in(directory: pathlib.Path):
    """Yield the words of every recording in a directory.

    Handles both shapes: what `discover` writes ({request, response}) and a
    plain `*_response.json` fixture.
    """
    paths = sorted(
        {*directory.glob("round*_*.json"), *directory.glob("*_response.json")}
    )
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        response = payload.get("response", payload)
        yield path.name, response.get("words") or []


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    categories: collections.Counter = collections.Counter()
    origins: collections.Counter = collections.Counter()
    crosstab: collections.Counter = collections.Counter()
    empty_prediction_lists = 0
    documents = words = 0

    for directory in argv:
        for _name, entries in words_in(pathlib.Path(directory)):
            documents += 1
            for word in entries:
                words += 1
                predictions = word.get("predictions")
                if isinstance(predictions, list) and not predictions:
                    empty_prediction_lists += 1
                categories[word.get("category")] += 1
                if word.get("origin_category"):
                    origins[word["origin_category"]] += 1
                crosstab[
                    (
                        len((word.get("content") or "").split()),
                        predictions is None,
                        word.get("category"),
                    )
                ] += 1

    print(f"{documents} documents, {words} words\n")

    print("category → the bucket we map it to")
    unmapped = []
    for category, count in categories.most_common():
        method = method_from_category(category)
        if method is UNMAPPED:
            label = "UNMAPPED (falls back to POS tagging)"
            unmapped.append(category)
        else:
            label = str(method)
        print(f"  {str(category):24} {count:>4}  →  {label}")

    print("\norigin_category (random picks only)")
    for origin, count in origins.most_common():
        print(f"  {origin:24} {count:>4}")

    print("\nshape | predictions null | category | n")
    for (shape, is_null, category), count in sorted(crosstab.items(), key=str):
        print(f"  {shape:>5} | {str(is_null):16} | {str(category):22} | {count}")

    print(f"\nempty prediction lists: {empty_prediction_lists}")

    # The equivalence migration 0029 leans on.
    violations = [
        (shape, is_null, category, count)
        for (shape, is_null, category), count in crosstab.items()
        if is_null != (category == "random")
    ]
    if violations:
        print("\nPREDICTIONS RULE BROKEN — migration 0029 cannot be justified:")
        for shape, is_null, category, count in violations:
            print(f"  shape={shape} predictions_null={is_null} category={category} ({count})")
    else:
        print("\npredictions is null ⇔ category is random: holds for every word")

    if unmapped:
        print(f"\nunmapped categories to teach muni_category about: {unmapped}")
    return 1 if (violations or unmapped) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
