"""Regression tests for document-level language detection.

Guards against the bug where a Czech thesis was occasionally labelled Slovak.
The old `get_document_language` ran Lingua on a random, *unseeded* 20-paragraph
sample; because Czech and Slovak are very close and Lingua's per-subset call is
unstable, the same document came out cs most of the time and sk a few percent of
the time. The fix detects from the whole document, which is both deterministic
and unambiguously Czech.

These are pure-function tests (no DB / browser); they request no fixtures.
"""

import glob
import json
from pathlib import Path

from core.utils import filter_valid_paragraphs, get_document_language
from core.utils.detect_language import detect_language

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "test_documents" / "data"
DISCOVERY = REPO_ROOT / "test_documents" / "discovery"


def _czech_corpus() -> list[str]:
    """A multi-paragraph Czech corpus (>20 paragraphs) from committed fixtures:
    the Czech demo documents plus the Czech discovery texts.

    >20 matters: it's larger than the old sample size, so the old random-sample
    code actually subset the document and could flip to sk. The corpus here
    reproduced ~5% sk over 150 trials of the old code.
    """
    paragraphs: list[str] = []
    for path in sorted(glob.glob(str(DATA / "cs_*_request.json"))):
        with open(path, encoding="utf-8") as fh:
            for para in json.load(fh).get("paragraphs", []):
                content = (para.get("content") or "").strip()
                if content:
                    paragraphs.append(content)
    for path in sorted(glob.glob(str(DISCOVERY / "cs_*.txt"))):
        paragraphs.extend(Path(path).read_text(encoding="utf-8").split("\n\n"))
    return filter_valid_paragraphs(paragraphs)


def _paragraphs_from_txt(name: str) -> list[str]:
    text = (DATA / name).read_text(encoding="utf-8")
    return filter_valid_paragraphs(text.split("\n\n"))


def test_get_document_language_is_deterministic_for_czech():
    """A Czech document must always resolve to 'cs' — never randomly to 'sk'."""
    paragraphs = _czech_corpus()
    assert len(paragraphs) > 20, "need >20 paragraphs to exercise the old sampling bug"

    results = {get_document_language(paragraphs) for _ in range(30)}

    assert results == {"cs"}, (
        f"language detection is not stably Czech: got {results}. "
        "It must not depend on a random paragraph sample."
    )


def test_get_document_language_does_not_overcorrect_slovak():
    """The fix must not push genuine Slovak documents to Czech."""
    assert get_document_language(_paragraphs_from_txt("sample-small-sk.txt")) == "sk"


def test_get_document_language_small_czech_sample():
    assert get_document_language(_paragraphs_from_txt("sample-small-cs.txt")) == "cs"


CYRILLIC_PARAGRAPH = (
    "Это тестовый документ, написанный на русском языке. Он содержит несколько "
    "предложений и абзацев, чтобы проверить, как ведёт себя детектор языка."
)


def test_detect_language_returns_none_without_letters():
    """Digits and punctuation carry no alphabet signal: None, not a guess."""
    assert detect_language("1234 5678 ---- ....") is None


def test_get_document_language_returns_none_for_unsupported_script():
    """Cyrillic passes the paragraph filter but matches none of cs / sk / en."""
    paragraphs = filter_valid_paragraphs([CYRILLIC_PARAGRAPH] * 6)
    assert len(paragraphs) >= 5, "Cyrillic must survive filtering for this test to mean anything"
    assert get_document_language(paragraphs) is None
