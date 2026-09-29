import json
import logging

from celery import shared_task
from django.db import IntegrityError, transaction

from core.models import (
    AnalysisFailureLog,
    AnalysisJob,
    Document,
    Paragraph,
    TestedParagraph,
    Word,
)
from core.muni import MUNI_JOB_DEADLINE_SECONDS, get_muni_client
from core.utils.DocumentAnalyzer import DocumentAnalyzer

logger = logging.getLogger(__name__)


def save_json_to_file(data, filename):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def _expected_embedding_for(content: str) -> bytes | None:
    """Encode multi-word `content` for later cosine scoring. Single-word
    expecteds are scored by exact match and need no embedding."""
    if len(content.split()) <= 1:
        return None
    from core.scoring import encode, serialize_embedding

    return serialize_embedding(encode(content))


def words_create(*, words):
    new_words = []
    for word in words:
        content = word.get("content") or ""
        try:
            embedding = _expected_embedding_for(content)
        except Exception:
            logger.exception("scoring: failed to encode expected content %r", content)
            embedding = None

        new_words.append(
            Word(
                paragraph_id=word.get("paragraph_id"),
                content=content,
                index=word.get("index"),
                source=word.get("source"),
                shape=word.get("shape"),
                selection_method=word.get("selection_method"),
                sentence_blanked=word.get("sentence_blanked"),
                sentence_index=word.get("sentence_index"),
                muni_category=word.get("muni_category"),
                muni_origin_category=word.get("muni_origin_category"),
                muni_pos_tags=word.get("muni_pos_tags"),
                predictions={
                    "words": [prediction for prediction in word.get("predictions", [])]
                }
                if word.get("predictions")
                else None,
                expected_embedding=embedding,
            )
        )
    return Word.objects.bulk_create(new_words)


# MUNI's NLP API is async (submit a job, then poll for the result); the
# protocol lives behind the client seam in `core/muni.py`. We run
# MUNI_API_CALLS attempts strictly one at a time: submit a job, poll it to a
# terminal state, collect its words, then start the next, so at most one aver
# job sits in MUNI's queue at once. Selection is stochastic, so raising the
# count broadens the word pool at the cost of one queue wait per attempt.
MUNI_API_CALLS = 1  # One queue wait per analysis; raise to broaden the word pool.


class AnalysisAborted(Exception):
    """The analysis job/document was deleted while we were polling MUNI.

    Raised by ``perform_ai_analysis`` so the caller can stop cleanly instead of
    polling to the deadline (and writing failure logs) for a document that no
    longer exists.
    """


def _method_from_pos(content: str, language_code: str) -> str | None:
    """Fallback bucket from stanza POS tags; None when tagging cannot help."""
    from core.utils.pos_method import classify_method

    try:
        return classify_method(content=content, language_code=language_code)
    except Exception:
        logger.exception(
            "perform_ai_analysis: POS classification failed for %r", content
        )
        return None


def _classify_returned_word(word: dict, language_code: str) -> dict:
    """Turn one raw MUNI API word dict into the row we persist."""
    from core.utils.muni_category import UNMAPPED, method_from_category

    content = word.get("content") or ""
    category = word.get("category") or None
    origin_category = word.get("origin_category") or None
    # Space-separated Universal POS tags, one per token of the n-gram.
    pos_tags = (word.get("pos_tags") or "").split() or None

    # The service says how it picked the word; that is the calibration
    # bucket, and it is the only thing that distinguishes a randomly removed
    # word from one its model chose. Its tags settle the one class the name
    # alone cannot. POS-tagging the content ourselves is the fallback for a
    # word the service told us too little about — one ingested before it
    # reported these fields, or a class we do not know.
    selection_method = method_from_category(category, pos_tags)
    if selection_method is UNMAPPED:
        if category is not None:
            logger.warning(
                "perform_ai_analysis: unmapped MUNI category %r for %r, "
                "falling back to POS tagging",
                category,
                content,
            )
        selection_method = _method_from_pos(content, language_code)

    # Shape is not cosmetic: `pick_test_words` keys off source, but the
    # export and the admin read shape, and `Word.shape_for` is what keeps it
    # consistent with the content. The stamped keys come after the spread so
    # they win by construction.
    return {
        **word,
        "source": Word.Source.MUNI_API,
        "shape": Word.shape_for(content),
        "selection_method": selection_method,
        "muni_category": category,
        "muni_origin_category": origin_category,
        "muni_pos_tags": " ".join(pos_tags) if pos_tags else None,
    }


