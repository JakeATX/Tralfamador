#!/usr/bin/env python3
"""Recover article-like pages from Wayback Machine catalog captures.

The script is intentionally conservative: it caches GET responses, sends a
descriptive User-Agent, retries transient failures, and keeps provenance in
JSONL manifests.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests


CDX_URL = "https://web.archive.org/cdx/search/cdx"
AVAILABILITY_URL = "https://archive.org/wayback/available"
DEFAULT_REQUEST_DELAY = 4.0
DEFAULT_USER_AGENT = (
    "tralfamador/0.1.1 "
    "(Wayback recovery research; set contact with --user-agent)"
)
DEFAULT_PREFIXES: list[str] = []
HREF_RE = re.compile(
    r"""<a\b[^>]*?\bhref\s*=\s*(?:"([^"]+)"|'([^']+)'|([^'"\s>]+))""",
    re.IGNORECASE,
)
DATE_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})")
DEFAULT_CANDIDATE_REGEX = r"https?://[^/]+/(?:[^/?#]+/)*[^/?#]*-[^/?#]*/?$"
DEFAULT_EXCLUDE_REGEX = (
    r"/(?:tag|tags|category|categories|author|authors|contributor|contributors|"
    r"about|about-us|contact|data|how-to-pitch[^/]*|jobs|masthead|newsletter|newsletters|"
    r"press|privacy|privacy-policy|search|sitemap|subscribe|terms|terms-of-use|page)(?:/|$)"
)
STATIC_ASSET_RE = re.compile(
    r"\.(?:css|js|json|xml|rss|atom|jpg|jpeg|png|gif|webp|svg|ico|pdf|zip|mp3|mp4|mov|m4v|webm)(?:[?#].*)?$",
    re.IGNORECASE,
)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def jsonl_append(path: Path, row: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class PoliteSession:
    def __init__(
        self,
        cache_dir: Path,
        user_agent: str = DEFAULT_USER_AGENT,
        delay: float = DEFAULT_REQUEST_DELAY,
        timeout: float = 45.0,
        retries: int = 5,
    ) -> None:
        self.cache_dir = cache_dir
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.last_request = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        ensure_dir(cache_dir)

    def get_text(self, url: str, use_cache: bool = True) -> tuple[str, dict[str, Any]]:
        key = cache_key(url)
        body_path = self.cache_dir / f"{key}.body"
        meta_path = self.cache_dir / f"{key}.json"
        if use_cache and body_path.exists() and meta_path.exists():
            return body_path.read_text(encoding="utf-8", errors="replace"), json.loads(
                meta_path.read_text(encoding="utf-8")
            )

        elapsed = time.monotonic() - self.last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)

        retries = max(1, self.retries)
        last_error: Exception | None = None
        for attempt in range(retries):
            self.last_request = time.monotonic()
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(max(2 ** attempt, self.delay))
                    continue
                raise
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                time.sleep(max(wait, self.delay))
                continue
            if response.status_code in {500, 502, 503, 504} and attempt < retries - 1:
                time.sleep(max(2 ** attempt, self.delay))
                continue
            response.raise_for_status()
            meta = {
                "url": url,
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "fetched_at": now_iso(),
            }
            body_path.write_text(response.text, encoding="utf-8", errors="replace")
            meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
            return response.text, meta
        if last_error:
            raise RuntimeError(f"request failed after retries: {url}: {last_error}") from last_error
        raise RuntimeError(f"request failed after retries: {url}")


def make_url(base_url: str, params: dict[str, Any]) -> str:
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, list):
            for item in value:
                pairs.append((key, str(item)))
        else:
            pairs.append((key, str(value)))
    return base_url + "?" + urlencode(pairs)


def make_cdx_url(params: dict[str, Any]) -> str:
    return make_url(CDX_URL, params)


def cdx_query(session: PoliteSession, params: dict[str, Any]) -> list[dict[str, str]]:
    query = {
        **params,
        "output": "json",
    }
    url = make_cdx_url(query)
    text, _ = session.get_text(url)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # CDX occasionally returns HTTP 200 with a truncated JSON body. Retry
        # once without cache so the bad partial response does not poison audits.
        text, _ = session.get_text(url, use_cache=False)
        data = json.loads(text)
    if not data:
        return []
    header = data[0]
    rows = []
    for values in data[1:]:
        if not values:
            continue
        if len(values) == 1 and isinstance(values[0], str) and "%" in values[0]:
            rows.append({"_resume_key": values[0]})
            continue
        rows.append(dict(zip(header, values)))
    return rows


def dedupe_cdx_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    deduped = []
    seen = set()
    for row in rows:
        key = (row.get("timestamp"), row.get("original"), row.get("digest"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def catalog_url_variants(root_url: str) -> list[str]:
    parsed = urlparse(root_url)
    if not parsed.netloc:
        raise ValueError(f"root URL must be absolute: {root_url}")
    host = canonical_host(parsed.netloc)
    paths = [parsed.path or "/"]
    if paths[0].endswith("/") and paths[0] != "/":
        paths.append(paths[0].rstrip("/"))
    elif paths[0] != "/":
        paths.append(paths[0] + "/")
    variants = []
    for scheme in ("https", "http"):
        for host_variant in (host, "www." + host):
            for path in paths:
                variants.append(urlunparse((scheme, host_variant, path, "", "", "")))
    return dedupe_preserve_order(variants)


def raw_wayback_url(timestamp: str, original: str) -> str:
    return f"https://web.archive.org/web/{timestamp}id_/{original}"


def canonical_host(host: str) -> str:
    host = host.lower()
    if host.startswith("www."):
        return host[4:]
    return host


def host_in_scope(candidate_host: str, root_host: str, include_subdomains: bool = False) -> bool:
    candidate_host = canonical_host(candidate_host)
    root_host = canonical_host(root_host)
    if candidate_host == root_host:
        return True
    return include_subdomains and candidate_host.endswith("." + root_host)


def normalize_url_for_root(
    url: str,
    root_url: str,
    include_subdomains: bool = False,
) -> str | None:
    url = html.unescape(url.strip())
    if not url or url.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        parsed = urlparse(urljoin(root_url, url))
    root = urlparse(root_url)
    host = canonical_host(parsed.netloc)
    if not host_in_scope(host, root.netloc, include_subdomains=include_subdomains):
        return None
    path = re.sub(r"/+", "/", parsed.path)
    if not path.startswith("/"):
        path = "/" + path
    if path.endswith("/amp/"):
        path = path[: -len("amp/")]
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith(("utm_", "cid", "ex_cid", "fbclid", "mc_cid", "mc_eid"))
        and k.lower() not in {"amp", "noamp", "share", "outputtype"}
    ]
    query = urlencode(query_pairs)
    scheme = root.scheme if root.scheme in {"http", "https"} else "https"
    return urlunparse((scheme, host, path, "", query, ""))


def strip_url_noise_for_root(
    url: str,
    root_url: str,
    include_subdomains: bool = False,
) -> str | None:
    normalized = normalize_url_for_root(url, root_url, include_subdomains=include_subdomains)
    if not normalized:
        return None
    parsed = urlparse(normalized)
    path = parsed.path
    if path != "/" and not path.endswith("/"):
        path += "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))


def is_generic_article_candidate(
    url: str,
    candidate_re: re.Pattern[str],
    exclude_re: re.Pattern[str] | None,
) -> bool:
    parsed = urlparse(url)
    path = parsed.path
    if not path or path == "/" or STATIC_ASSET_RE.search(path):
        return False
    if "/page/" in path or path.rstrip("/").endswith("/page"):
        return False
    if exclude_re and exclude_re.search(url):
        return False
    return bool(candidate_re.search(url))


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr = dict(attrs)
        href = attr.get("href")
        if href:
            self.links.append(href)


class ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.json_ld: list[str] = []
        self.capture_script = False
        self.script_type = ""
        self.script_buf: list[str] = []
        self.stack: list[str] = []
        self.article_depth = 0
        self.skip_depth = 0
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k.lower(): v or "" for k, v in attrs}
        self.stack.append(tag)
        if tag == "meta":
            key = attr.get("property") or attr.get("name")
            content = attr.get("content")
            if key and content:
                self.meta[key.lower()] = content
        elif tag == "script":
            self.script_type = attr.get("type", "").lower()
            if self.script_type == "application/ld+json":
                self.capture_script = True
                self.script_buf = []
        elif tag == "article":
            self.article_depth += 1
        elif tag in {"script", "style", "noscript", "nav", "header", "footer", "aside"}:
            self.skip_depth += 1
        elif tag == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.capture_script:
            self.json_ld.append("".join(self.script_buf))
            self.capture_script = False
        if tag == "article" and self.article_depth:
            self.article_depth -= 1
        if tag in {"script", "style", "noscript", "nav", "header", "footer", "aside"} and self.skip_depth:
            self.skip_depth -= 1
        if tag == "title":
            self.in_title = False
        if self.stack:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self.capture_script:
            self.script_buf.append(data)
            return
        cleaned = re.sub(r"\s+", " ", data).strip()
        if not cleaned:
            return
        if self.in_title:
            self.title_parts.append(cleaned)
        if self.article_depth and not self.skip_depth:
            self.text_parts.append(cleaned)


def extract_links(html_text: str, base_url: str) -> list[str]:
    return extract_links_for_root(html_text, base_url, base_url)


def extract_links_for_root(
    html_text: str,
    base_url: str,
    root_url: str,
    include_subdomains: bool = False,
) -> list[str]:
    found: list[str] = []
    seen = set()
    for match in HREF_RE.finditer(html_text):
        href = match.group(1) or match.group(2) or match.group(3) or ""
        normalized = normalize_url_for_root(
            urljoin(base_url, href),
            root_url,
            include_subdomains=include_subdomains,
        )
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append(normalized)
    return found


def first_json_ld_article(json_ld_chunks: list[str]) -> dict[str, Any]:
    def walk(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            typ = value.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if any(t in {"Article", "NewsArticle", "BlogPosting"} for t in types):
                return value
            graph = value.get("@graph")
            if graph:
                found = walk(graph)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        return None

    for chunk in json_ld_chunks:
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        found = walk(parsed)
        if found:
            return found
    return {}


def extract_article(html_text: str, source_url: str, timestamp: str) -> dict[str, Any]:
    parser = ArticleParser()
    parser.feed(html_text)
    ld = first_json_ld_article(parser.json_ld)
    title = (
        ld.get("headline")
        or parser.meta.get("og:title")
        or parser.meta.get("twitter:title")
        or " ".join(parser.title_parts)
    )
    byline = ld.get("author") or parser.meta.get("author") or parser.meta.get("article:author")
    if isinstance(byline, list):
        byline = ", ".join(
            item.get("name", str(item)) if isinstance(item, dict) else str(item) for item in byline
        )
    elif isinstance(byline, dict):
        byline = byline.get("name")
    text = "\n\n".join(dedupe_preserve_order(parser.text_parts))
    published = (
        ld.get("datePublished")
        or parser.meta.get("article:published_time")
        or parser.meta.get("date")
    )
    modified = ld.get("dateModified") or parser.meta.get("article:modified_time")
    notes = []
    if not text:
        notes.append("no_article_text")
    if len(text) < 500:
        notes.append("short_article_text")
    if title and any(marker in title.lower() for marker in ["not found", "page not found", "404"]):
        notes.append("not_found_title")
    if "abcnews.go.com" in html_text[:5000].lower():
        notes.append("abc_shell_marker")
    is_video_page = "/videos/" in urlparse(source_url).path
    if "no_article_text" in notes and is_video_page and title and published:
        notes.append("metadata_only_video_page")
    valid = (
        ("no_article_text" not in notes or "metadata_only_video_page" in notes)
        and "not_found_title" not in notes
    )
    return {
        "url": source_url,
        "timestamp": timestamp,
        "wayback_raw_url": raw_wayback_url(timestamp, source_url),
        "title": html.unescape(str(title)).strip() if title else "",
        "byline": html.unescape(str(byline)).strip() if byline else "",
        "published": published or "",
        "modified": modified or "",
        "text": text,
        "text_length": len(text),
        "parser_notes": notes,
        "valid_article": valid,
        "extracted_at": now_iso(),
    }


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def safe_slug(url: str) -> str:
    parsed = urlparse(url)
    slug = parsed.path.strip("/").replace("/", "__") or "index"
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", slug)
    return slug[:180]


def month_dir_for_article(article: dict[str, Any]) -> str:
    for key in ("published", "modified"):
        value = str(article.get(key) or "")
        match = DATE_MONTH_RE.match(value)
        if match:
            return f"{match.group(1)}-{match.group(2)}"
    timestamp = str(article.get("timestamp") or "")
    if len(timestamp) >= 6 and timestamp[:6].isdigit():
        return f"{timestamp[:4]}-{timestamp[4:6]}"
    return "unknown-month"


def article_to_standalone_html(article: dict[str, Any]) -> str:
    title = html.escape(article.get("title") or "(untitled)")
    byline = html.escape(article.get("byline") or "")
    published = html.escape(article.get("published") or "")
    modified = html.escape(article.get("modified") or "")
    url = html.escape(article.get("url") or "")
    wayback = html.escape(article.get("wayback_raw_url") or "")
    timestamp = html.escape(article.get("timestamp") or "")
    notes = ", ".join(article.get("parser_notes") or [])
    note_html = html.escape(notes)
    paragraphs = [
        f"<p>{html.escape(part.strip())}</p>"
        for part in re.split(r"\n{2,}", article.get("text") or "")
        if part.strip()
    ]
    body = "\n".join(paragraphs)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ font-family: Georgia, 'Times New Roman', serif; line-height: 1.55; margin: 0; color: #222; background: #fafafa; }}
    main {{ max-width: 760px; margin: 0 auto; padding: 40px 24px 64px; background: white; }}
    h1 {{ font-family: Arial, sans-serif; line-height: 1.1; margin-bottom: 12px; }}
    .meta {{ font-family: Arial, sans-serif; color: #555; font-size: 14px; border-bottom: 1px solid #ddd; padding-bottom: 18px; margin-bottom: 28px; }}
    .meta div {{ margin: 4px 0; }}
    p {{ font-size: 19px; margin: 0 0 20px; }}
    a {{ color: #1769aa; }}
  </style>
</head>
<body>
<main>
  <h1>{title}</h1>
  <section class="meta">
    <div><strong>Byline:</strong> {byline}</div>
    <div><strong>Published:</strong> {published}</div>
    <div><strong>Modified:</strong> {modified}</div>
    <div><strong>Original URL:</strong> <a href="{url}">{url}</a></div>
    <div><strong>Wayback raw capture:</strong> <a href="{wayback}">{timestamp}</a></div>
    <div><strong>Parser notes:</strong> {note_html}</div>
  </section>
  <article>
{body}
  </article>
</main>
</body>
</html>
"""


