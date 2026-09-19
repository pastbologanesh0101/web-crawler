#!/usr/bin/env python3
"""A tiny, fully local test site for exercising the crawler.

Serves a handful of interlinked static HTML pages over plain HTTP on
127.0.0.1, purely from Python's standard library (``http.server``). Used
by both the automated tests and the manual demo -- it never talks to any
real, third-party site.

Site map (all paths are on this same local server unless noted):

    /  and /index.html  -- home page
        -> /page2.html
        -> /page3.html
        -> /secret.html            (disallowed by /robots.txt)
        -> http://example.invalid/off-site   (external, never followed)

    /page2.html
        -> /index.html
        -> /page4.html
        -> missing.html            (bare relative -> /missing.html, 404: broken link)

    /page3.html
        -> page5.html              (bare relative -> /page5.html)
        -> /index.html

    /page4.html
        -> /page2.html             (circular)
        -> /sub/page6.html

    /page5.html
        -> /page3.html             (circular)
        -> /sub/page6.html

    /sub/page6.html
        -> ../page4.html           (dot-dot relative -> /page4.html)
        -> ../index.html           (dot-dot relative -> /index.html)

    /secret.html                   (never fetched: disallowed by robots.txt)
        -> /index.html

    /loop-a.html  <-> /loop-b.html  (a genuine HTTP 302 redirect loop,
                                      used to prove the crawler doesn't hang)

    /robots.txt   -- disallows /secret.html
"""

from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Tuple

INDEX_HTML = """\
<!DOCTYPE html>
<html><head><title>Home</title></head>
<body>
<h1>Test Site Home</h1>
<ul>
  <li><a href="/page2.html">Page 2</a></li>
  <li><a href="/page3.html">Page 3</a></li>
  <li><a href="/secret.html">Secret (robots.txt disallowed)</a></li>
  <li><a href="http://example.invalid/off-site">An external site</a></li>
</ul>
</body></html>
"""

PAGE2_HTML = """\
<!DOCTYPE html>
<html><head><title>Page 2</title></head>
<body>
<h1>Page 2</h1>
<ul>
  <li><a href="/index.html">Home</a></li>
  <li><a href="/page4.html">Page 4</a></li>
  <li><a href="missing.html">A broken link (bare relative)</a></li>
</ul>
</body></html>
"""

PAGE3_HTML = """\
<!DOCTYPE html>
<html><head><title>Page 3</title></head>
<body>
<h1>Page 3</h1>
<ul>
  <li><a href="page5.html">Page 5 (bare relative)</a></li>
  <li><a href="/index.html">Home</a></li>
</ul>
</body></html>
"""

PAGE4_HTML = """\
<!DOCTYPE html>
<html><head><title>Page 4</title></head>
<body>
<h1>Page 4</h1>
<ul>
  <li><a href="/page2.html">Back to Page 2 (circular)</a></li>
  <li><a href="/sub/page6.html">Page 6</a></li>
</ul>
</body></html>
"""

PAGE5_HTML = """\
<!DOCTYPE html>
<html><head><title>Page 5</title></head>
<body>
<h1>Page 5</h1>
<ul>
  <li><a href="/page3.html">Back to Page 3 (circular)</a></li>
  <li><a href="/sub/page6.html">Page 6</a></li>
</ul>
</body></html>
"""

PAGE6_HTML = """\
<!DOCTYPE html>
<html><head><title>Page 6</title></head>
<body>
<h1>Page 6 (in /sub/)</h1>
<ul>
  <li><a href="../page4.html">Up to Page 4 (dot-dot relative)</a></li>
  <li><a href="../index.html">Home (dot-dot relative)</a></li>
</ul>
</body></html>
"""

SECRET_HTML = """\
<!DOCTYPE html>
<html><head><title>Secret</title></head>
<body>
<h1>You should never see this fetched by a polite crawler.</h1>
<a href="/index.html">Home</a>
</body></html>
"""

ROBOTS_TXT = "User-agent: *\nDisallow: /secret.html\n"

PAGES = {
    "/": INDEX_HTML,
    "/index.html": INDEX_HTML,
    "/page2.html": PAGE2_HTML,
    "/page3.html": PAGE3_HTML,
    "/page4.html": PAGE4_HTML,
    "/page5.html": PAGE5_HTML,
    "/sub/page6.html": PAGE6_HTML,
    "/secret.html": SECRET_HTML,
}

# A genuine HTTP redirect loop: /loop-a.html <-> /loop-b.html.
REDIRECTS = {
    "/loop-a.html": "/loop-b.html",
    "/loop-b.html": "/loop-a.html",
}


class TestSiteServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that keeps a per-instance log of requested paths.

    Tests use ``request_log`` to make hard assertions such as "the
    robots-disallowed path was never requested at the HTTP level", not just
    "it's absent from the crawler's report".
    """

    allow_reuse_address = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.request_log: list[str] = []


class TestSiteHandler(BaseHTTPRequestHandler):
    server: TestSiteServer  # type: ignore[assignment]

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # keep test output quiet

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        path = self.path.split("?", 1)[0]
        self.server.request_log.append(path)

        if path == "/robots.txt":
            self._respond(200, ROBOTS_TXT, "text/plain")
            return

        if path in REDIRECTS:
            self.send_response(302)
            self.send_header("Location", REDIRECTS[path])
            self.end_headers()
            return

        if path in PAGES:
            self._respond(200, PAGES[path], "text/html")
            return

        self._respond(404, "<html><body><h1>Not Found</h1></body></html>", "text/html")

    def _respond(self, status: int, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(host: str = "127.0.0.1", port: int = 0) -> TestSiteServer:
    """Create (but do not start) a TestSiteServer on an optionally-ephemeral port."""
    return TestSiteServer((host, port), TestSiteHandler)


def run_in_thread(host: str = "127.0.0.1", port: int = 0) -> Tuple[TestSiteServer, threading.Thread]:
    """Start the test site on a background daemon thread and return (server, thread).

    Callers are responsible for calling ``server.shutdown()`` followed by
    ``server.server_close()`` (and joining the thread) when done.
    """
    server = make_server(host, port)
    thread = threading.Thread(target=server.serve_forever, name="testsite-server", daemon=True)
    thread.start()
    return server, thread


def shutdown(server: TestSiteServer, thread: threading.Thread, timeout: float = 5.0) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=timeout)


if __name__ == "__main__":
    requested_port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    srv, th = run_in_thread(port=requested_port)
    print(f"Serving local test site at http://127.0.0.1:{srv.server_port}/  (Ctrl+C to stop)")
    try:
        th.join()
    except KeyboardInterrupt:
        print("\nShutting down...")
        shutdown(srv, th)
