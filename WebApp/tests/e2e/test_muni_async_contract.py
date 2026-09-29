"""Contract coverage for `HttpMuniClient` against a real HTTP server.

A local server routes on path and replays the contract observed live on
2026-07-20, recorded in
`docs/superpowers/specs/2026-07-20-muni-async-api-contract.md`:

    POST /document         -> 202 {"job_id", "state": "queued", ...}
    GET  /jobs/<id>        -> 200 {"state", "error", ...}, 404 once expired
    GET  /jobs/<id>/result -> 409 + Retry-After while pending, then 200 {"words"}

Notably it asserts the *paths* the client hits, which lambda-style mocks
structurally cannot check. Protocol-only tests drive `client.run_attempt`
directly (no Django, no browser); pipeline tests run `start_analysis` on an
ORM-created document to pin persistence and job status.
"""

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core.muni import HttpMuniClient

# MUNI serves Bottle's HTML error page for 404s rather than JSON, so the client
# must not try to parse those bodies. Mirror that here.
_HTML_404 = b"<!DOCTYPE HTML PUBLIC><html><head><title>Error: 404</title></head></html>"


@dataclass
class JobPlan:
    """Scripted behaviour for one submitted job, applied in submit order."""

    # Served one per status poll; the final entry repeats once exhausted.
    states: list[str] = field(default_factory=lambda: ["finished"])
    # Number of 409s GET /result answers before serving the payload.
    result_conflicts: int = 0
    retry_after: str | None = None
    words: list[dict] = field(default_factory=list)
    error: str | None = None
    # Status polls served before the job starts 404ing (None = never expires).
    expire_after: int | None = None
    # Return 202 with no job_id at all, exercising bad_submit_response.
    submit_without_job_id: bool = False
    # Non-200 the status endpoint answers with (e.g. a proxy's 503), with
    # `retry_after` attached when set. 200 = normal state serving.
    status_http: int = 200


class _MuniHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass

    @property
    def _state(self):
        return self.server.muni

    def _send_json(self, code: int, body: dict, headers: dict | None = None):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _send_html_404(self):
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=UTF-8")
        self.send_header("Content-Length", str(len(_HTML_404)))
        self.end_headers()
        self.wfile.write(_HTML_404)

    def do_POST(self):
        state = self._state
        state.paths.append(("POST", self.path))
        if self.path != "/document":
            self._send_html_404()
            return

        length = int(self.headers.get("Content-Length") or 0)
        state.submitted_bodies.append(json.loads(self.rfile.read(length) or b"{}"))

        index = len(state.jobs)
        plan = state.plan_for(index)
        job_id = f"job-{index + 1}"
        state.jobs[job_id] = {"plan": plan, "status_polls": 0, "conflicts": 0}
        if plan.submit_without_job_id:
            self._send_json(202, {"state": "queued"})
            return
        state.in_flight += 1
        state.max_in_flight = max(state.max_in_flight, state.in_flight)
        self._send_json(
            202,
            {"job_id": job_id, "state": "queued", "status_url": f"/jobs/{job_id}"},
        )

    def do_GET(self):
        state = self._state
        state.paths.append(("GET", self.path))
        parts = self.path.strip("/").split("/")

        if len(parts) == 2 and parts[0] == "jobs":
            self._serve_status(parts[1])
        elif len(parts) == 3 and parts[0] == "jobs" and parts[2] == "result":
            self._serve_result(parts[1])
        else:
            self._send_html_404()

    def _serve_status(self, job_id):
        job = self._state.jobs.get(job_id)
        if job is None:
            self._send_html_404()
            return
        plan = job["plan"]
        if plan.expire_after is not None and job["status_polls"] >= plan.expire_after:
            self._send_html_404()
            return
        if plan.status_http != 200:
            job["status_polls"] += 1
            headers = {}
            if plan.retry_after is not None:
                headers["Retry-After"] = plan.retry_after
            self._send_json(plan.status_http, {}, headers)
            return
        index = min(job["status_polls"], len(plan.states) - 1)
        job["status_polls"] += 1
        self._send_json(
            200,
            {
                "job_id": job_id,
                "state": plan.states[index],
                "progress": {"done": 0, "total": 0},
                "error": plan.error,
            },
        )

    def _serve_result(self, job_id):
        job = self._state.jobs.get(job_id)
        if job is None:
            self._send_html_404()
            return
        plan = job["plan"]
        if job["conflicts"] < plan.result_conflicts:
            job["conflicts"] += 1
            headers = {}
            if plan.retry_after is not None:
                headers["Retry-After"] = plan.retry_after
            self._send_json(
                409,
                {"job_id": job_id, "state": "queued", "message": "Result not ready."},
                headers,
            )
            return
        if not job.get("done"):
            job["done"] = True
            self._state.in_flight -= 1
        self._send_json(200, {"job_id": job_id, "words": plan.words})


