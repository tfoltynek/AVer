"""Duration bookkeeping for AnalysisJob.

The async MUNI flow makes analyses take hours (queue wait dominates), so the
job row records when the worker actually started and finished. See
docs/superpowers/specs/2026-07-22-analysis-duration-design.md.
"""

from datetime import UTC, datetime, timedelta

from core.models import AnalysisJob


def test_duration_requires_both_timestamps():
    job = AnalysisJob()  # unsaved; no DB access needed
    assert job.duration is None

    job.started_at = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    assert job.duration is None

    job.finished_at = datetime(2026, 7, 22, 12, 30, tzinfo=UTC)
    assert job.duration == timedelta(hours=2, minutes=30)
