"""Sentence-embedding scoring for multi-word answers.

Single-word answers are scored by case-insensitive exact match.
Multi-word answers (bigram/trigram) are scored by cosine similarity of the
whole-phrase embeddings from paraphrase-multilingual-MiniLM-L12-v2.

The transformer model is loaded lazily and only inside the process that calls
`score_answer` — typically the Celery worker, never the Django request path.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

logger = logging.getLogger(__name__)

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
SIMILARITY_THRESHOLD = 0.7172

_model = None
_model_lock = threading.Lock()


def _load_model():
    """Lazy-load the SentenceTransformer with the int8-quantized ONNX backend.

    Falls back to PyTorch if the quantized ONNX file is unavailable.
    """
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        from sentence_transformers import SentenceTransformer

        try:
            _model = SentenceTransformer(
                MODEL_NAME,
                backend="onnx",
                model_kwargs={"file_name": "onnx/model_qint8_avx512_vnni.onnx"},
            )
            logger.info("scoring: loaded %s (onnx int8)", MODEL_NAME)
        except Exception:
            logger.warning(
                "scoring: ONNX int8 load failed for %s — falling back to torch",
                MODEL_NAME,
                exc_info=True,
            )
            _model = SentenceTransformer(MODEL_NAME)
        return _model


def encode(text: str) -> np.ndarray:
    """Return a float32 embedding for `text`."""
    model = _load_model()
    vec = model.encode(text, convert_to_numpy=True, normalize_embeddings=True)
    return vec.astype(np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity for two L2-normalized vectors."""
    return float(np.dot(a, b))


def _normalize(text: str) -> str:
    return text.strip().lower()


def score_answer(
    answer: str,
    expected: str,
    expected_embedding: np.ndarray | None = None,
) -> float:
    """Return a 0.0–1.0 score for `answer` against `expected`.

    Dispatch on whitespace-split length of `expected`:
      - 1 token  → case-insensitive exact match (1.0 / 0.0)
      - 2+ tokens → 1.0 on exact match, raw cosine ∈ [0, 1] otherwise
    """
    if not answer:
        return 0.0
    if _normalize(answer) == _normalize(expected):
        return 1.0

    if len(expected.split()) <= 1:
        return 0.0

    answer_vec = encode(answer)
    if expected_embedding is None:
        expected_embedding = encode(expected)
    sim = cosine(answer_vec, expected_embedding)
    return max(0.0, min(1.0, sim))


def grade_from_score(score: float) -> str:
    """Binary grade: 'correct' iff score >= SIMILARITY_THRESHOLD, else 'incorrect'."""
    return "correct" if score >= SIMILARITY_THRESHOLD else "incorrect"


def serialize_embedding(vec: np.ndarray) -> bytes:
    """fp16 packing — halves storage, well within rounding for cosine."""
    return vec.astype(np.float16).tobytes()


def deserialize_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float16).astype(np.float32)