def save_article_html(out_dir: Path, article: dict[str, Any]) -> Path:
    month = month_dir_for_article(article)
    article_dir = out_dir / "articles" / month
    ensure_dir(article_dir)
    output_path = article_dir / f"{safe_slug(article['url'])}.html"
    output_path.write_text(article_to_standalone_html(article), encoding="utf-8")
    return output_path


def portable_output_path(out_dir: Path, output_path: Path) -> str:
    try:
        return output_path.relative_to(out_dir).as_posix()
    except ValueError:
        return output_path.name


def resolve_manifest_output_path(run_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return run_dir / path


def discover_catalogs(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    session = PoliteSession(
        out_dir / "http_cache",
        delay=args.delay,
        user_agent=args.user_agent,
        timeout=args.timeout,
        retries=args.retries,
    )
    prefixes = args.prefix or DEFAULT_PREFIXES
    if not prefixes:
        raise ValueError("discover-catalogs requires at least one --prefix")
    catalog_path = out_dir / "catalog_pages.jsonl"
    candidates_path = out_dir / "candidate_urls.jsonl"
    seen_candidates = {
        row["url"] for row in read_jsonl(candidates_path) if row.get("url")
    }
    seen_catalogs = {
        (row.get("original"), row.get("timestamp")) for row in read_jsonl(catalog_path)
    }

    for prefix in prefixes:
        params = {
            "url": prefix + ("*" if not prefix.endswith("*") else ""),
            "from": args.from_ts,
            "to": args.to_ts,
            "filter": ["statuscode:200", "mimetype:text/html"],
            "collapse": "urlkey",
            "fl": "timestamp,original,statuscode,mimetype,digest,length",
            "limit": args.limit_per_prefix,
        }
        print(f"[discover] CDX prefix {prefix}", file=sys.stderr)
        try:
            rows = cdx_query(session, params)
        except Exception as exc:
            print(f"[warn] CDX failed for {prefix}: {exc}", file=sys.stderr)
            continue
        for row in rows:
            original = row.get("original", "")
            timestamp = row.get("timestamp", "")
            normalized = normalize_url_for_root(original, prefix, include_subdomains=args.include_subdomains)
            if not normalized:
                continue
            key = (original, timestamp)
            if key not in seen_catalogs:
                jsonl_append(catalog_path, {**row, "normalized": normalized, "discovered_at": now_iso()})
                seen_catalogs.add(key)
            try:
                html_text, _ = session.get_text(raw_wayback_url(timestamp, original))
            except Exception as exc:
                print(f"[warn] fetch catalog failed {timestamp} {original}: {exc}", file=sys.stderr)
                continue
            candidate_re = re.compile(args.candidate_regex)
            exclude_re = re.compile(args.exclude_regex) if args.exclude_regex else None
            for link in extract_links_for_root(
                html_text,
                original,
                root_url=prefix,
                include_subdomains=args.include_subdomains,
            ):
                clean = strip_url_noise_for_root(link, prefix, include_subdomains=args.include_subdomains)
                if not clean or not is_generic_article_candidate(clean, candidate_re, exclude_re):
                    continue
                if clean in seen_candidates:
                    continue
                seen_candidates.add(clean)
                jsonl_append(
                    candidates_path,
                    {
                        "url": clean,
                        "source_catalog_url": original,
                        "source_catalog_timestamp": timestamp,
                        "discovered_at": now_iso(),
                    },
                )
                print(f"[candidate] {clean}", file=sys.stderr)


def discover_cdx_urls(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    session = PoliteSession(
        out_dir / "http_cache",
        delay=args.delay,
        user_agent=args.user_agent,
        timeout=args.timeout,
        retries=args.retries,
    )
    prefixes = args.prefix or DEFAULT_PREFIXES
    if not prefixes:
        raise ValueError("discover-cdx-urls requires at least one --prefix")
    candidates_path = out_dir / "candidate_urls.jsonl"
    seen_candidates = {
        row["url"] for row in read_jsonl(candidates_path) if row.get("url")
    }

    for from_ts, to_ts, label in iter_date_chunks(args.year_start, args.year_end, args.chunk):
        for prefix in prefixes:
            candidate_re = re.compile(args.candidate_regex)
            exclude_re = re.compile(args.exclude_regex) if args.exclude_regex else None
            params = {
                "url": prefix + ("*" if not prefix.endswith("*") else ""),
                "from": from_ts,
                "to": to_ts,
                "filter": ["statuscode:200", "mimetype:text/html"],
                "collapse": "urlkey",
                "fl": "timestamp,original,statuscode,mimetype,digest,length",
                "limit": args.limit_per_query,
            }
            print(f"[cdx-inventory] {label} {prefix}", file=sys.stderr)
            try:
                rows = cdx_query(session, params)
            except Exception as exc:
                print(f"[warn] CDX inventory failed for {label} {prefix}: {exc}", file=sys.stderr)
                continue
            if len(rows) >= args.limit_per_query:
                print(
                    f"[warn] query reached limit; rerun with narrower prefix/date: {label} {prefix}",
                    file=sys.stderr,
                )
            for row in rows:
                original = row.get("original", "")
                clean = strip_url_noise_for_root(
                    original,
                    root_url=prefix,
                    include_subdomains=args.include_subdomains,
                )
                if not clean or not is_generic_article_candidate(clean, candidate_re, exclude_re) or clean in seen_candidates:
                    continue
                seen_candidates.add(clean)
                jsonl_append(
                    candidates_path,
                    {
                        "url": clean,
                        "source": "cdx_inventory",
                        "source_cdx": row,
                        "discovered_at": now_iso(),
                    },
                )
                print(f"[candidate] {clean}", file=sys.stderr)


def iter_date_chunks(year_start: int, year_end: int, chunk: str) -> list[tuple[str, str, str]]:
    chunks = []
    for year in range(year_start, year_end + 1):
        if chunk == "year":
            chunks.append((f"{year}0101", f"{year}1231235959", str(year)))
            continue
        for month in range(1, 13):
            last_day = calendar.monthrange(year, month)[1]
            label = f"{year}-{month:02d}"
            chunks.append(
                (
                    f"{year}{month:02d}01",
                    f"{year}{month:02d}{last_day:02d}235959",
                    label,
                )
            )
    return chunks


def latest_captures_for_url(
    session: PoliteSession,
    url: str,
    latest_limit: int,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> list[dict[str, str]]:
    params = {
        "url": url,
        "matchType": "exact",
        "from": from_ts,
        "to": to_ts,
        "filter": ["statuscode:200", "mimetype:text/html"],
        "fl": "timestamp,original,statuscode,mimetype,digest,length",
        "limit": -abs(latest_limit),
        "fastLatest": "true",
    }
    rows = [row for row in cdx_query(session, params) if "timestamp" in row]
    return sorted(rows, key=lambda row: row["timestamp"], reverse=True)


def availability_capture_for_url(
    session: PoliteSession,
    url: str,
    to_ts: str | None = None,
) -> list[dict[str, str]]:
    timestamp = to_ts or "99999999999999"
    text, _ = session.get_text(make_url(AVAILABILITY_URL, {"url": url, "timestamp": timestamp}))
    data = json.loads(text)
    closest = (data.get("archived_snapshots") or {}).get("closest") or {}
    if not closest.get("available") or closest.get("status") != "200" or not closest.get("timestamp"):
        return []
    return [
        {
            "timestamp": str(closest["timestamp"]),
            "original": url,
            "statuscode": str(closest.get("status", "200")),
            "mimetype": "text/html",
            "digest": "",
            "length": "",
            "source": "availability",
        }
    ]


def catalog_captures_for_range(
    session: PoliteSession,
    root_url: str,
    from_ts: str,
    to_ts: str,
    limit: int,
) -> list[dict[str, str]]:
    rows = []
    for variant in catalog_url_variants(root_url):
        rows.extend(
            cdx_query(
                session,
                {
                    "url": variant,
                    "matchType": "exact",
                    "from": from_ts,
                    "to": to_ts,
                    "filter": ["statuscode:200", "mimetype:text/html"],
                    "fl": "timestamp,original,statuscode,mimetype,digest,length",
                    "limit": limit,
                },
            )
        )
    return sorted(dedupe_cdx_rows([row for row in rows if "timestamp" in row]), key=lambda row: row["timestamp"])


def recover_article_record(
    session: PoliteSession,
    url: str,
    latest_limit: int,
    from_ts: str | None = None,
    to_ts: str | None = None,
    latest_strategy: str = "cdx",
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    manifest: dict[str, Any] = {
        "url": url,
        "attempted_at": now_iso(),
        "status": "not_found",
        "captures_checked": [],
    }
    try:
        if latest_strategy == "availability":
            captures = availability_capture_for_url(session, url, to_ts=to_ts)
        else:
            captures = latest_captures_for_url(
                session,
                url,
                latest_limit=latest_limit,
                from_ts=from_ts,
                to_ts=to_ts,
            )
    except Exception as exc:
        manifest["status"] = "cdx_error"
        manifest["error"] = str(exc)
        return None, manifest
    for capture in captures:
        timestamp = capture["timestamp"]
        original = capture.get("original") or url
        manifest["captures_checked"].append(capture)
        try:
            html_text, _ = session.get_text(raw_wayback_url(timestamp, original))
            article = extract_article(html_text, original, timestamp)
        except Exception as exc:
            capture["fetch_error"] = str(exc)
            continue
        if not article["valid_article"]:
            capture["parser_notes"] = article["parser_notes"]
            continue
        article["cdx"] = capture
        manifest.update(
            {
                "status": "recovered",
                "selected_timestamp": timestamp,
                "title": article["title"],
                "text_length": article["text_length"],
            }
        )
        return article, manifest
    return None, manifest


def month_chunks_for_year(year: int, skip_months: set[int] | None = None) -> list[tuple[str, str, str, int]]:
    chunks = []
    skip_months = skip_months or set()
    for month in range(1, 13):
        if month in skip_months:
            continue
        last_day = calendar.monthrange(year, month)[1]
        label = f"{year}-{month:02d}"
        chunks.append(
            (
                f"{year}{month:02d}01",
                f"{year}{month:02d}{last_day:02d}235959",
                label,
                month,
            )
        )
    return chunks


def parse_skip_months(values: list[str] | None) -> set[int]:
    if not values:
        return set()
    names = {name.lower(): idx for idx, name in enumerate(calendar.month_name) if name}
    names.update({name.lower(): idx for idx, name in enumerate(calendar.month_abbr) if name})
    months = set()
    for value in values:
        for part in re.split(r"[,\s]+", value.strip()):
            if not part:
                continue
            key = part.lower()
            if key.isdigit():
                month = int(key)
            else:
                month = names.get(key, 0)
            if not 1 <= month <= 12:
                raise ValueError(f"invalid month for --skip-month: {part}")
            months.add(month)
    return months


def write_catalog_run_summary(
    out_dir: Path,
    root_url: str,
    year: int,
    skip_months: set[int],
    capture_count: int,
    link_count: int,
    manifest_rows: list[dict[str, Any]],
) -> None:
    status_counts: dict[str, int] = {}
    month_counts: dict[str, int] = {}
    metadata_only_count = 0
    for row in manifest_rows:
        status = row.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        output_path = row.get("output_path")
        if output_path:
            month = Path(output_path).parent.name
            month_counts[month] = month_counts.get(month, 0) + 1
        selected = row.get("selected_timestamp")
        for capture in row.get("captures_checked") or []:
            if capture.get("timestamp") == selected and "metadata_only_video_page" in capture.get(
                "parser_notes", []
            ):
                metadata_only_count += 1
    skipped = ", ".join(f"{month:02d}" for month in sorted(skip_months)) or "none"
    status_lines = "\n".join(f"- `{key}`: {value}" for key, value in sorted(status_counts.items()))
    month_lines = "\n".join(f"- `articles/{key}/`: {value} pages" for key, value in sorted(month_counts.items()))
    summary = f"""# Catalog Recovery: {root_url} ({year})

Target:

- Root/catalog URL: `{root_url}`
- Year: `{year}`
- Skipped months: `{skipped}`
- Article recovery rule: latest valid archived capture for each discovered URL
- Output format: standalone `.html` files grouped by publication month

Results:

- Catalog captures found: {capture_count}
- Unique article-like URLs discovered: {link_count}
- Article recovery attempts: {len(manifest_rows)}
- Metadata-only video pages: {metadata_only_count}

Status counts:

{status_lines or "- none"}

Output groups:

{month_lines or "- none"}

Audit files:

- `catalog_captures.jsonl`: all archived catalog captures used.
- `discovered_links.jsonl`: deduplicated article URLs and source catalog timestamps.
- `article_manifest.jsonl`: per-article recovery status, selected capture, title, output path, and checked captures.
- `catalog/YYYY-MM/`: per-month capture and link audit files.
"""
    (out_dir / "RUN_SUMMARY.md").write_text(summary, encoding="utf-8")


def write_retry_summary(out_dir: Path, retry_rows: list[dict[str, Any]], retry_path: Path) -> None:
    status_counts: dict[str, int] = {}
    for row in retry_rows:
        status = row.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    status_lines = "\n".join(f"- `{key}`: {value}" for key, value in sorted(status_counts.items()))
    summary = f"""# Retry Summary

Retry manifest:

- `{retry_path}`

Results:

{status_lines or "- none"}
"""
    (out_dir / "RETRY_SUMMARY.md").write_text(summary, encoding="utf-8")


def source_catalog_month(row: dict[str, Any]) -> str:
    source = row.get("source") or row
    if not isinstance(source, dict):
        source = row
    month = source.get("first_seen_catalog_month") or source.get("catalog_month")
    if month:
        return str(month)
    timestamp = source.get("first_seen_catalog_timestamp") or source.get("source_catalog_timestamp")
    if timestamp and len(str(timestamp)) >= 6:
        ts = str(timestamp)
        return f"{ts[:4]}-{ts[4:6]}"
    return "unknown"


def load_output_audit(run_dirs: list[Path]) -> dict[str, Any]:
    discovered: dict[str, list[dict[str, Any]]] = {}
    manifests: dict[str, dict[str, Any]] = {}
    capture_months: dict[str, int] = {}
    html_months: dict[str, int] = {}
    duplicate_sources: dict[str, list[str]] = {}
    for run_dir in run_dirs:
        for capture_name in ("catalog_captures.jsonl", "politics_captures.jsonl"):
            for row in read_jsonl(run_dir / capture_name):
                month = row.get("catalog_month")
                if not month:
                    ts = str(row.get("timestamp", ""))
                    month = f"{ts[:4]}-{ts[4:6]}" if len(ts) >= 6 else "unknown"
                capture_months[str(month)] = capture_months.get(str(month), 0) + 1
        for link_name in ("discovered_links.jsonl", "politics_discovered_links.jsonl"):
            for row in read_jsonl(run_dir / link_name):
                url = row.get("url")
                if not url:
                    continue
                discovered.setdefault(url, []).append({**row, "_run_dir": str(run_dir)})
        manifest_rows: dict[str, dict[str, Any]] = {}
        for row in read_jsonl(run_dir / "article_manifest.jsonl"):
            if row.get("url"):
                manifest_rows[row["url"]] = row
        for row in read_jsonl(run_dir / "article_manifest_retry.jsonl"):
            if row.get("url") and (row.get("status") == "recovered" or manifest_rows.get(row["url"], {}).get("status") != "recovered"):
                manifest_rows[row["url"]] = row
        for url, row in manifest_rows.items():
            manifests[url] = row
        article_dir = run_dir / "articles"
        if article_dir.exists():
            for html_file in article_dir.glob("*/*.html"):
                month = html_file.parent.name
                html_months[month] = html_months.get(month, 0) + 1
    for url, rows in discovered.items():
        runs = sorted({row["_run_dir"] for row in rows})
        if len(rows) > 1:
            duplicate_sources[url] = runs
    return {
        "discovered": discovered,
        "manifests": manifests,
        "capture_months": capture_months,
        "html_months": html_months,
        "duplicate_sources": duplicate_sources,
    }


def direct_cdx_inventory(
    session: PoliteSession,
    root_url: str,
    prefixes: list[str],
    year: int,
    months: set[int],
    candidate_re: re.Pattern[str],
    exclude_re: re.Pattern[str] | None,
    include_subdomains: bool,
    limit: int,
) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for month in sorted(months):
        last_day = calendar.monthrange(year, month)[1]
        for day in range(1, last_day + 1):
            from_ts = f"{year}{month:02d}{day:02d}000000"
            to_ts = f"{year}{month:02d}{day:02d}235959"
            for prefix in prefixes:
                print(f"[direct-cdx] {year}-{month:02d}-{day:02d} {prefix}", file=sys.stderr)
                try:
                    rows = cdx_query(
                        session,
                        {
                            "url": prefix,
                            "from": from_ts,
                            "to": to_ts,
                            "filter": ["statuscode:200", "mimetype:text/html"],
                            "collapse": "urlkey",
                            "fl": "timestamp,original,statuscode,mimetype,digest,length",
                            "limit": limit,
                        },
                    )
                except Exception as exc:
                    found.setdefault("__errors__", []).append(
                        {"month": f"{year}-{month:02d}", "day": day, "prefix": prefix, "error": str(exc)}
                    )
                    continue
                for row in rows:
                    if "timestamp" not in row:
                        continue
                    clean = strip_url_noise_for_root(
                        row.get("original", ""),
                        root_url=root_url,
                        include_subdomains=include_subdomains,
                    )
                    if clean and is_generic_article_candidate(clean, candidate_re, exclude_re):
                        found.setdefault(clean, []).append(
                            {"month": f"{year}-{month:02d}", "prefix": prefix, "cdx": row}
                        )
    return found


def audit_catalog_output(args: argparse.Namespace) -> None:
    out_path = Path(args.audit_out)
    run_dirs = [Path(path) for path in args.run_dir]
    audit = load_output_audit(run_dirs)
    discovered = audit["discovered"]
    manifests = audit["manifests"]
    status_counts: dict[str, int] = {}
    first_seen_counts: dict[str, int] = {}
    missing_manifest = []
    missing_output = []
    for url, rows in discovered.items():
        first_seen_counts[source_catalog_month(rows[0])] = first_seen_counts.get(source_catalog_month(rows[0]), 0) + 1
        manifest = manifests.get(url)
        if not manifest:
            missing_manifest.append(url)
            continue
        status = manifest.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        if (
            status == "recovered"
            and manifest.get("output_path")
            and not resolve_manifest_output_path(Path(rows[0]["_run_dir"]), manifest["output_path"]).exists()
        ):
            missing_output.append(url)

    direct_missing: list[str] = []
    direct_errors: list[dict[str, Any]] = []
    if args.check_direct_cdx:
        if not args.root_url or not args.year:
            raise ValueError("--check-direct-cdx requires --root-url and --year")
        months = parse_skip_months(args.audit_month)
        if not months:
            months = set(range(1, 13))
        prefixes = args.direct_prefix or [
            urljoin(args.root_url, "/features/*"),
            urljoin(args.root_url, "/videos/*"),
            urljoin(args.root_url, "/live-blog/*"),
        ]
        session = PoliteSession(
            Path(args.cache_dir),
            delay=args.delay,
            user_agent=args.user_agent,
            timeout=args.timeout,
            retries=args.retries,
        )
        direct = direct_cdx_inventory(
            session=session,
            root_url=args.root_url,
            prefixes=prefixes,
            year=args.year,
            months=months,
            candidate_re=re.compile(args.candidate_regex),
            exclude_re=re.compile(args.exclude_regex) if args.exclude_regex else None,
            include_subdomains=args.include_subdomains,
            limit=args.direct_limit,
        )
        direct_errors = direct.pop("__errors__", [])
        direct_missing = sorted(url for url in direct if url not in discovered)

    report = {
        "run_dirs": [str(path) for path in run_dirs],
        "note": "articles/YYYY-MM folders are publication-month buckets; catalog/YYYY-MM and first_seen_catalog_month are catalog-discovery buckets.",
        "catalog_captures_by_month": dict(sorted(audit["capture_months"].items())),
        "discovered_urls": len(discovered),
        "discovered_first_seen_by_catalog_month": dict(sorted(first_seen_counts.items())),
        "manifest_status_counts": dict(sorted(status_counts.items())),
        "html_files_by_publication_month": dict(sorted(audit["html_months"].items())),
        "duplicate_discovered_urls": audit["duplicate_sources"],
        "missing_manifest_urls": missing_manifest,
        "missing_recovered_output_urls": missing_output,
        "direct_cdx_missing_from_catalog": direct_missing,
        "direct_cdx_errors": direct_errors,
    }
    ensure_dir(out_path.parent)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


def catalog_year_to_html(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    session = PoliteSession(
        out_dir / "http_cache",
        delay=args.delay,
        user_agent=args.user_agent,
        timeout=args.timeout,
        retries=args.retries,
    )
    root_url = normalize_url_for_root(args.root_url, args.root_url)
    if not root_url:
        raise ValueError(f"invalid --root-url: {args.root_url}")
    candidate_re = re.compile(args.candidate_regex)
    exclude_re = re.compile(args.exclude_regex) if args.exclude_regex else None
    skip_months = parse_skip_months(args.skip_month)
    captures_path = out_dir / "catalog_captures.jsonl"
    links_path = out_dir / "discovered_links.jsonl"
    manifest_path = out_dir / "article_manifest.jsonl"
    seen_links: dict[str, dict[str, Any]] = {}
    total_captures = 0

    print(
        f"[catalog-year] root={root_url} year={args.year} skip={sorted(skip_months)}",
        file=sys.stderr,
    )
    for from_ts, to_ts, label, _month in month_chunks_for_year(args.year, skip_months):
        month_dir = out_dir / "catalog" / label
        month_captures_path = month_dir / "catalog_captures.jsonl"
        month_links_path = month_dir / "discovered_links.jsonl"
        print(f"[month] {label} catalog captures {from_ts}..{to_ts}", file=sys.stderr)
        captures = catalog_captures_for_range(
            session,
            root_url=root_url,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=args.catalog_limit,
        )
        if len(captures) >= args.catalog_limit:
            print(f"[warn] {label} capture query reached --catalog-limit", file=sys.stderr)
        total_captures += len(captures)
        month_seen = set()

        for capture in captures:
            capture_record = {**capture, "catalog_month": label, "discovered_at": now_iso()}
            jsonl_append(captures_path, capture_record)
            jsonl_append(month_captures_path, capture_record)
            timestamp = capture["timestamp"]
            original = capture.get("original") or root_url
            print(f"[catalog] {label} {timestamp} {original}", file=sys.stderr)
            try:
                catalog_html, _ = session.get_text(raw_wayback_url(timestamp, original))
            except Exception as exc:
                error_record = {
                    "source_catalog_url": original,
                    "source_catalog_timestamp": timestamp,
                    "catalog_month": label,
                    "status": "catalog_fetch_error",
                    "error": str(exc),
                    "discovered_at": now_iso(),
                }
                jsonl_append(links_path, error_record)
                jsonl_append(month_links_path, error_record)
                continue
            links = []
            for link in extract_links_for_root(
                catalog_html,
                original,
                root_url=root_url,
                include_subdomains=args.include_subdomains,
            ):
                clean = strip_url_noise_for_root(
                    link,
                    root_url=root_url,
                    include_subdomains=args.include_subdomains,
                )
                if clean and is_generic_article_candidate(clean, candidate_re, exclude_re):
                    links.append(clean)
            for link in dedupe_preserve_order(links):
                month_seen.add(link)
                record = seen_links.setdefault(
                    link,
                    {
                        "url": link,
                        "source": "catalog_year",
                        "root_url": root_url,
                        "year": args.year,
                        "first_seen_catalog_url": original,
                        "first_seen_catalog_timestamp": timestamp,
                        "first_seen_catalog_month": label,
                        "seen_in_catalog_timestamps": [],
                        "seen_in_catalog_months": [],
                        "discovered_at": now_iso(),
                    },
                )
                record["seen_in_catalog_timestamps"].append(timestamp)
                if label not in record["seen_in_catalog_months"]:
                    record["seen_in_catalog_months"].append(label)

        for link in sorted(month_seen):
            jsonl_append(month_links_path, seen_links[link])

    candidates = list(seen_links.values())
    for candidate in candidates:
        jsonl_append(links_path, candidate)
    if args.max_urls:
        candidates = candidates[: args.max_urls]

    print(f"[catalog-year] recovering {len(candidates)} article URLs", file=sys.stderr)
    manifest_rows: list[dict[str, Any]] = []
    for idx, candidate in enumerate(candidates, 1):
        url = candidate["url"]
        print(f"[article] {idx}/{len(candidates)} {url}", file=sys.stderr)
        article, manifest = recover_article_record(
            session,
            url,
            latest_limit=args.latest_limit,
            from_ts=args.article_from,
            to_ts=args.article_to,
            latest_strategy=args.latest_strategy,
        )
        manifest["source"] = candidate
        if article:
            article["candidate_source"] = candidate
            output_path = save_article_html(out_dir, article)
            manifest["output_path"] = portable_output_path(out_dir, output_path)
        jsonl_append(manifest_path, manifest)
        manifest_rows.append(manifest)

    write_catalog_run_summary(
        out_dir=out_dir,
        root_url=root_url,
        year=args.year,
        skip_months=skip_months,
        capture_count=total_captures,
        link_count=len(seen_links),
        manifest_rows=manifest_rows,
    )


def retry_manifest_html(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    session = PoliteSession(
        out_dir / "http_cache",
        delay=args.delay,
        user_agent=args.user_agent,
        timeout=args.timeout,
        retries=args.retries,
    )
    manifest_path = Path(args.manifest)
    rows = read_jsonl(manifest_path)
    retry_path = out_dir / "article_manifest_retry.jsonl"
    retry_rows: list[dict[str, Any]] = []
    failures = [row for row in rows if row.get("status") != "recovered"]
    if args.max_urls:
        failures = failures[: args.max_urls]
    print(f"[retry] retrying {len(failures)} failed manifest rows", file=sys.stderr)
    for idx, failed in enumerate(failures, 1):
        url = failed["url"]
        print(f"[retry] {idx}/{len(failures)} {url}", file=sys.stderr)
        article, retry = recover_article_record(
            session,
            url,
            latest_limit=args.latest_limit,
            from_ts=args.article_from,
            to_ts=args.article_to,
            latest_strategy=args.latest_strategy,
        )
        retry["previous_status"] = failed.get("status")
        retry["previous_error"] = failed.get("error")
        retry["source"] = failed.get("source") or {"url": url}
        if article:
            article["candidate_source"] = retry["source"]
            output_path = save_article_html(out_dir, article)
            retry["output_path"] = portable_output_path(out_dir, output_path)
        jsonl_append(retry_path, retry)
        retry_rows.append(retry)
    write_retry_summary(out_dir, retry_rows, retry_path)


def resume_discovered_html(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    session = PoliteSession(
        out_dir / "http_cache",
        delay=args.delay,
        user_agent=args.user_agent,
        timeout=args.timeout,
        retries=args.retries,
    )
    candidates_path = Path(args.candidates)
    manifest_path = Path(args.manifest) if args.manifest else out_dir / "article_manifest.jsonl"
    candidates = [row for row in read_jsonl(candidates_path) if row.get("url")]
    existing_rows = read_jsonl(manifest_path)
    recovered_urls = {row["url"] for row in existing_rows if row.get("url") and row.get("status") == "recovered"}
    attempted_urls = {row["url"] for row in existing_rows if row.get("url")}

    pending = []
    for candidate in candidates:
        url = candidate["url"]
        if args.skip_recovered and url in recovered_urls:
            continue
        if args.skip_attempted and url in attempted_urls:
            continue
        pending.append(candidate)
    if args.max_urls:
        pending = pending[: args.max_urls]

    print(
        f"[resume] candidates={len(candidates)} existing={len(existing_rows)} pending={len(pending)}",
        file=sys.stderr,
    )
    for idx, candidate in enumerate(pending, 1):
        url = candidate["url"]
        print(f"[resume] {idx}/{len(pending)} {url}", file=sys.stderr)
        article, manifest = recover_article_record(
            session,
            url,
            latest_limit=args.latest_limit,
            from_ts=args.article_from,
            to_ts=args.article_to,
            latest_strategy=args.latest_strategy,
        )
        manifest["source"] = candidate
        manifest["resumed_at"] = now_iso()
        if article:
            article["candidate_source"] = candidate
            output_path = save_article_html(out_dir, article)
            manifest["output_path"] = portable_output_path(out_dir, output_path)
        jsonl_append(manifest_path, manifest)


def recover_articles(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    session = PoliteSession(
        out_dir / "http_cache",
        delay=args.delay,
        user_agent=args.user_agent,
        timeout=args.timeout,
        retries=args.retries,
    )
    article_dir = out_dir / "articles"
    ensure_dir(article_dir)
    candidates = read_jsonl(Path(args.candidates))
    if args.max_urls:
        candidates = candidates[: args.max_urls]
    manifest_path = out_dir / "article_manifest.jsonl"

    for idx, candidate in enumerate(candidates, 1):
        url = candidate["url"]
        print(f"[recover] {idx}/{len(candidates)} {url}", file=sys.stderr)
        article, manifest = recover_article_record(
            session,
            url,
            latest_limit=args.latest_limit,
            from_ts=args.from_ts,
            to_ts=args.to_ts,
            latest_strategy=args.latest_strategy,
        )
        manifest["source"] = candidate
        if article:
            article["candidate_source"] = candidate
            output_path = article_dir / f"{article['timestamp']}__{safe_slug(url)}.json"
            output_path.write_text(json.dumps(article, indent=2, ensure_ascii=False), encoding="utf-8")
            manifest["output_path"] = portable_output_path(out_dir, output_path)
        jsonl_append(manifest_path, manifest)


def probe(args: argparse.Namespace) -> None:
    out_dir = Path(args.out)
    session = PoliteSession(
        out_dir / "http_cache",
        delay=args.delay,
        user_agent=args.user_agent,
        timeout=args.timeout,
        retries=args.retries,
    )
    section_url = args.root_url
    rows = cdx_query(
        session,
        {
            "url": section_url,
            "matchType": "exact",
            "from": args.from_ts,
            "to": args.to_ts,
            "filter": ["statuscode:200", "mimetype:text/html"],
            "fl": "timestamp,original,statuscode,mimetype,digest,length",
            "limit": -abs(args.limit),
            "fastLatest": "true",
        },
    )
    print(json.dumps({"section_latest_captures": rows}, indent=2))
    if rows:
        chosen = sorted(rows, key=lambda row: row["timestamp"], reverse=True)[0]
        html_text, _ = session.get_text(raw_wayback_url(chosen["timestamp"], chosen["original"]))
        candidate_re = re.compile(args.candidate_regex)
        exclude_re = re.compile(args.exclude_regex) if args.exclude_regex else None
        links = [
            u
            for u in extract_links_for_root(
                html_text,
                chosen["original"],
                root_url=section_url,
                include_subdomains=args.include_subdomains,
            )
            if is_generic_article_candidate(u, candidate_re, exclude_re)
        ]
        print(json.dumps({"sample_article_links": links[:20], "count": len(links)}, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data")
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_REQUEST_DELAY,
        help=(
            "Seconds to wait between HTTP requests. Default is conservative "
            f"({DEFAULT_REQUEST_DELAY:g}s) for Internet Archive bulk access."
        ),
    )
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help="HTTP attempts per uncached request before recording a failure.",
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="Run a small API and parser probe.")
    p_probe.add_argument("--root-url", required=True, help="Exact catalog page URL to probe.")
    p_probe.add_argument("--from", dest="from_ts", default=None)
    p_probe.add_argument("--to", dest="to_ts", default=None)
    p_probe.add_argument("--limit", type=int, default=3)
    p_probe.add_argument("--include-subdomains", action="store_true")
    p_probe.add_argument("--candidate-regex", default=DEFAULT_CANDIDATE_REGEX)
    p_probe.add_argument("--exclude-regex", default=DEFAULT_EXCLUDE_REGEX)
    p_probe.set_defaults(func=probe)

    p_discover = sub.add_parser("discover-catalogs", help="Discover article candidates from catalog captures.")
    p_discover.add_argument("--prefix", action="append", help="Section URL prefix. Can be repeated.")
    p_discover.add_argument("--include-subdomains", action="store_true")
    p_discover.add_argument("--candidate-regex", default=DEFAULT_CANDIDATE_REGEX)
    p_discover.add_argument("--exclude-regex", default=DEFAULT_EXCLUDE_REGEX)
    p_discover.add_argument("--from", dest="from_ts", default="20080101")
    p_discover.add_argument("--to", dest="to_ts", default=None)
    p_discover.add_argument("--limit-per-prefix", type=int, default=1000)
    p_discover.set_defaults(func=discover_catalogs)

    p_cdx = sub.add_parser("discover-cdx-urls", help="Discover article candidates directly from CDX rows.")
    p_cdx.add_argument("--prefix", action="append", help="Section URL prefix. Can be repeated.")
    p_cdx.add_argument("--include-subdomains", action="store_true")
    p_cdx.add_argument("--candidate-regex", default=DEFAULT_CANDIDATE_REGEX)
    p_cdx.add_argument("--exclude-regex", default=DEFAULT_EXCLUDE_REGEX)
    p_cdx.add_argument("--year-start", type=int, default=2008)
    p_cdx.add_argument("--year-end", type=int, default=time.gmtime().tm_year)
    p_cdx.add_argument("--chunk", choices=["year", "month"], default="month")
    p_cdx.add_argument("--limit-per-query", type=int, default=5000)
    p_cdx.set_defaults(func=discover_cdx_urls)

    p_year = sub.add_parser(
        "catalog-year-html",
        help="Recover same-site article links from archived root/catalog captures for a full year.",
    )
    p_year.add_argument("--root-url", required=True, help="Exact catalog page URL to query in CDX.")
    p_year.add_argument("--year", type=int, required=True)
    p_year.add_argument(
        "--skip-month",
        action="append",
        help="Month to skip by number or name. Can be repeated or comma-separated.",
    )
    p_year.add_argument("--include-subdomains", action="store_true")
    p_year.add_argument("--catalog-limit", type=int, default=5000)
    p_year.add_argument("--latest-limit", type=int, default=60)
    p_year.add_argument("--latest-strategy", choices=["cdx", "availability"], default="cdx")
    p_year.add_argument("--article-from", default=None)
    p_year.add_argument("--article-to", default=None)
    p_year.add_argument("--candidate-regex", default=DEFAULT_CANDIDATE_REGEX)
    p_year.add_argument("--exclude-regex", default=DEFAULT_EXCLUDE_REGEX)
    p_year.add_argument("--max-urls", type=int, default=0)
    p_year.set_defaults(func=catalog_year_to_html)

    p_retry = sub.add_parser(
        "retry-manifest-html",
        help="Retry non-recovered rows from a manifest and write monthly HTML outputs.",
    )
    p_retry.add_argument("--manifest", required=True)
    p_retry.add_argument("--latest-limit", type=int, default=60)
    p_retry.add_argument("--latest-strategy", choices=["cdx", "availability"], default="cdx")
    p_retry.add_argument("--article-from", default=None)
    p_retry.add_argument("--article-to", default=None)
    p_retry.add_argument("--max-urls", type=int, default=0)
    p_retry.set_defaults(func=retry_manifest_html)

    p_resume = sub.add_parser(
        "resume-discovered-html",
        help="Recover discovered_links.jsonl candidates into monthly HTML outputs, skipping existing successes.",
    )
    p_resume.add_argument("--candidates", default="data/discovered_links.jsonl")
    p_resume.add_argument("--manifest", default=None)
    p_resume.add_argument("--latest-limit", type=int, default=60)
    p_resume.add_argument("--latest-strategy", choices=["cdx", "availability"], default="cdx")
    p_resume.add_argument("--article-from", default=None)
    p_resume.add_argument("--article-to", default=None)
    p_resume.add_argument("--max-urls", type=int, default=0)
    p_resume.add_argument("--skip-recovered", action=argparse.BooleanOptionalAction, default=True)
    p_resume.add_argument("--skip-attempted", action=argparse.BooleanOptionalAction, default=False)
    p_resume.set_defaults(func=resume_discovered_html)

    p_audit = sub.add_parser(
        "audit-catalog-output",
        help="Audit recovered catalog outputs and optionally compare against direct CDX article-prefix inventory.",
    )
    p_audit.add_argument("--run-dir", action="append", required=True)
    p_audit.add_argument("--audit-out", default="data_audit/catalog_output_audit.json")
    p_audit.add_argument("--root-url")
    p_audit.add_argument("--year", type=int)
    p_audit.add_argument("--audit-month", action="append", help="Month to direct-check by number or name.")
    p_audit.add_argument("--check-direct-cdx", action="store_true")
    p_audit.add_argument("--direct-prefix", action="append")
    p_audit.add_argument("--direct-limit", type=int, default=1000)
    p_audit.add_argument("--cache-dir", default="data_audit_http_cache")
    p_audit.add_argument("--include-subdomains", action="store_true")
    p_audit.add_argument("--candidate-regex", default=DEFAULT_CANDIDATE_REGEX)
    p_audit.add_argument("--exclude-regex", default=DEFAULT_EXCLUDE_REGEX)
    p_audit.set_defaults(func=audit_catalog_output)

    p_recover = sub.add_parser("recover-articles", help="Recover latest valid article capture for candidates.")
    p_recover.add_argument("--candidates", default="data/candidate_urls.jsonl")
    p_recover.add_argument("--from", dest="from_ts", default=None)
    p_recover.add_argument("--to", dest="to_ts", default=None)
    p_recover.add_argument("--latest-limit", type=int, default=40)
    p_recover.add_argument("--latest-strategy", choices=["cdx", "availability"], default="cdx")
    p_recover.add_argument("--max-urls", type=int, default=0)
    p_recover.set_defaults(func=recover_articles)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