def _log_failure(
    job: AnalysisJob | None,
    attempt: int | None,
    kind: str,
    summary: str,
    **details,
) -> None:
    """Persist one MUNI-call failure, in addition to the stdout log.

    Best-effort: if writing the log row itself raises (e.g. DB outage), we
    swallow the inner exception — the outer flow shouldn't be derailed by
    diagnostic plumbing.
    """
    if job is None:
        return
    try:
        AnalysisFailureLog.objects.create(
            analysis_job=job,
            attempt=attempt,
            kind=kind,
            summary=summary[:255],
            details=details,
        )
    except IntegrityError:
        # The parent job/document was deleted mid-analysis (cascade). There's
        # nothing to attach the failure to — expected, so log one quiet line
        # rather than a FK-constraint traceback.
        logger.warning(
            "perform_ai_analysis: job %s gone; skipping %s failure log", job.pk, kind
        )
    except Exception:
        logger.exception(
            "perform_ai_analysis: could not persist failure log row for job %s", job.pk
        )


def perform_ai_analysis(
    document: Document,
    document_paragraphs: list[Paragraph],
    job: AnalysisJob | None = None,
    client=None,
):
    """Submit MUNI_API_CALLS async jobs to the MUNI NLP API, poll them to
    completion, and collect the word picks.

    Tolerates partial failure: each job is independent. Submission failures,
    failed jobs, expirations and per-job deadline overruns are logged (both to
    stdout and to AnalysisFailureLog when `job` is given) and skipped. Returns
    whatever words we collected; any non-empty result is committed as a
    partial-success analysis. Raises if we ended up with nothing to commit --
    the caller only distinguishes success from failure by the exception, so
    returning an empty list would mark the job 'completed' with an empty test.
    """
    client = client or get_muni_client()
    data = {
        "language": document.language.code,
        "document_id": str(document.pk),
        "paragraphs": [
            {"id": str(paragraph.pk), "content": paragraph.content}
            for paragraph in document_paragraphs
        ],
    }

    def check_abort(where: str = "") -> None:
        """Stop the run when the job/document was deleted underneath us."""
        if job is not None and not AnalysisJob.objects.filter(pk=job.pk).exists():
            logger.info(
                "perform_ai_analysis: job %s deleted %s; aborting", job.pk, where
            )
            raise AnalysisAborted(f"analysis job {job.pk} deleted mid-analysis")

    language_code = document.language.code
    analyzed_words = []
    successes = 0
    failures = 0

    logger.info(
        "perform_ai_analysis: starting for document %s — %d paragraphs, up to %d "
        "MUNI attempt(s) run one at a time, %ds deadline each",
        document.pk, len(data["paragraphs"]), MUNI_API_CALLS, MUNI_JOB_DEADLINE_SECONDS,
    )

    # Run each MUNI attempt strictly one at a time: submit a job, poll it to a
    # terminal state (or its per-call deadline), collect its words, then start
    # the next. MUNI's selection is stochastic, so the attempts broaden the word
    # pool; running them sequentially keeps only one job in MUNI's queue at once.
    for i in range(MUNI_API_CALLS):
        attempt = i + 1

        def on_failure(kind, summary, *, _attempt=attempt, **details):
            _log_failure(job, _attempt, kind, summary, **details)

        words = client.run_attempt(
            data,
            attempt=attempt,
            total_attempts=MUNI_API_CALLS,
            deadline_seconds=MUNI_JOB_DEADLINE_SECONDS,
            check_abort=check_abort,
            on_failure=on_failure,
        )
        if words is None:
            failures += 1
        else:
            successes += 1
            for word in words:
                analyzed_words.append(_classify_returned_word(word, language_code))
        logger.info(
            "perform_ai_analysis: attempt %d/%d done (%s) — running totals: "
            "successes=%d failures=%d words=%d",
            attempt, MUNI_API_CALLS, "no words" if words is None else f"{len(words)} words",
            successes, failures, len(analyzed_words),
        )

    logger.info(
        "perform_ai_analysis: document %s done. successes=%d/%d failures=%d words=%d",
        document.pk, successes, MUNI_API_CALLS, failures, len(analyzed_words),
    )
    if not analyzed_words:
        # Nothing to commit — either every job failed, or they all "succeeded"
        # with an empty word list, which would otherwise land as a 'completed'
        # analysis holding an empty test. Surface it so the job is marked failed
        # and the user is told to retry.
        _log_failure(
            job, None, "unhandled",
            f"no words from {MUNI_API_CALLS} MUNI jobs",
            failures=failures, successes=successes,
        )
        raise Exception(
            f"no words from {MUNI_API_CALLS} MUNI analysis jobs for document "
            f"{document.pk} (successes={successes} failures={failures})"
        )
    return analyzed_words


# Ceiling: MUNI_API_CALLS sequential attempts of up to MUNI_JOB_DEADLINE_SECONDS
# each, plus an hour of slack. Holds one worker slot the whole time.
START_ANALYSIS_TIME_LIMIT_SECONDS = (
    MUNI_API_CALLS * MUNI_JOB_DEADLINE_SECONDS + 3600
)