class _MuniState:
    def __init__(self, plans):
        self._plans = plans
        self.jobs = {}
        self.paths = []
        self.submitted_bodies = []
        # Concurrency tracking: a job counts as "in flight" from the moment it's
        # submitted until the client fetches its result (200). max_in_flight == 1
        # means the client ran the jobs strictly one at a time.
        self.in_flight = 0
        self.max_in_flight = 0
        self.client: HttpMuniClient | None = None

    def plan_for(self, index):
        if not self._plans:
            return JobPlan()
        return self._plans[min(index, len(self._plans) - 1)]


@pytest.fixture
def fake_muni(monkeypatch):
    """Start a local MUNI stand-in and build a client pointed at it.

    Yields a factory: call it with a list of `JobPlan`s (applied in submit
    order, the last repeating) and it returns the server state, whose
    ``.client`` is an `HttpMuniClient` on the local base URL. The factory also
    monkeypatches `core.tasks.get_muni_client` so pipeline tests that go
    through `start_analysis` use the same client.
    """
    import core.tasks as tasks_module

    servers = []

    def start(plans=None):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _MuniHandler)
        server.muni = _MuniState(plans or [])
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        host, port = server.server_address
        client = HttpMuniClient(base_url=f"http://{host}:{port}", poll_interval=0)
        server.muni.client = client
        monkeypatch.setattr(tasks_module, "get_muni_client", lambda: client)
        return server.muni

    yield start

    for server in servers:
        server.shutdown()
        server.server_close()


def _word_for(paragraph):
    return {
        "paragraph_id": paragraph.pk,
        "content": "test word",
        "predictions": None,
        "sentence_blanked": "this is a <<BLANK>>.",
        "sentence_index": 0,
        "index": 4,
    }


def _payload():
    """A minimal submit payload for protocol-only tests (no Django objects)."""
    return {
        "language": "en",
        "document_id": "doc-1",
        "paragraphs": [{"id": "p-1", "content": "A paragraph."}],
    }


# ---------------------------------------------------------------------------
# Protocol-only tests: drive the client directly, no Django models, no DB.
# ---------------------------------------------------------------------------


def test_run_logs_visible_progress(fake_muni, monkeypatch):
    """The log must narrate the run: the attempt announces the MUNI job it
    submitted, and polling surfaces MUNI's reported state — not just a single
    line at the very end after hours of silence."""
    import core.muni as muni_module

    muni = fake_muni([
        JobPlan(states=["queued", "processing", "finished"], words=[])
    ])

    infos = []

    def capture(msg, *args):
        try:
            infos.append(msg % args if args else str(msg))
        except Exception:
            infos.append(str(msg))

    monkeypatch.setattr(muni_module.logger, "info", capture)

    muni.client.run_attempt(
        _payload(), attempt=1, total_attempts=1, deadline_seconds=30
    )

    assert sum("submitted MUNI job" in m for m in infos) == 1, (
        "the attempt should announce its submit; got:\n" + "\n".join(infos)
    )
    assert any("processing" in m for m in infos), (
        "polling should log MUNI's reported state; got:\n" + "\n".join(infos)
    )


