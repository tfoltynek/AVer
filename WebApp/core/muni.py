"""MUNI NLP API client.

The async protocol (submit a job, poll it to a terminal state, fetch its
result) lives here behind one interface:

    client.run_attempt(payload, attempt=..., total_attempts=...,
                       deadline_seconds=..., check_abort=..., on_failure=...)
        -> list[dict] | None
    client.probe() -> ProbeResult

`HttpMuniClient` speaks the real protocol (202/409/404 semantics,
Retry-After, per-attempt deadlines); `FakeMuniClient` replays scripted
attempts for tests. Failure *events* leave through the `on_failure`
callback as data (kind, summary, details) — persisting them is the
caller's business, so the client never touches the ORM. Abort checks come
in through `check_abort`, which raises to stop a run (the client calls it
before submitting and on every polling round).
"""

import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

MUNI_API_BASE = os.environ.get("MUNI_API_BASE", "https://nlp.fi.muni.cz/projekty/aver/api.cgi").rstrip("/")
MUNI_SUBMIT_TIMEOUT_SECONDS = 30  # POST /document just enqueues, returns 202 fast.
MUNI_POLL_TIMEOUT_SECONDS = 30  # Per GET request; status/result are cheap reads.
MUNI_POLL_INTERVAL_SECONDS = 5  # Sleep between polling rounds (tests set to 0).
# Per-job wall-clock budget. Queue waits run to hours (33 and 146 min observed,
# see docs/superpowers/specs/2026-07-20-muni-async-api-contract.md), so this is
# queue patience, not compute time. MUNI keeps results 7 days, well past this.
MUNI_JOB_DEADLINE_SECONDS = 86400

# on_failure(kind: str, summary: str, **details) — kinds match
# AnalysisFailureLog.KIND_CHOICES by convention.
FailureHook = Callable[..., None]
# check_abort(where: str) — raises to stop the run.
AbortHook = Callable[[str], None]


def _noop_failure(kind: str, summary: str, **details) -> None:
    return None


def _noop_abort(where: str = "") -> None:
    return None


@dataclass
class ProbeResult:
    """Outcome of one health-check submit handshake."""

    is_up: bool
    status_code: int | None
    latency_ms: int
    error: str | None


class _ResultNotReady:
    """Type of the _NOT_READY sentinel — distinct so callers can narrow it out."""


# Sentinel: the result endpoint answered 409 (job finished per status, but the
# result isn't materialized yet). Keep polling.
_NOT_READY = _ResultNotReady()


def _body_excerpt(response) -> str:
    """First 1 KiB of a response body, for diagnostics. Never raises."""
    try:
        return (response.text or "")[:1024]
    except Exception:
        return ""


def _retry_after_seconds(response) -> float | None:
    """Parse a numeric `Retry-After` header (seconds). Ignores HTTP-date form."""
    try:
        value = response.headers.get("Retry-After")
    except Exception:
        return None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _progress_text(body: dict | None) -> str:
    """Human-readable ``done/total`` from MUNI's ``progress`` field, or ""."""
    progress = (body or {}).get("progress") or {}
    total = progress.get("total")
    if total:
        return f" {progress.get('done', 0)}/{total}"
    return ""


