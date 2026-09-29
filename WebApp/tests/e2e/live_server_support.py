"""The WSGI server class the e2e live server runs on.

Django's ``ThreadedWSGIServer`` handles each request in a daemon thread and
``server_close()`` does not wait for those threads. pytest-django stops its
session-wide live server with ``terminate()`` and immediately revokes the
shared in-memory SQLite connection's thread sharing, so a request thread that
finishes its cleanup (``close_request`` → ``connections.close_all()``) after
that finds the connection no longer shareable and dies with "DatabaseWrapper
objects created in a thread can only be used in that same thread".

The threads that outlive the server are idle keep-alive handlers. Playwright's
driver process pools the HTTP connections behind ``page.request.get(...)``,
and because the session browser is launched before any DB fixture (see
``_browser_before_db`` in conftest) it is torn down after the live server, so
those sockets stay open until then. A plain ``join()`` on the handler threads
therefore deadlocks the session: the handler waits for the driver, the driver
waits for pytest, pytest waits for the handler.

``server_close()`` here first shuts down the read side of every request socket
still open. The idle handler's ``readline()`` returns EOF, it leaves its
request loop and runs its cleanup while thread sharing is still on, and the
bounded join below sees it finish. Threads stay daemonic, so a handler stuck
anywhere else costs the timeout and the old warning, never a hang.
"""

import socket
import threading
import time

from django.core.servers.basehttp import ThreadedWSGIServer


class JoiningWSGIServer(ThreadedWSGIServer):
    """``ThreadedWSGIServer`` whose ``server_close()`` ends idle handlers and waits for them."""

    join_timeout = 10.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._handlers: dict[threading.Thread, socket.socket] = {}
        self._handlers_lock = threading.Lock()

    def process_request(self, request, client_address):
        thread = threading.Thread(
            target=self.process_request_thread,
            args=(request, client_address),
            daemon=self.daemon_threads,
        )
        with self._handlers_lock:
            self._handlers[thread] = request
        thread.start()

    def shutdown_request(self, request):
        # Runs in the handler thread once the request loop is over; the socket
        # leaves the table before it is closed, so server_close() never touches
        # a closed one.
        with self._handlers_lock:
            self._handlers.pop(threading.current_thread(), None)
        super().shutdown_request(request)

    def server_close(self):
        super().server_close()
        with self._handlers_lock:
            handlers = dict(self._handlers)
            for request in handlers.values():
                try:
                    request.shutdown(socket.SHUT_RD)
                except OSError:
                    pass  # the peer already closed it; the handler is about to see EOF
        deadline = time.monotonic() + self.join_timeout
        for thread in handlers:
            thread.join(max(0.0, deadline - time.monotonic()))
