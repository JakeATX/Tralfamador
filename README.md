# Tralfamador

Tralfamador is a command-line recovery harness for finding article-like pages
through Internet Archive Wayback Machine catalog captures.

Given a parent/catalog page such as a news section, archive index, tag page, or
blog homepage, it:

1. queries CDX for every archived save of that catalog page in a target year,
2. fetches the raw archived catalog HTML politely and with local caching,
3. extracts same-site links with a compiled href regex,
4. filters article candidates with configurable include/exclude regexes,
5. resolves each candidate to its latest valid archived HTML capture, and
6. writes standalone HTML files grouped by article publication month.

No API key is required. The tool uses public Wayback Machine endpoints.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

## Primary Command

Run a full-year catalog recovery:

```bash
tralfamador \
  --out data/example-news-2021 \
  --user-agent "tralfamador/0.1.1 (research archive recovery; contact: you@example.org)" \
  catalog-year-html \
  --root-url https://example.org/news/ \
  --year 2021
```

Outputs:

```text
data/example-news-2021/
  articles/YYYY-MM/*.html
  catalog/YYYY-MM/catalog_captures.jsonl
  catalog/YYYY-MM/discovered_links.jsonl
  catalog_captures.jsonl
  discovered_links.jsonl
  article_manifest.jsonl
  RUN_SUMMARY.md
  http_cache/
```

The `articles/YYYY-MM/` folders are publication-month buckets. Catalog discovery
months are tracked separately in `catalog/YYYY-MM/` and in manifest provenance.

## Useful Options

```bash
--include-subdomains
--candidate-regex 'https?://[^/]+/news/[0-9]{4}/[0-9]{2}/[^/?#]+/?$'
--exclude-regex '/(?:tag|author|page|search|about|privacy)(?:/|$)'
--skip-month january
--latest-limit 100
--article-from 20210101
--article-to 20211231235959
--max-urls 25
```

The default candidate regex is intentionally broad: it looks for same-site paths
with hyphenated final slugs. For a new publication, run a small probe first and
then tighten `--candidate-regex` to match that site's article URL shape.

## Internet Archive Access Policy

Tralfamador is intentionally conservative by default:

- Single-threaded requests only.
- `--delay` defaults to `4` seconds between HTTP requests.
- HTTP responses are cached under `http_cache/` to avoid repeat downloads.
- Transient errors retry with exponential backoff.
- `429 Too Many Requests` responses honor `Retry-After` before continuing.
- `--user-agent` should identify your project and include a contact address for
  sustained or large recovery work.

Do not lower `--delay` or run concurrent copies unless you have coordinated with
Internet Archive or are doing a very small interactive probe.

## Probe

```bash
tralfamador \
  --out data/probe \
  probe \
  --root-url https://example.org/news/ \
  --from 20210101 \
  --to 20210131235959
```

## Retry Failures

```bash
tralfamador \
  --out data/example-news-2021 \
  retry-manifest-html \
  --manifest data/example-news-2021/article_manifest.jsonl
```

## Resume An Interrupted Run

If a full-year run completed catalog discovery but stopped during article
recovery, resume from the discovered link manifest:

```bash
tralfamador \
  --out data/example-news-2021 \
  resume-discovered-html \
  --candidates data/example-news-2021/discovered_links.jsonl
```

By default this skips URLs already recovered in `article_manifest.jsonl` and
retries earlier failures. Add `--skip-attempted` to skip every URL already
present in the manifest, including failures.

## Audit Outputs

```bash
tralfamador \
  audit-catalog-output \
  --run-dir data/example-news-2021 \
  --audit-out data/example-news-2021/audit/catalog_output_audit.json
```

## FiveThirtyEight Example

```bash
tralfamador \
  --out data/fivethirtyeight-politics-2021 \
  --user-agent "tralfamador/0.1.1 (research archive recovery; contact: you@example.org)" \
  catalog-year-html \
  --root-url https://fivethirtyeight.com/politics/ \
  --year 2021
```

## Public Repo Hygiene

Generated article HTML, manifests, and HTTP cache files are intentionally ignored
by `.gitignore`. Review copyright and redistribution rules before publishing any
recovered content. A public repository should usually contain code, docs, tests,
and small synthetic fixtures, not downloaded archive payloads.

Avoid putting personal contact details in committed examples. Supply a real
project contact at runtime with `--user-agent` when running at scale.

## Wayback Endpoints Used

- CDX search: `https://web.archive.org/cdx/search/cdx`
- Raw replay: `https://web.archive.org/web/{timestamp}id_/{original_url}`

Internet Archive asks automated clients to use a descriptive `User-Agent`, cache
responses, add delays, and honor `429 Retry-After` responses. Tralfamador does
those by default; keep `--delay` at or above the default unless coordinating
directly with Internet Archive.