def request_analysis(document: Document, analysis_type: str = "ai") -> AnalysisJob:
    """Create the job row and enqueue its worker as one operation."""
    job = AnalysisJob.objects.create(
        document=document, analysis_type=analysis_type, status="pending"
    )
    start_analysis.delay(job.pk)
    return job


@shared_task(
    time_limit=START_ANALYSIS_TIME_LIMIT_SECONDS,
    soft_time_limit=START_ANALYSIS_TIME_LIMIT_SECONDS - 900,
)
def start_analysis(analysis_job_pk: int):
    job = (
        AnalysisJob.objects.select_related("document__language")
        .filter(pk=analysis_job_pk)
        .first()
    )
    if job is None:
        logger.error("start_analysis: AnalysisJob %s not found", analysis_job_pk)
        return
    document = job.document

    job.mark_running()

    try:
        document_paragraphs = list(
            Paragraph.objects.filter(document=document, language=document.language)
        )

        if job.analysis_type == "basic":
            analyzed_words = DocumentAnalyzer(
                paragraphs=document_paragraphs, document_language=document.language
            ).get_analyzed_words()
        elif job.analysis_type == "ai":
            analyzed_words = perform_ai_analysis(
                document, document_paragraphs, job=job
            )
        else:
            raise ValueError(f"Invalid analysis type: {job.analysis_type}")

        with transaction.atomic():
            words_create(words=analyzed_words)
            job.mark_completed()

    except AnalysisAborted:
        # The document/job was deleted mid-analysis. Nothing left to update or
        # mark failed — the row is already gone. Stop quietly.
        logger.info(
            "start_analysis: %s analysis for document %s aborted "
            "(job/document deleted mid-analysis)",
            job.analysis_type, document.pk,
        )
        return
    except Exception:
        logger.exception(
            "Analysis job %s failed for document %s", job.analysis_type, document.pk
        )
        job.mark_failed()


def _score_paragraph(paragraph: TestedParagraph) -> None:
    """Compute and persist the score + grade for one answered paragraph.

    Avoids loading the embedding model when it isn't needed: empty answers,
    exact (case-insensitive) matches, and single-word expecteds all short
    out before any encoding work.
    """
    from core.scoring import (
        SIMILARITY_THRESHOLD,  # noqa: F401  (used implicitly via grade_from_score)
        deserialize_embedding,
        encode,
        grade_from_score,
        score_answer,
        serialize_embedding,
    )

    word = paragraph.word
    expected = word.content or ""
    answer = paragraph.answered_word or ""

    expected_vec = None
    if word.expected_embedding:
        expected_vec = deserialize_embedding(bytes(word.expected_embedding))
    elif (
        answer
        and answer.strip().lower() != expected.strip().lower()
        and len(expected.split()) > 1
    ):
        # Slow path will need it — encode once and cache for next time.
        expected_vec = encode(expected)
        word.expected_embedding = serialize_embedding(expected_vec)
        word.save(update_fields=["expected_embedding"])

    score = score_answer(answer, expected, expected_embedding=expected_vec)
    paragraph.computed_score = score
    paragraph.computed_grade = grade_from_score(score) if answer else ""
    paragraph.save(update_fields=["computed_score", "computed_grade"])


@shared_task(time_limit=300, soft_time_limit=240)
def score_paragraph(tested_paragraph_pk: int) -> None:
    try:
        paragraph = TestedParagraph.objects.select_related("word").get(
            pk=tested_paragraph_pk
        )
    except TestedParagraph.DoesNotExist:
        logger.error("score_paragraph: %s not found", tested_paragraph_pk)
        return
    _score_paragraph(paragraph)


@shared_task(time_limit=1800, soft_time_limit=1500)
def score_test(test_pk) -> None:
    """Score every paragraph in a test that hasn't been scored yet.

    Idempotent — safe to fire on submit as a backstop even if per-paragraph
    tasks already ran or are still in flight."""
    paragraphs = TestedParagraph.objects.select_related("word").filter(
        test_id=test_pk, computed_score__isnull=True
    )
    for paragraph in paragraphs:
        try:
            _score_paragraph(paragraph)
        except Exception:
            logger.exception("score_test: failed to score paragraph %s", paragraph.pk)


def dispatch_score_paragraph(tested_paragraph_pk: int) -> None:
    """Run score_paragraph inline if SCORE_INLINE is set (tests), else enqueue."""
    from django.conf import settings

    if getattr(settings, "SCORE_INLINE", False):
        score_paragraph(tested_paragraph_pk)
    else:
        score_paragraph.delay(tested_paragraph_pk)


def dispatch_score_test(test_pk) -> None:
    from django.conf import settings

    if getattr(settings, "SCORE_INLINE", False):
        score_test(test_pk)
    else:
        score_test.delay(test_pk)
