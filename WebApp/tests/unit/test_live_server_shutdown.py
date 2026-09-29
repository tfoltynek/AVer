"""The e2e live server ends its idle handlers and waits for its request threads before it closes.

pytest-django stops the session-wide live server with ``terminate()`` and then
revokes the in-memory SQLite connection's thread sharing. Django's stock
``ThreadedWSGIServer`` runs request handlers as daemon threads that
``server_close()`` does not wait for, so a handler still in its cleanup after
the last test (``close_request`` → ``connections.close_all()``) lost that race
and died with "DatabaseWrapper objects created in a thread can only be used in
that same thread", surfacing as a ``PytestUnhandledThreadExceptionWarning``.
The handlers that linger are idle keep-alive connections held by Playwright's
driver, which outlives the live server, so merely joining them would hang.
"""

import http.client
import threading
import time

from django.test.testcases import QuietWSGIRequestHandler

from tests.e2e.live_server_support import JoiningWSGIServer


def _handler_threads(baseline):
    # Only the threads started since ``baseline``: when both suites run in one
    # session, pytest-django's session-wide e2e live server is still up and
    # holds idle keep-alive handlers of its own.
    return [t for t in threading.enumerate() if "process_request_thread" in t.name and t not in baseline]


def _serve(app):
    server = JoiningWSGIServer(("127.0.0.1", 0), QuietWSGIRequestHandler, allow_reuse_address=False)
    server.set_app(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _ok(start_response):
    start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
    return [b"ok"]


def test_server_close_waits_for_a_request_still_being_handled():
    entered = threading.Event()
    finished = threading.Event()

    def app(environ, start_response):
        entered.set()
        time.sleep(0.5)  # the handler is mid-flight when the server closes
        finished.set()
        return _ok(start_response)

    baseline = set(threading.enumerate())
    server = _serve(app)
    host, port = server.server_address

    def request():
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("GET", "/")
        connection.getresponse().read()

    threading.Thread(target=request, daemon=True).start()
    assert entered.wait(5), "the request never reached the handler"

    server.shutdown()
    server.server_close()

    assert finished.is_set(), "server_close() returned while a handler was still running"
    assert _handler_threads(baseline) == []


def test_server_close_ends_an_idle_keep_alive_connection():
    baseline = set(threading.enumerate())
    server = _serve(lambda environ, start_response: _ok(start_response))
    host, port = server.server_address

    # A Content-Length response on HTTP/1.1 keeps the connection open; the
    # client holds it, like Playwright's driver does, so the handler blocks
    # on the next request line.
    connection = http.client.HTTPConnection(host, port, timeout=5)
    connection.request("GET", "/")
    assert connection.getresponse().read() == b"ok"
    assert len(_handler_threads(baseline)) == 1, "expected one idle keep-alive handler"

    server.shutdown()
    started = time.monotonic()
    server.server_close()

    assert time.monotonic() - started < server.join_timeout, "server_close() waited for the join timeout"
    assert _handler_threads(baseline) == []
    assert connection.sock.recv(1) == b"", "the server did not close the idle connection"
