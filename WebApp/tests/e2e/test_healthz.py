"""E2E coverage for the /healthz liveness + readiness probe.

The endpoint is intentionally public (load balancers do not authenticate)
and returns JSON. Two flavours:

  - GET /healthz       → 200 with status=ok (no DB hit)
  - GET /healthz?deep  → 200 with status=ok + db=ok, or 503 if the DB is down
"""

import json

import pytest


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_healthz_returns_ok_without_auth(live_server, page):
    response = page.request.get(f"{live_server.url}/healthz")
    assert response.status == 200
    body = json.loads(response.text())
    assert body["status"] == "ok"
    assert body["checks"]["process"] == "ok"
    assert "db" not in body["checks"], "liveness probe must not hit the DB"


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_healthz_deep_probes_the_database(live_server, page):
    response = page.request.get(f"{live_server.url}/healthz?deep")
    assert response.status == 200
    body = json.loads(response.text())
    assert body["status"] == "ok"
    assert body["checks"] == {"process": "ok", "db": "ok"}


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_healthz_deep_reports_503_on_db_failure(live_server, page, monkeypatch):
    """When the cursor can't be opened, the deep probe must surface 503 so
    a load balancer pulls the instance out of rotation."""
    from core.views import pages as pages_module

    class _BrokenConnection:
        def cursor(self):
            raise RuntimeError("simulated db outage")

    monkeypatch.setattr(pages_module, "connection", _BrokenConnection())

    response = page.request.get(f"{live_server.url}/healthz?deep")
    assert response.status == 503
    body = json.loads(response.text())
    assert body["status"] == "degraded"
    assert body["checks"]["db"].startswith("error:")
