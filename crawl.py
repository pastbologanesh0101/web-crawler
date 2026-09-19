#!/usr/bin/env python3
"""crawl.py -- a polite, single-domain BFS web crawler and sitemap builder.

Features
--------
* Respects robots.txt (disallow rules and Crawl-delay) via
  ``urllib.robotparser``.
* Breadth-first crawl restricted to the starting domain; links that point
  to other domains are recorded as "external links" but never fetched.
* Configurable ``--max-depth`` and ``--max-pages`` limits, plus a polite
  delay between requests.
* HTML link extraction via ``html.parser`` (no third-party dependencies).
* Builds a page -> outgoing-links graph and detects broken links (HTTP
  error status or connection failure), reporting which page(s) link to
  each broken URL.

Only the Python standard library is used, so the crawler works out of the
box against any HTTP(S) site -- point it at a real URL, or at the bundled
``testsite/`` for a safe, fully local demo.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from typing import Dict, List, Optional, Set

DEFAULT_USER_AGENT = "SiteMapperBot/1.0 (+https://github.com/pastbologanesh0101/web-crawler)"
DEFAULT_TIMEOUT = 10.0
DEFAULT_DELAY = 0.5
DEFAULT_MAX_PAGES = 100

_IGNORED_SCHEMES = ("mailto:", "javascript:", "tel:", "data:")


class LinkExtractor(HTMLParser):
    """Collects the ``href`` attribute of every ``<a>`` tag in an HTML doc."""

    def __init__(self) -> None:
        super().__init__()
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.links.append(value.strip())


def normalize_url(url: str) -> str:
    """Normalize a URL for use as a dictionary/set key.

    Drops the fragment, lower-cases scheme/host, strips default ports, and
    ensures an empty path is represented as ``/``.
    """
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[: -len(":80")]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[: -len(":443")]
    path = parts.path or "/"
    return urllib.parse.urlunsplit((scheme, netloc, path, parts.query, ""))


def same_domain(url_a: str, url_b: str) -> bool:
    """True if both URLs share the same scheme+host+port (netloc)."""
    a = urllib.parse.urlsplit(url_a)
    b = urllib.parse.urlsplit(url_b)
    return a.netloc.lower() == b.netloc.lower()


@dataclasses.dataclass
class FetchResult:
    url: str
    status: Optional[int]
    ok: bool
    html: str = ""
    error: str = ""


def fetch(url: str, user_agent: str, timeout: float = DEFAULT_TIMEOUT) -> FetchResult:
    """Fetch ``url`` and return a :class:`FetchResult`.

    Never raises: HTTP errors, connection failures, timeouts, and redirect
    loops are all caught and reported as a non-``ok`` result instead of
    propagating an exception (which matters for --  e.g. -- redirect loops,
    where ``urllib`` raises ``HTTPError`` once it gives up following
    redirects).
    """
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            content_type = resp.headers.get_content_type() or ""
            is_html = "html" in content_type or text.lstrip().lower().startswith("<")
            return FetchResult(url=url, status=status, ok=True, html=text if is_html else "")
    except urllib.error.HTTPError as exc:
        return FetchResult(url=url, status=exc.code, ok=False, error=str(exc))
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return FetchResult(url=url, status=None, ok=False, error=str(exc))


class RobotsGate:
    """Wraps ``urllib.robotparser`` for a single domain."""

    def __init__(self, start_url: str, user_agent: str) -> None:
        parts = urllib.parse.urlsplit(start_url)
        self.robots_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        self.user_agent = user_agent
        self.crawl_delay: Optional[float] = None
        self._parser: Optional[urllib.robotparser.RobotFileParser] = None
        self._load()

    def _load(self) -> None:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(self.robots_url)
        try:
            parser.read()
        except Exception:
            # If robots.txt is unreachable/unparsable, default to allowing
            # everything (matches common crawler behaviour).
            self._parser = None
            return
        self._parser = parser
        try:
            delay = parser.crawl_delay(self.user_agent)
        except Exception:
            delay = None
        if delay:
            self.crawl_delay = float(delay)

    def can_fetch(self, url: str) -> bool:
        if self._parser is None:
            return True
        try:
            return self._parser.can_fetch(self.user_agent, url)
        except Exception:
            return True


@dataclasses.dataclass
class PageRecord:
    url: str
    status: Optional[int]
    depth: int
    links: List[str] = dataclasses.field(default_factory=list)


class SiteMapper:
    """BFS crawler that builds a sitemap graph for a single domain."""

    def __init__(
        self,
        start_url: str,
        max_depth: Optional[int] = None,
        max_pages: int = DEFAULT_MAX_PAGES,
        delay: float = DEFAULT_DELAY,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.start_url = normalize_url(start_url)
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.user_agent = user_agent
        self.timeout = timeout

        self.robots = RobotsGate(self.start_url, user_agent)
        self.delay = max(delay, self.robots.crawl_delay or 0.0)

        self.visited: Set[str] = set()
        self.pages: Dict[str, PageRecord] = {}
        self.external_links: Set[str] = set()
        self.referrers: Dict[str, Set[str]] = collections.defaultdict(set)
        self.broken_links: Dict[str, Set[str]] = collections.defaultdict(set)
        self.skipped_by_robots: Set[str] = set()

    def crawl(self) -> dict:
        queue: collections.deque = collections.deque()
        queue.append((self.start_url, 0))
        self.visited.add(self.start_url)

        made_a_request = False
        while queue and len(self.pages) < self.max_pages:
            url, depth = queue.popleft()

            if self.max_depth is not None and depth > self.max_depth:
                continue

            if not self.robots.can_fetch(url):
                self.skipped_by_robots.add(url)
                continue

            if made_a_request and self.delay > 0:
                time.sleep(self.delay)
            made_a_request = True

            result = fetch(url, self.user_agent, self.timeout)

            if not result.ok:
                self.pages[url] = PageRecord(url=url, status=result.status, depth=depth)
                for ref in self.referrers.get(url, ()):  # who linked to this broken page
                    self.broken_links[url].add(ref)
                continue

            outgoing: List[str] = []
            if result.html:
                extractor = LinkExtractor()
                try:
                    extractor.feed(result.html)
                except Exception:
                    pass
                for href in extractor.links:
                    if href.startswith("#") or href.lower().startswith(_IGNORED_SCHEMES):
                        continue
                    absolute = urllib.parse.urljoin(url, href)
                    absolute = normalize_url(absolute)
                    if not absolute.startswith(("http://", "https://")):
                        continue

                    outgoing.append(absolute)
                    self.referrers[absolute].add(url)

                    if same_domain(absolute, self.start_url):
                        if absolute not in self.visited:
                            self.visited.add(absolute)
                            queue.append((absolute, depth + 1))
                    else:
                        self.external_links.add(absolute)

            self.pages[url] = PageRecord(url=url, status=result.status, depth=depth, links=outgoing)

        return self.report()

    def report(self) -> dict:
        return {
            "start_url": self.start_url,
            "pages_crawled": len(self.pages),
            "pages": {
                url: {"status": rec.status, "depth": rec.depth, "links": rec.links}
                for url, rec in self.pages.items()
            },
            "external_links": sorted(self.external_links),
            "broken_links": {url: sorted(refs) for url, refs in self.broken_links.items()},
            "skipped_by_robots": sorted(self.skipped_by_robots),
        }


def print_report(result: dict) -> None:
    print(f"Sitemap report for {result['start_url']}")
    print(f"  Pages crawled:                {result['pages_crawled']}")
    print(f"  External domains linked to:   {len(result['external_links'])}")
    print(f"  Skipped (robots.txt disallow):{len(result['skipped_by_robots']):>3}")
    print(f"  Broken links found:           {len(result['broken_links'])}")

    if result["broken_links"]:
        print("\nBroken links:")
        for url, referrers in sorted(result["broken_links"].items()):
            print(f"  {url}")
            for ref in referrers:
                print(f"    linked from: {ref}")

    if result["external_links"]:
        print("\nExternal links found (not followed):")
        for url in result["external_links"]:
            print(f"  {url}")

    if result["skipped_by_robots"]:
        print("\nSkipped due to robots.txt:")
        for url in result["skipped_by_robots"]:
            print(f"  {url}")

    print("\nPages (status, depth, outgoing link count):")
    for url, page in sorted(result["pages"].items()):
        print(f"  [{page['status']}] depth={page['depth']:<2} {url}  ({len(page['links'])} links)")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawl.py",
        description="Polite, robots.txt-respecting BFS web crawler and sitemap/broken-link reporter.",
    )
    parser.add_argument("url", help="Start URL, e.g. http://127.0.0.1:8000/ or https://example.com/")
    parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
        help="Maximum link depth to follow from the start URL (default: unlimited, bounded by --max-pages)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"Maximum number of pages to fetch (default: {DEFAULT_MAX_PAGES})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help=f"Base delay between requests in seconds; robots.txt Crawl-delay overrides it if larger (default: {DEFAULT_DELAY})",
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent header to send")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Per-request timeout in seconds")
    parser.add_argument("-o", "--output", help="Write the full JSON report to this file")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress the human-readable summary on stdout")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    mapper = SiteMapper(
        args.url,
        max_depth=args.max_depth,
        max_pages=args.max_pages,
        delay=args.delay,
        user_agent=args.user_agent,
        timeout=args.timeout,
    )
    result = mapper.crawl()

    if not args.quiet:
        print_report(result)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
        if not args.quiet:
            print(f"\nFull JSON report written to {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
