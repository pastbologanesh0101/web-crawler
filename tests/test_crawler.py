"""Real, end-to-end unit tests for the crawler.

Every test starts the bundled local test site (testsite/server.py) on an
ephemeral localhost port in a background thread, then crawls it over real
HTTP. No test ever contacts a real, third-party site.
"""

import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from crawl import SiteMapper, normalize_url
from testsite import server as testsite_server

REPO_ROOT = Path(__file__).resolve().parent.parent


class CrawlerTestCase(unittest.TestCase):
    """Base class: spins up a fresh local test site per test."""

    def setUp(self) -> None:
        self.server, self.thread = testsite_server.run_in_thread()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/"

    def tearDown(self) -> None:
        testsite_server.shutdown(self.server, self.thread)

    def url(self, path: str) -> str:
        return normalize_url(f"http://127.0.0.1:{self.server.server_port}{path}")


class TestCrawlDiscovery(CrawlerTestCase):
    def test_discovers_all_internal_pages(self) -> None:
        mapper = SiteMapper(self.base_url, max_pages=50, delay=0)
        result = mapper.crawl()

        expected_successful = {
            self.url("/"),
            self.url("/index.html"),
            self.url("/page2.html"),
            self.url("/page3.html"),
            self.url("/page4.html"),
            self.url("/page5.html"),
            self.url("/sub/page6.html"),
        }
        successful = {u for u, p in result["pages"].items() if p["status"] == 200}
        self.assertEqual(successful, expected_successful)

    def test_circular_links_terminate_and_do_not_duplicate_pages(self) -> None:
        # index<->page2/page3, page4<->page2, page5<->page3, page6<->page4/index
        # form several cycles; the visited-set must stop BFS from looping.
        mapper = SiteMapper(self.base_url, max_pages=50, delay=0)
        done = threading.Event()
        result_holder = {}

        def run():
            result_holder["result"] = mapper.crawl()
            done.set()

        t = threading.Thread(target=run)
        t.start()
        finished = done.wait(timeout=15)
        self.assertTrue(finished, "crawl() did not terminate within timeout -- possible infinite loop")

        result = result_holder["result"]
        # 7 successful pages + 1 broken (missing.html) = 8 total attempts;
        # secret.html is skipped via robots.txt, not fetched.
        self.assertEqual(result["pages_crawled"], 8)


class TestRobots(CrawlerTestCase):
    def test_robots_txt_is_fetched(self) -> None:
        mapper = SiteMapper(self.base_url, max_pages=50, delay=0)
        mapper.crawl()
        self.assertIn("/robots.txt", self.server.request_log)

    def test_disallowed_path_is_never_fetched(self) -> None:
        mapper = SiteMapper(self.base_url, max_pages=50, delay=0)
        result = mapper.crawl()

        secret_url = self.url("/secret.html")
        self.assertIn(secret_url, result["skipped_by_robots"])
        self.assertNotIn(secret_url, result["pages"])
        # Hard guarantee at the HTTP level: the server never saw the request.
        self.assertNotIn("/secret.html", self.server.request_log)


class TestBrokenLinks(CrawlerTestCase):
    def test_broken_link_detected_with_referrer(self) -> None:
        mapper = SiteMapper(self.base_url, max_pages=50, delay=0)
        result = mapper.crawl()

        missing_url = self.url("/missing.html")
        self.assertIn(missing_url, result["broken_links"])
        self.assertEqual(result["broken_links"][missing_url], [self.url("/page2.html")])
        self.assertEqual(result["pages"][missing_url]["status"], 404)


class TestExternalLinks(CrawlerTestCase):
    def test_external_links_recorded_but_not_followed(self) -> None:
        mapper = SiteMapper(self.base_url, max_pages=50, delay=0)
        result = mapper.crawl()

        self.assertIn("http://example.invalid/off-site", result["external_links"])
        self.assertNotIn("http://example.invalid/off-site", result["pages"])


class TestLimits(CrawlerTestCase):
    def test_max_pages_limit_is_respected(self) -> None:
        mapper = SiteMapper(self.base_url, max_pages=3, delay=0)
        result = mapper.crawl()
        self.assertLessEqual(result["pages_crawled"], 3)
        self.assertEqual(result["pages_crawled"], 3)  # the site has >3 reachable pages

    def test_max_depth_limit_is_respected(self) -> None:
        # depth 0 = start page only; nothing else should be fetched.
        mapper = SiteMapper(self.base_url, max_pages=50, max_depth=0, delay=0)
        result = mapper.crawl()
        self.assertEqual(result["pages_crawled"], 1)
        self.assertEqual(list(result["pages"].keys()), [self.url("/")])


class TestUrlNormalization(CrawlerTestCase):
    def test_bare_relative_links_normalize_correctly(self) -> None:
        mapper = SiteMapper(self.base_url, max_pages=50, delay=0)
        result = mapper.crawl()

        page2_links = result["pages"][self.url("/page2.html")]["links"]
        self.assertIn(self.url("/missing.html"), page2_links)

        page3_links = result["pages"][self.url("/page3.html")]["links"]
        self.assertIn(self.url("/page5.html"), page3_links)

    def test_dot_dot_relative_links_normalize_correctly(self) -> None:
        mapper = SiteMapper(self.base_url, max_pages=50, delay=0)
        result = mapper.crawl()

        page6_links = result["pages"][self.url("/sub/page6.html")]["links"]
        self.assertIn(self.url("/page4.html"), page6_links)
        self.assertIn(self.url("/index.html"), page6_links)
        # Must NOT be mis-resolved relative to /sub/.
        self.assertNotIn(self.url("/sub/page4.html"), page6_links)


class TestRedirectLoop(CrawlerTestCase):
    def test_redirect_loop_terminates_without_hanging(self) -> None:
        loop_url = f"http://127.0.0.1:{self.server.server_port}/loop-a.html"
        mapper = SiteMapper(loop_url, max_pages=10, delay=0)

        done = threading.Event()
        holder = {}

        def run():
            holder["result"] = mapper.crawl()
            done.set()

        t = threading.Thread(target=run)
        t.start()
        finished = done.wait(timeout=15)
        self.assertTrue(finished, "crawl() hung on a redirect loop instead of giving up")

        result = holder["result"]
        # The single page attempted should be recorded as a failure (urllib
        # gives up after too many redirects), not silently dropped.
        self.assertEqual(result["pages_crawled"], 1)
        (page,) = result["pages"].values()
        self.assertFalse(200 <= (page["status"] or 0) < 300)


class TestPoliteness(CrawlerTestCase):
    def test_delay_between_requests_is_honored(self) -> None:
        delay = 0.3
        mapper = SiteMapper(self.base_url, max_pages=3, delay=delay)
        start = time.monotonic()
        result = mapper.crawl()
        elapsed = time.monotonic() - start

        self.assertEqual(result["pages_crawled"], 3)
        # 3 pages => 2 gaps of at least `delay` seconds each.
        self.assertGreaterEqual(elapsed, delay * 2 * 0.8)


class TestCli(CrawlerTestCase):
    def test_cli_produces_json_report(self) -> None:
        output_path = REPO_ROOT / "tests" / "_cli_report.json"
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "crawl.py"),
                    self.base_url,
                    "--max-pages",
                    "50",
                    "--delay",
                    "0",
                    "--output",
                    str(output_path),
                    "--quiet",
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(output_path.exists())
            with open(output_path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("pages", data)
            self.assertIn("broken_links", data)
            self.assertGreaterEqual(data["pages_crawled"], 7)
        finally:
            if output_path.exists():
                output_path.unlink()


if __name__ == "__main__":
    unittest.main()
