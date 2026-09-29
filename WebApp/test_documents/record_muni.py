#!/usr/bin/env python3
"""Talk to the live MUNI NLP API to record fixtures or discover its vocabulary.

Two subcommands, both sequential (the service handles one job at a time) and
polite (first poll after 30 s, then whatever ``Retry-After`` asks for):

    replay    Re-send every ``*_request.json`` in test_documents/data unchanged
              and overwrite the sibling ``*_response.json`` with the live
              answer. Fails loudly if a document comes back with no words, or
              if the alphabetically first document of a language (what the
              try-mode picker shows first) has fewer than two.

    discover  Send every ``*.txt`` in a directory (paragraphs separated by
              blank lines, language from the ``cs_`` / ``sk_`` / ``en_``
              filename prefix) and tabulate the ``category`` and
              ``origin_category`` values the API returns, cross-tabulated with
              shape and with whether ``predictions`` is null. Raw responses go
              to --out.

              Selection is deterministic: the same text submitted twice comes
              back with the same words, in the same order, down to the index
              (verified 2026-09-20 on two separate jobs). Widening the
              vocabulary therefore means more varied documents, not more
              rounds over the same ones, and a document already present in
              --out is skipped rather than resubmitted.

Standalone: needs only ``requests``. Run with ``uv run python``.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import pathlib
import sys
import time
import uuid

import requests

BASE = "https://nlp.fi.muni.cz/projekty/aver/api.cgi"
DATA_DIR = pathlib.Path(__file__).resolve().parent / "data"
METADATA = pathlib.Path(__file__).resolve().parent / "metadata.json"
FIRST_POLL_S = 30
MIN_POLL_S = 30
# The contract spec records queue waits of 33 and 146 minutes, so a short
# deadline would abandon jobs that are still going to run on their side.
JOB_DEADLINE_S = 4 * 60 * 60
HTTP_TIMEOUT_S = 30
TERMINAL_OK = {"finished", "done", "completed", "success"}


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ── protocol ────────────────────────────────────────────────────────────────


def run_job(payload: dict) -> dict:
    """Submit one document, poll to a terminal state, return the result body."""
    r = requests.post(f"{BASE}/document", json=payload, timeout=HTTP_TIMEOUT_S)
    if r.status_code != 202:
        raise RuntimeError(f"submit: HTTP {r.status_code}: {r.text[:300]}")
    job_id = r.json()["job_id"]
    log(f"  job {job_id} queued ({len(payload['paragraphs'])} paragraphs)")

    started = time.monotonic()
    wait = FIRST_POLL_S
    while True:
        time.sleep(wait)
        if time.monotonic() - started > JOB_DEADLINE_S:
            raise TimeoutError(f"job {job_id} not finished within {JOB_DEADLINE_S}s")
        s = requests.get(f"{BASE}/jobs/{job_id}", timeout=HTTP_TIMEOUT_S)
        body = s.json() if s.headers.get("content-type", "").startswith("application/json") else {}
        state = body.get("state")
        if state in TERMINAL_OK:
            break
        if state == "failed":
            raise RuntimeError(f"job {job_id} failed: {body.get('error')}")
        wait = max(MIN_POLL_S, int(s.headers.get("Retry-After") or 0))

    res = requests.get(f"{BASE}/jobs/{job_id}/result", timeout=HTTP_TIMEOUT_S)
    if res.status_code != 200:
        raise RuntimeError(f"result: HTTP {res.status_code}: {res.text[:300]}")
    elapsed = time.monotonic() - started
    data = res.json()
    log(f"  job {job_id} finished in {elapsed:.0f}s, {len(data.get('words') or [])} words")
    return data


def dump_json(path: pathlib.Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ── replay ──────────────────────────────────────────────────────────────────


def first_title_per_language() -> dict[str, str]:
    """Request filename of the document the try-mode picker lists first, per language."""
    meta = json.loads(METADATA.read_text(encoding="utf-8"))
    firsts: dict[str, tuple[str, str]] = {}
    for filename, info in meta.items():
        lang = filename.split("_", 1)[0]
        key = (info["title"], filename)
        if lang not in firsts or key < firsts[lang]:
            firsts[lang] = key
    return {lang: fn for lang, (_, fn) in firsts.items()}


def cmd_replay(args: argparse.Namespace) -> int:
    requests_files = sorted(DATA_DIR.glob("*_request.json"))
    if args.only:
        requests_files = [f for f in requests_files if f.name in set(args.only)]
    firsts = first_title_per_language()
    log(f"replaying {len(requests_files)} fixture requests; first-by-title: {firsts}")

    problems: list[str] = []
    for req_path in requests_files:
        payload = json.loads(req_path.read_text(encoding="utf-8"))
        log(f"{req_path.name}")
        data = run_job(payload)
        words = data.get("words") or []
        n = len(words)
        resp_path = req_path.with_name(
            req_path.name.replace("_request.json", "_response.json")
        )
        if n == 0:
            # A demo document with no words cannot be taken in try mode, and
            # the service does return an empty list for short or unusual
            # texts. Keep whatever we had rather than break the corpus.
            problems.append(f"{req_path.name}: 0 words, kept the previous recording")
            log("  0 words — keeping the previous recording")
            continue
        if req_path.name in firsts.values() and n < 2:
            problems.append(
                f"{req_path.name}: first-by-title document has only {n} word(s)"
            )
        if not args.dry_run:
            dump_json(resp_path, data)
        cats = collections.Counter(w.get("category") for w in words)
        log(f"  {n} words, categories: {dict(cats)}")

    if problems:
        log("needs a look before committing:")
        for p in problems:
            log(f"  - {p}")
        return 1
    log("replay done, every document came back usable")
    return 0


# ── discover ────────────────────────────────────────────────────────────────


def load_corpus(directory: pathlib.Path) -> list[tuple[str, str, list[str]]]:
    docs = []
    for path in sorted(directory.glob("*.txt")):
        lang = path.name.split("_", 1)[0]
        if lang not in {"cs", "sk", "en"}:
            log(f"skipping {path.name}: no language prefix")
            continue
        paragraphs = [p.strip() for p in path.read_text(encoding="utf-8").split("\n\n") if p.strip()]
        docs.append((path.stem, lang, paragraphs))
    return docs


def shape_of(content: str) -> int:
    return len((content or "").split())


def tally(w, rnd, name, lang, seen_categories, seen_origins, new_this_round,
          cat_counter, origin_counter, crosstab, per_language, all_words) -> None:
    """Fold one returned word into the running tables."""
    cat = w.get("category")
    origin = w.get("origin_category")
    preds = w.get("predictions")
    if cat is not None and cat not in seen_categories:
        new_this_round.add(cat)
        seen_categories.add(cat)
    if origin is not None and origin not in seen_origins:
        new_this_round.add(f"origin:{origin}")
        seen_origins.add(origin)
    cat_counter[cat] += 1
    origin_counter[origin] += 1
    crosstab[(shape_of(w.get("content")), preds is None, cat)] += 1
    per_language[lang][cat] += 1
    all_words.append({"round": rnd, "doc": name, "lang": lang, **w})


def cmd_discover(args: argparse.Namespace) -> int:
    corpus = load_corpus(pathlib.Path(args.corpus))
    if not corpus:
        log("no *.txt documents found")
        return 2
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log(f"{len(corpus)} documents, up to {args.rounds} rounds, output in {out}")

    seen_categories: set[str] = set()
    seen_origins: set[str] = set()
    cat_counter: collections.Counter = collections.Counter()
    origin_counter: collections.Counter = collections.Counter()
    # (shape, predictions is null, category) -> count
    crosstab: collections.Counter = collections.Counter()
    empty_predictions = 0
    per_language: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    all_words: list[dict] = []

    for rnd in range(1, args.rounds + 1):
        new_this_round: set[str] = set()
        log(f"=== round {rnd} ===")
        for name, lang, paragraphs in corpus:
            payload = {
                "language": lang,
                "document_id": str(uuid.uuid4()),
                "paragraphs": [{"id": str(uuid.uuid4()), "content": p} for p in paragraphs],
            }
            target = out / f"round{rnd}_{name}.json"
            if target.exists():
                log(f"{name}: already recorded, skipping (selection is deterministic)")
                for w in json.load(target.open(encoding="utf-8"))["response"].get("words") or []:
                    tally(w, rnd, name, lang, seen_categories, seen_origins, new_this_round,
                          cat_counter, origin_counter, crosstab, per_language, all_words)
                continue
            log(f"{name} ({lang}, {len(paragraphs)} paragraphs)")
            try:
                data = run_job(payload)
            except Exception as exc:  # keep going, one bad job must not end the run
                log(f"  ERROR: {exc}")
                continue
            dump_json(out / f"round{rnd}_{name}.json", {"request": payload, "response": data})
            for w in data.get("words") or []:
                preds = w.get("predictions")
                if isinstance(preds, list) and len(preds) == 0:
                    empty_predictions += 1
                tally(w, rnd, name, lang, seen_categories, seen_origins, new_this_round,
                      cat_counter, origin_counter, crosstab, per_language, all_words)
            # Summary after every document: the queue is slow enough that a
            # partial run has to be usable.
            write_summary(out, cat_counter, origin_counter, crosstab,
                          per_language, empty_predictions, all_words)
        log(f"round {rnd}: new values {sorted(new_this_round) or 'none'}")
        report(cat_counter, origin_counter, crosstab, per_language, empty_predictions)
        if not new_this_round and rnd >= args.min_rounds:
            log("no new values in a full round, stopping")
            break

    write_summary(out, cat_counter, origin_counter, crosstab, per_language,
                  empty_predictions, all_words)
    log(f"summary written to {out / 'summary.json'}")
    return 0


def write_summary(out, cat_counter, origin_counter, crosstab, per_language,
                  empty_predictions, all_words) -> None:
    summary = {
        "categories": dict(cat_counter),
        "origin_categories": dict(origin_counter),
        "crosstab_shape_predictionsnull_category": {
            f"shape={sh} preds_null={pn} category={c}": n
            for (sh, pn, c), n in sorted(crosstab.items(), key=str)
        },
        "per_language": {k: dict(v) for k, v in per_language.items()},
        "empty_prediction_lists": empty_predictions,
        "word_keys": sorted(
            {k for w in all_words for k in w if k not in {"round", "doc", "lang"}}
        ),
        "n_words": len(all_words),
    }
    dump_json(out / "summary.json", summary)
    dump_json(out / "all_words.json", {"words": all_words})


def report(cats, origins, crosstab, per_language, empty_predictions) -> None:
    log("  category counts:      " + json.dumps(dict(cats), ensure_ascii=False))
    log("  origin_category:      " + json.dumps(dict(origins), ensure_ascii=False))
    log("  per language:         " + json.dumps({k: dict(v) for k, v in per_language.items()}, ensure_ascii=False))
    log(f"  empty prediction lists: {empty_predictions}")
    log("  shape | preds null | category | n")
    for (s, p, c), n in sorted(crosstab.items(), key=str):
        log(f"    {s:>2}    | {str(p):<10} | {str(c):<24} | {n}")


# ── main ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("replay", help="re-record test_documents/data/*_response.json from the live API")
    rp.add_argument("--only", nargs="*", help="limit to these *_request.json file names")
    rp.add_argument("--dry-run", action="store_true", help="talk to the API but do not overwrite fixtures")
    rp.set_defaults(func=cmd_replay)

    dp = sub.add_parser("discover", help="collect category values from a directory of *.txt documents")
    dp.add_argument("corpus", help="directory with cs_*.txt / sk_*.txt / en_*.txt")
    dp.add_argument("--out", required=True, help="directory for raw responses and summary.json")
    dp.add_argument("--rounds", type=int, default=1,
                    help="passes over the corpus (default 1; selection is deterministic, "
                         "so a second pass only helps for documents added meanwhile)")
    dp.add_argument("--min-rounds", type=int, default=1, help="never stop before this many rounds")
    dp.set_defaults(func=cmd_discover)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