class HttpMuniClient:
    """The real MUNI adapter: owns URLs, status semantics and wait policy."""

    def __init__(
        self,
        *,
        base_url: str = MUNI_API_BASE,
        poll_interval: float = MUNI_POLL_INTERVAL_SECONDS,
        submit_timeout: float = MUNI_SUBMIT_TIMEOUT_SECONDS,
        poll_timeout: float = MUNI_POLL_TIMEOUT_SECONDS,
    ):
        self._base_url = base_url
        self._poll_interval = poll_interval
        self._submit_timeout = submit_timeout
        self._poll_timeout = poll_timeout

    @property
    def submit_url(self) -> str:
        return f"{self._base_url}/document"

    def _status_url(self, job_id: str) -> str:
        return f"{self._base_url}/jobs/{job_id}"

    def _result_url(self, job_id: str) -> str:
        return f"{self._base_url}/jobs/{job_id}/result"

    def _submit(
        self, payload: dict, *, attempt: int, total_attempts: int, on_failure: FailureHook
    ) -> str | None:
        """Submit one analysis job. Returns its job_id, or None on failure."""
        document_id = payload.get("document_id")
        try:
            response = requests.post(
                self.submit_url, json=payload, timeout=self._submit_timeout
            )
        except requests.exceptions.RequestException as exc:
            logger.exception(
                "muni: submit %d/%d network error for document %s",
                attempt, total_attempts, document_id,
            )
            on_failure(
                "network_error",
                f"{type(exc).__name__}: {exc}",
                phase="submit", exception_type=type(exc).__name__,
            )
            return None

        if response.status_code != requests.codes.accepted:  # 202
            logger.warning(
                "muni: submit %d/%d returned status %s for document %s",
                attempt, total_attempts, response.status_code, document_id,
            )
            on_failure(
                "http_status",
                f"MUNI submit returned HTTP {response.status_code}",
                phase="submit", status_code=response.status_code,
                body_excerpt=_body_excerpt(response),
            )
            return None

        try:
            body = response.json()
        except ValueError as exc:
            logger.exception(
                "muni: submit %d/%d returned non-JSON body for %s",
                attempt, total_attempts, document_id,
            )
            on_failure(
                "non_json",
                f"non-JSON submit body: {exc}",
                phase="submit", body_excerpt=_body_excerpt(response),
            )
            return None

        job_id = (body or {}).get("job_id")
        if not job_id:
            logger.warning(
                "muni: submit %d/%d returned 202 without a job_id for %s",
                attempt, total_attempts, document_id,
            )
            on_failure(
                "bad_submit_response",
                "MUNI returned 202 without a job_id",
                phase="submit", body_excerpt=_body_excerpt(response),
            )
            return None

        return str(job_id)

    def _poll_state(
        self, job_id: str, *, on_failure: FailureHook
    ) -> tuple[str, float | None, str]:
        """Check one job's status via GET /jobs/<id>.

        Returns ``(outcome, retry_after, status_text)`` where outcome is one of
        ``finished`` / ``pending`` / ``failed`` / ``gone``. Transient errors and
        not-yet-terminal states map to ``pending`` so the caller re-polls
        (bounded by the per-attempt deadline); ``retry_after`` carries a parsed
        Retry-After hint; ``status_text`` is a short human description.
        """
        try:
            response = requests.get(self._status_url(job_id), timeout=self._poll_timeout)
        except requests.exceptions.RequestException as exc:
            # Transient — keep polling until the deadline; don't fail the slot.
            logger.warning(
                "muni: status poll network error for job %s: %s", job_id, exc
            )
            return "pending", None, "unreachable (network error)"

        if response.status_code == requests.codes.not_found:  # 404
            logger.warning("muni: job %s unknown/expired (404)", job_id)
            on_failure(
                "expired",
                "MUNI job unknown or expired (status 404)",
                phase="poll", job_id=job_id,
            )
            return "gone", None, "gone (expired)"

        if response.status_code != requests.codes.ok:  # treat as transient, re-poll
            logger.warning(
                "muni: status poll for job %s returned %s",
                job_id, response.status_code,
            )
            return "pending", _retry_after_seconds(response), f"HTTP {response.status_code}"

        try:
            body = response.json()
        except ValueError:
            # A 200 carrying HTML is a proxy/WAF page, not MUNI speaking — same
            # class of transient fault as a 5xx, so re-poll rather than killing
            # the slot.
            logger.warning(
                "muni: status poll for job %s returned non-JSON body", job_id
            )
            on_failure(
                "non_json",
                "non-JSON status body",
                phase="poll", job_id=job_id, body_excerpt=_body_excerpt(response),
            )
            return "pending", _retry_after_seconds(response), "non-JSON body"

        state = (body or {}).get("state")
        if state == "finished":
            return "finished", None, "finished"
        if state == "failed":
            error = (body or {}).get("error")
            logger.warning(
                "muni: job %s reported state=failed (%s)", job_id, error
            )
            on_failure(
                "job_failed",
                f"MUNI job failed: {error}",
                phase="poll", job_id=job_id, error=error,
            )
            return "failed", None, f"failed ({error})"

        # queued / processing / missing → keep waiting.
        return (
            "pending",
            _retry_after_seconds(response),
            f"{state or 'pending'}{_progress_text(body)}",
        )

    def _fetch_result(
        self, job_id: str, *, on_failure: FailureHook
    ) -> tuple[list | None | _ResultNotReady, float | None]:
        """Fetch a finished job's result via GET /jobs/<id>/result.

        Returns ``(value, retry_after)``. ``value`` is a list of raw word dicts
        on success (possibly empty), the ``_NOT_READY`` sentinel if the result
        isn't materialized yet (409 or a transient error), or None on failure.
        ``retry_after`` carries the response's parsed Retry-After hint, which
        MUNI does send on its 409s; it is None for every terminal outcome.
        """
        try:
            response = requests.get(self._result_url(job_id), timeout=self._poll_timeout)
        except requests.exceptions.RequestException as exc:
            # Status said finished but the fetch hiccuped — re-poll next round.
            logger.warning(
                "muni: result fetch network error for job %s: %s", job_id, exc
            )
            return _NOT_READY, None

        if response.status_code == requests.codes.conflict:  # 409 — pending
            return _NOT_READY, _retry_after_seconds(response)

        if response.status_code == requests.codes.not_found:  # 404
            logger.warning("muni: result for job %s unknown/expired (404)", job_id)
            on_failure(
                "expired",
                "MUNI result unknown or expired (status 404)",
                phase="result", job_id=job_id,
            )
            return None, None

        if response.status_code != requests.codes.ok:  # 200
            logger.warning(
                "muni: result for job %s returned %s", job_id, response.status_code
            )
            on_failure(
                "http_status",
                f"MUNI result returned HTTP {response.status_code}",
                phase="result", job_id=job_id, status_code=response.status_code,
                body_excerpt=_body_excerpt(response),
            )
            return None, None

        try:
            body = response.json()
        except ValueError as exc:
            logger.exception(
                "muni: result for job %s returned non-JSON body", job_id
            )
            on_failure(
                "non_json",
                f"non-JSON result body: {exc}",
                phase="result", job_id=job_id, body_excerpt=_body_excerpt(response),
            )
            return None, None

        if (body or {}).get("state") == "failed":
            error = (body or {}).get("error")
            logger.warning(
                "muni: job %s result reported failed (%s)", job_id, error
            )
            on_failure(
                "job_failed",
                f"MUNI job failed: {error}",
                phase="result", job_id=job_id, error=error,
            )
            return None, None

        return (body or {}).get("words") or [], None

    def run_attempt(
        self,
        payload: dict,
        *,
        attempt: int,
        total_attempts: int,
        deadline_seconds: float,
        check_abort: AbortHook = _noop_abort,
        on_failure: FailureHook = _noop_failure,
    ) -> list | None:
        """Submit one MUNI job and poll it to a terminal state.

        Returns a list of raw word dicts on success (possibly empty), or None
        on any failure (submission error, MUNI-side failure, expiry, or this
        attempt's deadline). ``check_abort`` runs before the submit and on
        every polling round; whatever it raises propagates.
        """
        check_abort(f"before attempt {attempt}/{total_attempts}")

        job_id = self._submit(
            payload, attempt=attempt, total_attempts=total_attempts,
            on_failure=on_failure,
        )
        if job_id is None:
            return None
        logger.info(
            "muni: attempt %d/%d submitted MUNI job %s",
            attempt, total_attempts, job_id,
        )

        # Per-attempt deadline: this one attempt gets its own wall-clock budget.
        started = time.monotonic()
        deadline = started + deadline_seconds
        last_status = None
        last_logged_at = started
        while True:
            # A single attempt may poll for up to the whole deadline, so
            # re-check the abort each round rather than only between attempts.
            check_abort(f"while polling MUNI job {job_id}")

            if time.monotonic() >= deadline:
                logger.warning(
                    "muni: job %s exceeded %ss deadline", job_id, deadline_seconds
                )
                on_failure(
                    "deadline_exceeded",
                    f"MUNI job did not finish within {deadline_seconds}s",
                    phase="poll", job_id=job_id,
                )
                return None

            outcome, retry_after, status_text = self._poll_state(
                job_id, on_failure=on_failure
            )
            # Narrate the poll: on every state/progress change, plus a heartbeat
            # so a long-stuck job still shows it's alive rather than going silent.
            now = time.monotonic()
            if status_text != last_status or (now - last_logged_at) >= 60:
                logger.info(
                    "muni: attempt %d/%d job %s: %s (%.0fs elapsed)",
                    attempt, total_attempts, job_id, status_text, now - started,
                )
                last_status = status_text
                last_logged_at = now

            if outcome == "finished":
                result, result_retry_after = self._fetch_result(
                    job_id, on_failure=on_failure
                )
                if not isinstance(result, _ResultNotReady):
                    logger.info(
                        "muni: attempt %d/%d job %s finished with %d words",
                        attempt, total_attempts, job_id,
                        len(result) if result else 0,
                    )
                    return result  # list (possibly empty) or None
                # Result not materialised yet — prefer MUNI's own Retry-After hint.
                wait = result_retry_after or self._poll_interval
            elif outcome in ("failed", "gone"):
                return None
            else:  # pending
                wait = retry_after or self._poll_interval

            # Sleep before the next poll, never past this attempt's deadline.
            time.sleep(max(0.0, min(wait, deadline - time.monotonic())))

    def probe(self) -> ProbeResult:
        """One health-check submit handshake.

        A healthy submit returns 202 (and enqueues a real job that MUNI
        auto-expires within 7 days). Only the handshake is checked, not the
        eventual analysis result.
        """
        payload = {
            "language": "en",
            "document_id": str(uuid.uuid4()),
            "paragraphs": [
                {
                    "id": str(uuid.uuid4()),
                    "content": "This is a short health-check paragraph.",
                }
            ],
        }
        started = time.monotonic()
        try:
            response = requests.post(
                self.submit_url, json=payload, timeout=self._submit_timeout
            )
        except requests.RequestException as exc:
            return ProbeResult(
                is_up=False,
                status_code=None,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=str(exc),
            )
        return ProbeResult(
            is_up=response.status_code == requests.codes.accepted,
            status_code=response.status_code,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=None,
        )


