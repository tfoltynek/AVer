"""
Sentence similarity using sentence-transformers.

Computes cosine similarity between sequences with a multilingual embedding model
(default works for en/cs/sk). The model is loaded lazily on first use and cached
for the process. This is a fast, synchronous operation served directly by the
web tier — it does not go through the job queue.

Configure with:
    AVER_SIMILARITY_MODEL       HF model id (default: multilingual MiniLM L12)
    AVER_SIMILARITY_THRESHOLD   cosine cutoff for the match boolean (default 0.7172)
"""

import logging
import os
import threading

logger = logging.getLogger("aver.similarity")

DEFAULT_MODEL = os.getenv(
    "AVER_SIMILARITY_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)

# Pairs with cosine similarity >= THRESHOLD are reported as a match. The default
# was calibrated on the AVer answer-similarity data.
THRESHOLD = float(os.getenv("AVER_SIMILARITY_THRESHOLD", "0.7172"))

_model = None
_model_lock = threading.Lock()


def model_name():
    return DEFAULT_MODEL


def threshold():
    return THRESHOLD


def is_match(score):
    """True if a cosine similarity score counts as a match."""
    return score >= THRESHOLD


def get_model():
    """Load and cache the embedding model (thread-safe, once per process)."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                logger.info("Loading similarity model: %s", DEFAULT_MODEL)
                _model = SentenceTransformer(DEFAULT_MODEL, device="cpu")
                logger.info("Similarity model loaded.")
    return _model


def _encode(texts):
    model = get_model()
    # normalize_embeddings=True makes the dot product equal to cosine similarity.
    return model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)


def similarity(text1, text2):
    """Cosine similarity of two strings, in [-1.0, 1.0]."""
    emb = _encode([text1, text2])
    return float(emb[0] @ emb[1])


def similarity_batch(pairs):
    """Cosine similarity for a list of (text1, text2) pairs."""
    if not pairs:
        return []
    flat = [t for pair in pairs for t in pair]
    emb = _encode(flat)
    return [float(emb[2 * i] @ emb[2 * i + 1]) for i in range(len(pairs))]