def test_result_retry_after_is_honoured(fake_muni):
    """MUNI sends `Retry-After` on its 409s. The client must wait that long
    rather than falling back to its own (zeroed) poll interval."""
    import time

    muni = fake_muni([
        JobPlan(
            states=["finished"],
            result_conflicts=1,
            retry_after="1",
            words=[{"content": "test word"}],
        )
    ])

    started = time.monotonic()
    words = muni.client.run_attempt(
        _payload(), attempt=1, total_attempts=1, deadline_seconds=30
    )
    elapsed = time.monotonic() - started

    assert words == [{"content": "test word"}]
    # Poll interval is 0, so any wait at all came from Retry-After.
    assert elapsed >= 1.0, f"Retry-After ignored; run took only {elapsed:.2f}s"


def test_html_404_on_status_is_not_parsed_as_json(fake_muni):
    """MUNI answers unknown/expired jobs with an HTML body. The client must
    read the status code first and emit `expired`, never `non_json`."""
    muni = fake_muni([JobPlan(states=["queued"], expire_after=0)])

    events = []
    words = muni.client.run_attempt(
        _payload(), attempt=1, total_attempts=1, deadline_seconds=30,
        on_failure=lambda kind, summary, **details: events.append(kind),
    )

    assert words is None
    assert "expired" in events, f"expected 'expired', got {events}"
    assert "non_json" not in events, "HTML 404 body was parsed as JSON"


def test_huge_retry_after_still_hits_attempt_deadline(fake_muni):
    """A pathological `Retry-After` (e.g. from a proxy) must not push the next
    poll past the attempt deadline — the attempt ends with deadline_exceeded
    instead of spinning until Celery kills the task."""
    muni = fake_muni([
        JobPlan(status_http=503, retry_after="999999")
    ])

    events = []
    words = muni.client.run_attempt(
        _payload(), attempt=1, total_attempts=1, deadline_seconds=0.2,
        on_failure=lambda kind, summary, **details: events.append(kind),
    )

    assert words is None
    assert "deadline_exceeded" in events, f"expected deadline_exceeded, got {events}"


def test_abort_check_runs_every_poll_round(fake_muni):
    """`check_abort` must run on every polling round, so a deleted job stops
    the run promptly instead of polling to the deadline."""

    class _Stop(Exception):
        pass

    muni = fake_muni([JobPlan(states=["queued"])])

    calls = {"n": 0}

    def check_abort(where=""):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise _Stop(where)

    with pytest.raises(_Stop):
        muni.client.run_attempt(
            _payload(), attempt=1, total_attempts=1, deadline_seconds=30,
            check_abort=check_abort,
        )

    status_polls = [p for p in muni.paths if p[0] == "GET" and "result" not in p[1]]
    assert len(status_polls) < 50, (
        f"expected an early abort, polled {len(status_polls)} times"
    )