@dataclass
class FakeAttempt:
    """Scripted outcome for one ``run_attempt`` call.

    ``words=None`` means the attempt fails; ``failures`` are (kind, summary)
    events emitted through ``on_failure``; ``before`` runs first (after the
    payload is recorded, before the abort check) — use it to mutate state
    mid-run, e.g. deleting the job to exercise the abort path.
    """

    words: list[dict] | None = None
    failures: list[tuple[str, str]] = field(default_factory=list)
    before: Callable[[], None] | None = None


class FakeMuniClient:
    """In-memory adapter: replays scripted attempts, records payloads.

    Plans apply in call order; the last plan repeats once exhausted (mirroring
    the JobPlan convention in the contract tests). With no plans, every
    attempt fails without events.
    """

    def __init__(
        self,
        attempts: list[FakeAttempt] | None = None,
        probe_result: ProbeResult | None = None,
    ):
        self._attempts = list(attempts or [])
        self._probe_result = probe_result or ProbeResult(
            is_up=True, status_code=202, latency_ms=1, error=None
        )
        self.payloads: list[dict] = []
        self.attempts_run = 0

    @property
    def submit_url(self) -> str:
        return "fake://muni/document"

    def _plan_for(self, index: int) -> FakeAttempt:
        if not self._attempts:
            return FakeAttempt()
        return self._attempts[min(index, len(self._attempts) - 1)]

    def run_attempt(
        self,
        payload: dict,
        *,
        attempt: int,
        total_attempts: int,
        deadline_seconds: float,
        check_abort: AbortHook = _noop_abort,
        on_failure: FailureHook = _noop_failure,
    ) -> list | None:
        plan = self._plan_for(self.attempts_run)
        self.attempts_run += 1
        self.payloads.append(payload)
        if plan.before is not None:
            plan.before()
        check_abort(f"before attempt {attempt}/{total_attempts}")
        for kind, summary in plan.failures:
            on_failure(kind, summary, phase="fake")
        return list(plan.words) if plan.words is not None else None

    def probe(self) -> ProbeResult:
        return self._probe_result


def get_muni_client() -> HttpMuniClient:
    """The client the app talks to. Tests monkeypatch this to inject a fake."""
    return HttpMuniClient()
