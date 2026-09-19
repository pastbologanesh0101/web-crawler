# Web Crawler / Site Mapper

A small, dependency-free, polite BFS web crawler written in pure Python
(`urllib`, `http.client`, `html.parser`, `urllib.robotparser` — standard
library only). Point it at a domain and it will:

- Crawl that domain breadth-first, following only same-domain links.
- Respect `robots.txt` (disallow rules **and** `Crawl-delay`).
- Record links to other domains as "external" without ever fetching them.
- Build a sitemap graph (page → the links it points to).
- Detect broken links (HTTP errors / connection failures) and report
  exactly which page(s) link to each one.
- Stop cleanly at a configurable `--max-depth` and `--max-pages`, and
  never loop forever on circular links or HTTP redirect loops.

## How it works

| Concern | Implementation |
|---|---|
| Fetching | `urllib.request.urlopen`, with a `User-Agent` header and a timeout. HTTP errors, connection failures, and "too many redirects" (redirect loops) are all caught and turned into a failed-fetch result rather than an exception. |
| robots.txt | `urllib.robotparser.RobotFileParser`, loaded once per crawl from `http(s)://<domain>/robots.txt`. `Disallow` rules gate every fetch (including the start URL); `Crawl-delay`, if present, raises the effective delay between requests. If `robots.txt` can't be fetched at all, the crawler defaults to allowing everything, matching common crawler behavior. |
| Link extraction | A tiny `html.parser.HTMLParser` subclass collects every `<a href="...">`. Links are resolved against the *current page's* URL with `urllib.parse.urljoin`, so both bare-relative (`page.html`) and dot-dot-relative (`../page.html`) hrefs resolve correctly, then normalized (fragment stripped, scheme/host lower-cased, default port dropped). |
| BFS + domain scoping | A `collections.deque` plus a `visited` set of normalized URLs. A link is followed only if its `scheme://host:port` matches the start URL's; otherwise it's added to `external_links` and never fetched. The `visited` set is what makes circular links (A → B → A) and redirect loops terminate instead of hanging. |
| Sitemap graph | `SiteMapper.report()` returns `pages: {url: {status, depth, links}}` — an adjacency list of the crawl. |
| Broken links | While extracting links from a page, the crawler also records, for every link it sees, which page it came from (`referrers`). When a URL is later fetched and fails (4xx/5xx status or a connection error), its referrers become the `broken_links[url]` entry. |

## Project layout

```
crawl.py             the crawler + CLI (stdlib only)
testsite/            a tiny local HTTP test site used by the demo and tests
  server.py            8 static pages, robots.txt, a redirect loop, all served locally
tests/
  test_crawler.py      12 real, end-to-end unit tests against the local test site
requirements.txt     just `pytest`, for running the tests
```

## Running the bundled demo (100% local — nothing real is contacted)

In one terminal, start the local test site:

```bash
python3 testsite/server.py 8000
# Serving local test site at http://127.0.0.1:8000/  (Ctrl+C to stop)
```

In another terminal, crawl it:

```bash
python3 crawl.py http://127.0.0.1:8000/ --max-pages 50
```

Example output:

```
Sitemap report for http://127.0.0.1:8000/
  Pages crawled:                8
  External domains linked to:   1
  Skipped (robots.txt disallow):  1
  Broken links found:           1

Broken links:
  http://127.0.0.1:8000/missing.html
    linked from: http://127.0.0.1:8000/page2.html

External links found (not followed):
  http://example.invalid/off-site

Skipped due to robots.txt:
  http://127.0.0.1:8000/secret.html

Pages (status, depth, outgoing link count):
  [200] depth=0  http://127.0.0.1:8000/  (4 links)
  [200] depth=2  http://127.0.0.1:8000/index.html  (4 links)
  [404] depth=2  http://127.0.0.1:8000/missing.html  (0 links)
  [200] depth=1  http://127.0.0.1:8000/page2.html  (3 links)
  [200] depth=1  http://127.0.0.1:8000/page3.html  (2 links)
  [200] depth=2  http://127.0.0.1:8000/page4.html  (2 links)
  [200] depth=2  http://127.0.0.1:8000/page5.html  (2 links)
  [200] depth=3  http://127.0.0.1:8000/sub/page6.html  (2 links)
```

Add `-o report.json` to also save the full machine-readable report (the
same dict `SiteMapper.report()` returns), or `-q` to suppress the printed
summary and only write the file.

Remember to stop the test server (`Ctrl+C`, or kill the process) when
you're done.

## Pointing it at a real site

The crawler itself is completely generic — it isn't hard-wired to the
bundled test site. To crawl a real site you own or have permission to
crawl:

```bash
python3 crawl.py https://example.com --max-depth 3 --max-pages 200 --delay 1.0
```

- It will fetch and honor that site's `robots.txt` automatically.
- `--delay` sets the *minimum* polite delay; if the site's `robots.txt`
  specifies a larger `Crawl-delay`, that value wins.
- Only links on the same host (scheme + hostname + port) are followed;
  everything else is reported under `external_links`.
- Use `--max-depth` / `--max-pages` to bound the crawl, and `--user-agent`
  to identify your bot honestly.

**Please crawl responsibly**: only point this at sites you own or have
permission to crawl, keep `--delay` reasonable, and respect `robots.txt`
(which the crawler already does by default).

## Running the tests

```bash
pip install -r requirements.txt
pytest -v
```

All 12 tests start the bundled local test site on an ephemeral port in a
background thread and crawl it for real over HTTP (no mocking, no real
third-party network access), then shut the server down. They cover:

- discovering every internal page reachable within `--max-depth`
- never fetching the path disallowed by `robots.txt` (checked at the raw
  HTTP request-log level, not just in the report)
- detecting the deliberately broken link and reporting which page links to it
- recording external-domain links without following them
- `--max-pages` and `--max-depth` limits being respected
- relative URL normalization for both bare-relative (`page.html`) and
  dot-dot-relative (`../page.html`) links
- a genuine HTTP redirect loop and circular internal links both
  terminating instead of hanging
- polite delay between requests
- the CLI producing a valid JSON report

CI (`.github/workflows/tests.yml`) runs the same suite on Python 3.11 and
3.12 on every push and pull request.

## License

MIT — see [LICENSE](LICENSE).