# ---------------------------------------------------------------------------
# Pipeline tests: start_analysis on an ORM-created document, still no browser.
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_happy_path_hits_the_documented_routes_and_persists_words(
    pending_ai_document, fake_muni
):
    """The full pipeline against a real server: submit, poll through
    queued/processing/finished, absorb a 409, fetch the result, persist words."""
    import core.tasks as tasks_module
    from core.models import Paragraph, Word
    from core.tasks import start_analysis

    document = pending_ai_document
    paragraph = Paragraph.objects.filter(document=document).first()

    muni = fake_muni([
        JobPlan(
            states=["queued", "processing", "finished"],
            result_conflicts=1,
            retry_after="0",
            words=[_word_for(paragraph)],
        )
    ])

    start_analysis(document.current_analysis.pk)
    document.refresh_from_db()

    assert document.current_analysis.status == "completed", (
        f"expected 'completed', got {document.current_analysis.status!r}"
    )
    assert Word.objects.filter(paragraph__document=document).count() > 0

    job = document.current_analysis
    assert job.started_at is not None, "running transition should stamp started_at"
    assert job.finished_at is not None, "completion should stamp finished_at"
    assert job.finished_at >= job.started_at
    assert job.duration is not None

    # The URLs themselves are the point: lambda-style mocks would pass even if
    # the client's URL building were malformed.
    assert ("POST", "/document") in muni.paths
    assert ("GET", "/jobs/job-1") in muni.paths
    assert ("GET", "/jobs/job-1/result") in muni.paths
    assert len(muni.submitted_bodies) == tasks_module.MUNI_API_CALLS
    body = muni.submitted_bodies[0]
    assert body["document_id"] == str(document.pk)
    assert body["language"] == document.language.code
    assert body["paragraphs"], "submit body carried no paragraphs"


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_muni_jobs_run_strictly_one_at_a_time(
    pending_ai_document, fake_muni, monkeypatch
):
    """Attempt N+1 must not be submitted until attempt N has finished: at most
    one MUNI job may be outstanding at any moment (strict sequential)."""
    import core.tasks as tasks_module
    from core.models import Paragraph
    from core.tasks import start_analysis

    # Sequencing only shows with several attempts; production default is 1.
    monkeypatch.setattr(tasks_module, "MUNI_API_CALLS", 5)

    document = pending_ai_document
    paragraph = Paragraph.objects.filter(document=document).first()

    # Every attempt finishes on its first poll and returns a word.
    muni = fake_muni([JobPlan(states=["finished"], words=[_word_for(paragraph)])])

    start_analysis(document.current_analysis.pk)
    document.refresh_from_db()

    assert document.current_analysis.status == "completed"
    assert len(muni.submitted_bodies) == tasks_module.MUNI_API_CALLS
    assert muni.max_in_flight == 1, (
        f"expected one MUNI job in flight at a time, saw {muni.max_in_flight}"
    )


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_mixed_outcomes_commit_the_surviving_job(
    pending_ai_document, fake_muni, monkeypatch
):
    """One finished job among a failure, an expiry and a bad submit still
    commits, and each failure lands under its own `kind`."""
    import core.tasks as tasks_module
    from core.models import AnalysisFailureLog, Paragraph, Word
    from core.tasks import start_analysis

    # Mixed outcomes need several attempts; production default is 1.
    monkeypatch.setattr(tasks_module, "MUNI_API_CALLS", 5)

    document = pending_ai_document
    paragraph = Paragraph.objects.filter(document=document).first()

    fake_muni([
        JobPlan(states=["finished"], words=[_word_for(paragraph)]),
        JobPlan(states=["failed"], error="simulated upstream failure"),
        JobPlan(states=["queued"], expire_after=1),
        JobPlan(submit_without_job_id=True),
        JobPlan(states=["failed"], error="simulated upstream failure"),
    ])

    start_analysis(document.current_analysis.pk)
    document.refresh_from_db()

    assert document.current_analysis.status == "completed"
    assert Word.objects.filter(paragraph__document=document).count() > 0

    kinds = set(
        AnalysisFailureLog.objects.filter(
            analysis_job=document.current_analysis
        ).values_list("kind", flat=True)
    )
    assert {"job_failed", "expired", "bad_submit_response"} <= kinds, (
        f"expected each failure kind to be logged, got {kinds}"
    )


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_finished_jobs_returning_no_words_fail_rather_than_complete_empty(
    pending_ai_document, fake_muni
):
    """Every job finishing with an empty `words` array must fail the analysis.

    Otherwise the run is marked 'completed' and the user is handed an empty
    cloze test, which is worse than an honest failure.
    """
    from core.models import Word
    from core.tasks import start_analysis

    document = pending_ai_document
    fake_muni([JobPlan(states=["finished"], words=[])])

    start_analysis(document.current_analysis.pk)
    document.refresh_from_db()

    assert document.current_analysis.status == "failed", (
        f"expected 'failed' for an all-empty result, got "
        f"{document.current_analysis.status!r}"
    )
    assert Word.objects.filter(paragraph__document=document).count() == 0
    assert document.current_analysis.finished_at is not None, (
        "a failed run must still stamp finished_at"
    )
