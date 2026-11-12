#!/usr/bin/env python3
"""Model-free arXiv search/source-fetch MCP server for confined DS41 runs."""

import gzip
import html
import io
import json
import os
import contextlib
import email.utils
import fcntl
import hashlib
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PROTOCOL_VERSION = "2024-11-05"
API = "https://export.arxiv.org/api/query"
ARXIV = "https://export.arxiv.org"
OPENALEX_API = "https://api.openalex.org/works"
OPENALEX_MAILTO = os.environ.get("DS_OPENALEX_MAILTO", "contact@1stproof.org")
ALLOWED_HOSTS = {"api.openalex.org", "export.arxiv.org", "arxiv.org"}
USER_AGENT = os.environ.get(
    "DS_ARXIV_USER_AGENT",
    "ds41-math-research-harness/1.0 (model-free arXiv source audit)",
)
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 120
ARXIV_GAP_SECONDS = float(os.environ.get("DS_ARXIV_REQUEST_GAP", "3.05"))
OPENALEX_GAP_SECONDS = float(os.environ.get("DS_OPENALEX_REQUEST_GAP", "0.25"))
MAX_RETRY_WAIT = float(os.environ.get("DS_ARXIV_MAX_RETRY_WAIT", "45"))
MAX_ATTEMPTS = int(os.environ.get("DS_ARXIV_MAX_ATTEMPTS", "4"))
MAX_DOWNLOAD_BYTES = int(os.environ.get("DS_ARXIV_MAX_DOWNLOAD_BYTES", 50 * 1024 * 1024))
MAX_EXTRACTED_BYTES = int(os.environ.get("DS_ARXIV_MAX_EXTRACTED_BYTES", 200 * 1024 * 1024))
MAX_FILES = int(os.environ.get("DS_ARXIV_MAX_FILES", 2000))
MAX_HTML_BYTES = int(os.environ.get("DS_ARXIV_MAX_HTML_BYTES", 10 * 1024 * 1024))
ABSTRACT_CHARS = int(os.environ.get("DS_ARXIV_ABSTRACT_CHARS", 1600))
LOCAL_INDEX = Path(os.environ.get(
    "DS_ARXIV_LOCAL_INDEX", os.path.expanduser(os.environ.get("HARNESS_DATA","~/oss-harness-data"))+"/arxiv-meta/arxiv-metadata.sqlite3"
)).resolve()

try:
    CALL_CAP = int(os.environ.get("DS_ARXIV_CALL_CAP", "10"))
    if CALL_CAP < 0:
        raise ValueError
except ValueError:
    CALL_CAP = 10

RUN_DIR = Path(os.environ.get("DS_RUN_DIR", os.getcwd())).resolve()
LITERATURE = RUN_DIR / "literature"
CACHE_ROOT = Path(os.environ.get("DS_ARXIV_CACHE_DIR", os.path.expanduser(os.environ.get("HARNESS_DATA","~/oss-harness-data"))+"/arxiv-cache")).resolve()
calls = 0


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        parsed = urllib.parse.urlparse(target)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.port not in (None, 443):
            raise RuntimeError(f"refusing redirect outside scholarly-host allowlist: {target}")
        return super().redirect_request(req, fp, code, msg, headers, target)


OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler(), SafeRedirect(), urllib.request.HTTPSHandler()
)


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def tool_result(request_id, payload, is_error=False):
    send({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "isError": bool(is_error),
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}],
        },
    })


def trim_space(text):
    return " ".join((text or "").split())


def trim_abstract(text):
    text = trim_space(text)
    return text if len(text) <= ABSTRACT_CHARS else text[:ABSTRACT_CHARS].rstrip() + " [...]"


def check_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.port not in (None, 443):
        raise RuntimeError(f"refusing URL outside scholarly-host allowlist: {url}")


def ensure_cache_dirs():
    CACHE_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ("locks", "rate", "responses", "eprints"):
        (CACHE_ROOT / name).mkdir(mode=0o700, exist_ok=True)


@contextlib.contextmanager
def file_lock(path):
    ensure_cache_dirs()
    with open(path, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def service_for_url(url):
    host = urllib.parse.urlparse(url).hostname
    if host == "api.openalex.org":
        return "openalex", OPENALEX_GAP_SECONDS
    if host == "export.arxiv.org":
        return "export-arxiv", ARXIV_GAP_SECONDS
    return "arxiv-org", ARXIV_GAP_SECONDS


def rate_state(service):
    return CACHE_ROOT / "rate" / f"{service}.json"


def acquire_host_slot(service, gap, deadline):
    """Serialize request starts across all processes using a host-wide flock."""
    state_path = rate_state(service)
    with file_lock(CACHE_ROOT / "locks" / f"rate-{service}.lock"):
        now = time.time()
        next_allowed = 0.0
        try:
            next_allowed = float(json.loads(state_path.read_text()).get("next_allowed", 0.0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        delay = max(0.0, next_allowed - now)
        if time.monotonic() + delay > deadline:
            raise RuntimeError(
                f"bounded network wait exhausted before {service} request; "
                f"another process or Retry-After requires {delay:.1f}s more"
            )
        if delay:
            time.sleep(delay)
        started = time.time()
        atomic_write(state_path, json.dumps({
            "last_started": started,
            "next_allowed": started + gap,
            "pid": os.getpid(),
        }, sort_keys=True).encode() + b"\n")
        audit = os.environ.get("DS_ARXIV_RATE_AUDIT")
        if audit:
            with open(audit, "a", encoding="utf-8") as out:
                out.write(json.dumps({"service": service, "pid": os.getpid(), "started": started}) + "\n")


def defer_host(service, delay):
    state_path = rate_state(service)
    with file_lock(CACHE_ROOT / "locks" / f"rate-{service}.lock"):
        current = 0.0
        try:
            current = float(json.loads(state_path.read_text()).get("next_allowed", 0.0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        atomic_write(state_path, json.dumps({
            "next_allowed": max(current, time.time() + delay),
            "deferred_by_pid": os.getpid(),
        }, sort_keys=True).encode() + b"\n")


def retry_after_seconds(value):
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            return max(0.0, parsed.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def atomic_write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


OPENALEX_API_KEY = os.environ.get("DS_OPENALEX_API_KEY", "").strip()
OPENALEX_DAILY_MAX = int(os.environ.get("DS_OPENALEX_DAILY_MAX", "800"))
OPENALEX_LANE_SHARE = int(os.environ.get("DS_OPENALEX_LANE_SHARE", "160"))
OPENALEX_BORROW_AFTER_UTC_HOUR = int(os.environ.get("DS_OPENALEX_BORROW_AFTER_UTC_HOUR", "6"))
OPENALEX_LANES = ("k3", "dsf", "qwen24t", "dsp", "glm")
OPENALEX_LANE = os.environ.get("DS_OPENALEX_LANE", "").strip()
OPENALEX_LAST_QUOTA = None


class OpenAlexQuotaError(RuntimeError):
    def __init__(self, message, quota):
        super().__init__(message)
        self.quota = quota


def _quota_state(now=None):
    now = time.time() if now is None else float(now)
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    path = CACHE_ROOT / "rate" / "openalex-daily.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        raw = {}
    if raw.get("day") != day:
        raw = {"day": day, "lanes": {}, "legacy_count": 0}
    lane_data = raw.get("lanes") or {}
    lanes = {name: max(0, int(lane_data.get(name, 0))) for name in OPENALEX_LANES}
    legacy = max(0, int(raw.get("legacy_count", 0)))
    # The pre-lane format had only {day,count}. Attribute that already-used
    # allowance to the default smoke lane so the five shares still sum to 800.
    if not lane_data and not legacy and raw.get("count"):
        lanes["dsf"] = max(0, int(raw["count"]))
    return path, day, lanes, legacy, time.gmtime(now).tm_hour


def openalex_quota_snapshot(lane=None, now=None):
    lane = lane or OPENALEX_LANE
    _, day, lanes, legacy, hour = _quota_state(now)
    lane_used = lanes.get(lane, 0)
    global_used = legacy + sum(lanes.values())
    return {
        "quota_day": day,
        "openalex_lane": lane,
        "openalex_lane_share": OPENALEX_LANE_SHARE,
        "openalex_lane_used": lane_used,
        "openalex_global_used": global_used,
        "openalex_global_max": OPENALEX_DAILY_MAX,
        "openalex_borrow_after_utc_hour": OPENALEX_BORROW_AFTER_UTC_HOUR,
        "openalex_borrow_allowed": hour >= OPENALEX_BORROW_AFTER_UTC_HOUR,
        "openalex_borrowed": False,
        "openalex_quota_charged": False,
    }


def charge_openalex_daily(lane=None, now=None):
    """Charge one uncached search to a five-lane host-wide UTC-day budget."""
    lane = lane or OPENALEX_LANE
    if lane not in OPENALEX_LANES:
        quota = openalex_quota_snapshot(lane, now)
        raise OpenAlexQuotaError(
            f"OpenAlex lane is unset or invalid ({lane!r}); the per-model lane limit cannot be enforced. "
            "The local arXiv index is still available through arxiv_local_search.", quota
        )
    path = CACHE_ROOT / "rate" / "openalex-daily.json"
    with file_lock(CACHE_ROOT / "locks" / "openalex-daily.lock"):
        path, day, lanes, legacy, hour = _quota_state(now)
        lane_used = lanes[lane]
        global_used = legacy + sum(lanes.values())
        quota = openalex_quota_snapshot(lane, now)
        if global_used >= OPENALEX_DAILY_MAX:
            raise OpenAlexQuotaError(
                f"The host-wide OpenAlex limit of {OPENALEX_DAILY_MAX} uncached searches for {day} UTC was hit. "
                "The local arXiv index is still available through arxiv_local_search.", quota
            )
        borrowed = lane_used >= OPENALEX_LANE_SHARE
        if borrowed and hour < OPENALEX_BORROW_AFTER_UTC_HOUR:
            raise OpenAlexQuotaError(
                f"The OpenAlex per-model lane share of {OPENALEX_LANE_SHARE} uncached searches for lane {lane} "
                f"was hit before borrowing opens at {OPENALEX_BORROW_AFTER_UTC_HOUR:02d}:00 UTC. "
                "The local arXiv index is still available through arxiv_local_search.", quota
            )
        lanes[lane] += 1
        state = {"day": day, "lanes": lanes, "legacy_count": legacy, "count": legacy + sum(lanes.values())}
        atomic_write(path, json.dumps(state, sort_keys=True).encode() + b"\n")
        quota.update({
            "openalex_lane_used": lanes[lane],
            "openalex_global_used": state["count"],
            "openalex_borrowed": borrowed,
            "openalex_quota_charged": True,
        })
        return quota


def network_request_bytes(url, cap):
    """Bounded GET with shared pacing, Retry-After, and exponential 429 backoff."""
    check_url(url)
    service, gap = service_for_url(url)
    deadline = time.monotonic() + MAX_RETRY_WAIT
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    global OPENALEX_LAST_QUOTA
    OPENALEX_LAST_QUOTA = None
    if service == "openalex" and OPENALEX_API_KEY:
        headers["Authorization"] = f"Bearer {OPENALEX_API_KEY}"  # header, so the key never lands in cached URLs or logs
        OPENALEX_LAST_QUOTA = charge_openalex_daily()
    req = urllib.request.Request(url, headers=headers)
    last_error = ""
    for attempt in range(MAX_ATTEMPTS):
        acquire_host_slot(service, gap, deadline)
        try:
            mock_file = os.environ.get("DS_ARXIV_MOCK_RESPONSE")
            if mock_file:
                return Path(mock_file).read_bytes(), {"Content-Type": "application/octet-stream"}, url
            remaining = max(1.0, deadline - time.monotonic())
            with OPENER.open(req, timeout=min(READ_TIMEOUT, remaining)) as response:
                final_url = response.geturl()
                check_url(final_url)
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > cap:
                    raise RuntimeError(f"response Content-Length {declared} exceeds cap {cap}")
                chunks, total = [], 0
                while True:
                    chunk = response.read(min(1024 * 1024, cap + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > cap:
                        raise RuntimeError(f"response exceeds byte cap {cap}")
                    chunks.append(chunk)
                return b"".join(chunks), dict(response.headers), final_url
        except urllib.error.HTTPError as exc:
            body = exc.read(1000).decode("utf-8", "replace")
            if exc.code != 429:
                raise RuntimeError(f"HTTP {exc.code} for {url}: {trim_space(body)[:500]}") from exc
            last_error = trim_space(body)[:500]
            delay = retry_after_seconds(exc.headers.get("Retry-After"))
            if delay is None:
                delay = gap * (2 ** attempt)
            delay = max(gap, delay)
            # Publish Retry-After to every process even when this caller lacks
            # enough bounded wait budget to make another attempt itself.
            defer_host(service, delay)
            if attempt + 1 >= MAX_ATTEMPTS or time.monotonic() + delay > deadline:
                raise RuntimeError(
                    f"HTTP 429 persisted from {service} after {attempt + 1} attempts; "
                    f"bounded wait {MAX_RETRY_WAIT:.1f}s exhausted. Retry later. {last_error}"
                ) from exc
        except urllib.error.URLError as exc:
            delay = gap * (2 ** attempt)
            defer_host(service, delay)
            if attempt + 1 >= MAX_ATTEMPTS or time.monotonic() + delay > deadline:
                raise RuntimeError(
                    f"network error persisted for {service} after {attempt + 1} attempts; "
                    f"bounded wait {MAX_RETRY_WAIT:.1f}s exhausted: {exc.reason}"
                ) from exc
    raise RuntimeError(f"HTTP 429 persisted from {service}; retry later. {last_error}")


def cache_digest(kind, normalized):
    return hashlib.sha256(f"{kind}\0{normalized}".encode("utf-8")).hexdigest()


def cached_response(kind, normalized, url, cap):
    """Return immutable cached bytes, coalescing concurrent cache misses."""
    if kind in ('arxiv-eprint', 'arxiv-html'):
        endpoint = os.environ.get('DS_SOURCE_CACHE_ENDPOINT')
        if endpoint:
            import base64
            request = urllib.request.Request(endpoint, data=json.dumps({'url': url}).encode(), headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read(48*1024*1024))
            data = base64.b64decode(result['bytes'], validate=True)
            if len(data) > cap: raise ValueError('source byte bound exceeded')
            return data, result['headers'], result['final_url'], result['status'], result['key']
        try: from source_cache import get
        except ImportError: from v2.source_cache import get
        return get(os.environ.get('DS_SOURCE_CACHE_DIR', str(CACHE_ROOT/'source-cache')), url,
                   lambda: network_request_bytes(url, cap), cap=cap)
    digest = cache_digest(kind, normalized)
    body_path = CACHE_ROOT / "responses" / f"{kind}-{digest}.body"
    meta_path = CACHE_ROOT / "responses" / f"{kind}-{digest}.json"
    with file_lock(CACHE_ROOT / "locks" / f"response-{kind}-{digest}.lock"):
        if body_path.is_file() and meta_path.is_file():
            body = body_path.read_bytes()
            if len(body) <= cap:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                return body, meta.get("headers", {}), meta.get("final_url", url), "hit", digest
        body, headers, final_url = network_request_bytes(url, cap)
        atomic_write(body_path, body)
        atomic_write(meta_path, json.dumps({
            "kind": kind,
            "normalized": normalized,
            "url": url,
            "final_url": final_url,
            "headers": headers,
            "bytes": len(body),
            "fetched_at": time.time(),
        }, ensure_ascii=False, sort_keys=True).encode() + b"\n")
        return body, headers, final_url, "miss", digest


def atom_entries(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise RuntimeError(f"invalid arXiv API XML: {exc}") from exc
    ns = {"a": "http://www.w3.org/2005/Atom"}
    results = []
    for entry in root.findall("a:entry", ns):
        url_id = trim_space(entry.findtext("a:id", default="", namespaces=ns))
        match = re.search(r"/abs/(.+)$", url_id)
        versioned_id = match.group(1) if match else ""
        if not versioned_id:
            continue
        authors = [trim_space(a.findtext("a:name", default="", namespaces=ns)) for a in entry.findall("a:author", ns)]
        categories = []
        for category in entry.findall("a:category", ns):
            term = category.attrib.get("term", "")
            if term and term not in categories:
                categories.append(term)
        published = trim_space(entry.findtext("a:published", default="", namespaces=ns))
        results.append({
            "id": versioned_id,
            "title": trim_space(entry.findtext("a:title", default="", namespaces=ns)),
            "authors": authors,
            "date": published,
            "abstract": trim_abstract(entry.findtext("a:summary", default="", namespaces=ns)),
            "categories": categories,
            "url": f"{ARXIV}/abs/{versioned_id}",
        })
    return results


def openalex_arxiv_id(work):
    candidates = []
    ids = work.get("ids") if isinstance(work.get("ids"), dict) else {}
    candidates.append(ids.get("arxiv"))
    for location in work.get("locations") or []:
        if isinstance(location, dict):
            candidates.extend([location.get("landing_page_url"), location.get("pdf_url")])
    primary = work.get("primary_location")
    if isinstance(primary, dict):
        candidates.extend([primary.get("landing_page_url"), primary.get("pdf_url")])
    for candidate in candidates:
        match = re.search(
            r"arxiv\.org/(?:abs|html|pdf|e-print)/(?:pdf/)?((?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/[0-9]{7})(?:v[1-9][0-9]*)?)",
            str(candidate or ""), re.I,
        )
        if match:
            return match.group(1)
    return None


def local_connection():
    if not LOCAL_INDEX.is_file():
        raise RuntimeError(
            f"local arXiv index is not ready at {LOCAL_INDEX}; ask the harness operator to finish the offline index build"
        )
    uri = f"file:{urllib.parse.quote(str(LOCAL_INDEX))}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def local_metadata(raw_id):
    try:
        base_id, _ = normalize_id(raw_id)
    except ValueError:
        return None
    with local_connection() as connection:
        row = connection.execute(
            "SELECT id,title,authors,abstract,categories,versions,update_date FROM papers WHERE id=? COLLATE NOCASE",
            (base_id,),
        ).fetchone()
    if not row:
        return None
    try:
        version_labels = json.loads(row[5])
    except (TypeError, ValueError):
        version_labels = []
    latest = version_labels[-1] if version_labels else None
    return {
        "id": row[0],
        "latest_id": f"{row[0]}{latest}" if latest else row[0],
        "title": row[1],
        "authors": [row[2]] if row[2] else [],
        "abstract": trim_abstract(row[3]),
        "categories": row[4].split(),
        "versions": version_labels,
        "date": row[6],
        "url": f"{ARXIV}/abs/{row[0]}{latest or ''}",
        "source": "local",
    }


def local_fts_query(query):
    tokens = re.findall(r"[\w]+", query, flags=re.UNICODE)
    if not tokens:
        raise ValueError("local search query must contain letters or digits")
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens[:32])


def local_fts_query_broad(query):
    tokens = re.findall(r"[\w]+", query, flags=re.UNICODE)
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens[:32])


def search_local(query, limit, call_number=None):
    started = time.monotonic()
    exact = re.fullmatch(r"id:\s*(.+)", query, re.I)
    if exact:
        item = local_metadata(exact.group(1))
        results = [item] if item else []
    else:
        fts_query = local_fts_query(query)
        with local_connection() as connection:
            rows = connection.execute(
                """SELECT p.id,p.title,p.authors,p.abstract,p.categories,p.versions,p.update_date
                   FROM papers_fts JOIN papers p ON p.rowid=papers_fts.rowid
                   WHERE papers_fts MATCH ? ORDER BY bm25(papers_fts,6.0,12.0,3.0,1.0,2.0) LIMIT ?""",
                (fts_query, limit),
            ).fetchall()
            if not rows and " AND " in fts_query:
                rows = connection.execute(
                    """SELECT p.id,p.title,p.authors,p.abstract,p.categories,p.versions,p.update_date
                       FROM papers_fts JOIN papers p ON p.rowid=papers_fts.rowid
                       WHERE papers_fts MATCH ? ORDER BY bm25(papers_fts,6.0,12.0,3.0,1.0,2.0) LIMIT ?""",
                    (local_fts_query_broad(query), limit),
                ).fetchall()
        results = []
        for row in rows:
            try:
                version_labels = json.loads(row[5])
            except (TypeError, ValueError):
                version_labels = []
            latest = version_labels[-1] if version_labels else None
            results.append({
                "id": f"{row[0]}{latest}" if latest else row[0],
                "title": row[1],
                "authors": [row[2]] if row[2] else [],
                "abstract": trim_abstract(row[3]),
                "categories": row[4].split(),
                "versions": version_labels,
                "date": row[6],
                "url": f"{ARXIV}/abs/{row[0]}{latest or ''}",
                "source": "local",
            })
    return {
        "backend": "local-arxiv-fts5",
        "source": "local",
        "tool": "arxiv_local_search",
        "query": query,
        "arxiv_call_cap": CALL_CAP,
        "arxiv_call": call_number,
        "returned_ids": [item["id"] for item in results],
        "returned_urls": [item["url"] for item in results],
        "results": results,
        "bytes": 0,
        "files": [],
        "api_url": None,
        "cache_status": "local",
        "cache_scope": "host-read-only",
        "cache_key": None,
        "network_fetches": 0,
        "success": True,
        "error": None,
        "duration_ms": round((time.monotonic() - started) * 1000),
        **openalex_quota_snapshot(),
    }


def search_openalex(query, limit):
    global OPENALEX_LAST_QUOTA
    OPENALEX_LAST_QUOTA = None
    normalized = " ".join(query.split()).casefold()
    pool_size = min(100, max(25, limit * 5))
    params = urllib.parse.urlencode({
        "search": " ".join(query.split()),
        "per-page": pool_size,
        "mailto": OPENALEX_MAILTO,
    })
    url = f"{OPENALEX_API}?{params}"
    body, _, final_url, cache_status, cache_key = cached_response(
        "openalex-search", f"q={normalized}&pool={pool_size}", url, 10 * 1024 * 1024
    )
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid OpenAlex JSON: {exc}") from exc
    results = []
    for work in payload.get("results") or []:
        if not isinstance(work, dict):
            continue
        arxiv_id = openalex_arxiv_id(work)
        if not arxiv_id:
            continue
        authors = []
        for authorship in work.get("authorships") or []:
            author = authorship.get("author") if isinstance(authorship, dict) else None
            name = author.get("display_name") if isinstance(author, dict) else None
            if name:
                authors.append(name)
        result = {
            "id": arxiv_id,
            "title": trim_space(work.get("display_name", "")),
            "authors": authors,
            "date": work.get("publication_date"),
            "categories": [
                topic.get("display_name") for topic in (work.get("topics") or [])
                if isinstance(topic, dict) and topic.get("display_name")
            ],
            "url": f"{ARXIV}/abs/{arxiv_id}",
            "index_url": work.get("id"),
            "source": "openalex",
        }
        if LOCAL_INDEX.is_file():
            local = local_metadata(arxiv_id)
            if local:
                result["id"] = local["latest_id"]
                result["url"] = local["url"]
                result["versions"] = local["versions"]
                result["categories"] = local["categories"]
                result["local_metadata"] = True
        results.append(result)
        if len(results) >= limit:
            break
    quota = OPENALEX_LAST_QUOTA if cache_status == "miss" and OPENALEX_LAST_QUOTA else openalex_quota_snapshot()
    return results, final_url, len(body), cache_status, cache_key, quota


def search_arxiv(query, limit, call_number):
    started = time.monotonic()
    exact_id = re.fullmatch(r"id:\s*((?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/[0-9]{7})(?:v[1-9][0-9]*)?)", query, re.I)
    if exact_id:
        requested_id = exact_id.group(1)
        normalized = requested_id.casefold()
        params = urllib.parse.urlencode({"id_list": requested_id, "max_results": 1})
        api_url = f"{API}?{params}"
        body, _, final_url, cache_status, cache_key = cached_response(
            "arxiv-id", normalized, api_url, 5 * 1024 * 1024
        )
        results = atom_entries(body)
        backend = "arxiv-public-api"
    else:
        try:
            results, final_url, body_size, cache_status, cache_key, quota = search_openalex(query, limit)
            body = b""  # response size is reported separately; raw JSON remains in the shared cache
            backend = "openalex"
        except (OpenAlexQuotaError, RuntimeError) as exc:
            local = search_local(query, limit, call_number)
            local.update({
                "tool": "arxiv_search",
                "fallback_from": "openalex",
                "fallback_reason": str(exc),
                "openalex_refused": isinstance(exc, OpenAlexQuotaError),
            })
            if isinstance(exc, OpenAlexQuotaError):
                local.update(exc.quota)
            elif OPENALEX_LAST_QUOTA:
                local.update(OPENALEX_LAST_QUOTA)
            return local
    payload = {
        "backend": backend,
        "tool": "arxiv_search",
        "query": query,
        "arxiv_call_cap": CALL_CAP,
        "arxiv_call": call_number,
        "returned_ids": [item["id"] for item in results],
        "returned_urls": [item["url"] for item in results],
        "results": results,
        "bytes": len(body) if exact_id else body_size,
        "files": [],
        "api_url": final_url,
        "cache_status": cache_status,
        "cache_scope": "shared",
        "cache_key": cache_key,
        "network_fetches": 0 if cache_status == "hit" else 1,
        "success": True,
        "error": None,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
    if not exact_id:
        payload.update(quota)
    return payload


def normalize_id(value):
    raw = str(value or "").strip()
    raw = re.sub(r"^https?://(?:export\.)?arxiv\.org/(?:abs|html|e-print)/", "", raw, flags=re.I)
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    raw = re.sub(r"\.pdf$", "", raw, flags=re.I).strip("/")
    # Modern ids and legacy archive/number ids only; no path metacharacters or encodings.
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/[0-9]{7})(?:v[1-9][0-9]*)?", raw):
        raise ValueError("invalid arXiv id; expected e.g. 1706.03762 or math/0301234v2")
    match = re.fullmatch(r"(.+?)(?:v([1-9][0-9]*))?", raw)
    return match.group(1), int(match.group(2)) if match.group(2) else None


def resolve_version(base_id):
    params = urllib.parse.urlencode({"id_list": base_id, "max_results": 1})
    body, _, final_url, cache_status, cache_key = cached_response(
        "arxiv-id", base_id.casefold(), f"{API}?{params}", 2 * 1024 * 1024
    )
    entries = atom_entries(body)
    if not entries:
        raise RuntimeError(f"arXiv id not found: {base_id}")
    resolved = entries[0]["id"]
    rbase, version = normalize_id(resolved)
    if rbase.lower() != base_id.lower() or version is None:
        raise RuntimeError(f"arXiv API returned unexpected id {resolved} for {base_id}")
    return version, final_url, len(body), cache_status, cache_key


def safe_member_name(name):
    if not name or "\\" in name or name.startswith("/") or "\x00" in name:
        raise RuntimeError(f"unsafe archive member path: {name!r}")
    path = PurePosixPath(name)
    if any(part in ("", ".", "..") for part in path.parts):
        raise RuntimeError(f"unsafe archive member path: {name!r}")
    return path


def bounded_gunzip(blob):
    output = bytearray()
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
        while True:
            chunk = stream.read(min(1024 * 1024, MAX_EXTRACTED_BYTES + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_EXTRACTED_BYTES:
                raise RuntimeError(f"decompressed source exceeds cap {MAX_EXTRACTED_BYTES}")
    return bytes(output)


def looks_like_tar(blob):
    if len(blob) < 512:
        return False
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as archive:
            archive.next()
        return True
    except tarfile.TarError:
        return False


def write_tar_safely(blob, root):
    files, total = [], 0
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as archive:
        members = archive.getmembers()
        regular = [member for member in members if member.isfile()]
        if len(regular) > MAX_FILES:
            raise RuntimeError(f"archive has {len(regular)} files; cap is {MAX_FILES}")
        for member in members:
            rel = safe_member_name(member.name)
            if member.isdir():
                (root.joinpath(*rel.parts)).mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise RuntimeError(f"refusing non-regular archive member {member.name!r} type={member.type!r}")
            if member.size < 0 or member.size > MAX_EXTRACTED_BYTES or total + member.size > MAX_EXTRACTED_BYTES:
                raise RuntimeError(f"archive extracted bytes exceed cap {MAX_EXTRACTED_BYTES}")
            target = root.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"could not read archive member {member.name!r}")
            remaining = member.size
            with open(target, "xb") as output:
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError(f"truncated archive member {member.name!r}")
                    output.write(chunk)
                    remaining -= len(chunk)
                if source.read(1):
                    raise RuntimeError(f"archive member {member.name!r} exceeds declared size")
            total += member.size
            files.append(rel.as_posix())
    return files, total


def choose_main_tex(root, files):
    candidates = [name for name in files if name.lower().endswith(".tex")]
    if not candidates:
        return None
    def score(name):
        path = root.joinpath(*PurePosixPath(name).parts)
        sample = path.read_bytes()[:512 * 1024].decode("utf-8", "replace")
        base = path.name.lower()
        points = 100 if "\\begin{document}" in sample else 0
        points += 50 if "\\documentclass" in sample else 0
        points += 25 if base in {"main.tex", "paper.tex", "ms.tex", "article.tex"} else 0
        points -= name.count("/")
        return points, path.stat().st_size
    return max(candidates, key=score)


def make_read_only(root):
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_file():
            path.chmod(0o444)
        elif path.is_dir():
            path.chmod(0o555)
    root.chmod(0o555)


def relative_to_run(path):
    return path.relative_to(RUN_DIR).as_posix()


def cached_eprint(versioned_id, source_url):
    """Cache one immutable versioned e-print archive, coalescing concurrent fetches."""
    return cached_response('arxiv-eprint', versioned_id, source_url, MAX_DOWNLOAD_BYTES)


def existing_result(target, versioned_id, source_url, call_number, started):
    files = sorted(relative_to_run(p) for p in target.rglob("*") if p.is_file() and p.name != ".source-bytes.json")
    tex = [name for name in files if name.lower().endswith(".tex")]
    main = None
    if tex:
        rel_inside = [str(Path(name).relative_to(relative_to_run(target))) for name in tex]
        selected = choose_main_tex(target, rel_inside)
        main = f"{relative_to_run(target)}/{selected}" if selected else None
    size = sum((RUN_DIR / name).stat().st_size for name in files)
    try: source_receipt = json.loads((target/".source-bytes.json").read_text())
    except (OSError, ValueError): source_receipt = {}
    return {
        "backend": "arxiv-source",
        "tool": "arxiv_fetch",
        "id": versioned_id,
        "version": int(versioned_id.rsplit("v", 1)[1]),
        "source_sha256": source_receipt.get("sha256"),
        "arxiv_call_cap": CALL_CAP,
        "arxiv_call": call_number,
        "source_url": source_url,
        "returned_urls": [source_url],
        "bytes": size,
        "download_bytes": 0,
        "cache_status": "hit",
        "cache_scope": "run",
        "cache_key": versioned_id.replace("/", "_"),
        "network_fetches": 0,
        "files": files,
        "main_tex_path": main,
        "source_format": "existing",
        "validated_by_local_index": LOCAL_INDEX.is_file(),
        "success": True,
        "error": None,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def fetch_arxiv(raw_id, call_number):
    started = time.monotonic()
    base_id, version = normalize_id(raw_id)
    resolve_url, resolve_bytes, resolve_cache, resolve_key = None, 0, None, None
    local = local_metadata(base_id) if LOCAL_INDEX.is_file() else None
    if LOCAL_INDEX.is_file() and local is None:
        raise RuntimeError(f"arXiv id {base_id} is not present in the local arXiv reference index; fetch refused")
    if local is not None:
        available = local["versions"]
        if version is None:
            if not available:
                raise RuntimeError(f"local arXiv reference index has no versions for {base_id}; fetch refused")
            version = int(available[-1][1:])
            resolve_cache = "local"
        elif f"v{version}" not in available:
            raise RuntimeError(
                f"arXiv version {base_id}v{version} is not present in the local arXiv reference index; fetch refused"
            )
    elif version is None:
        version, resolve_url, resolve_size, resolve_cache, resolve_key = resolve_version(base_id)
        resolve_bytes = resolve_size if resolve_cache == "miss" else 0
    versioned_id = f"{base_id}v{version}"
    source_url = f"{ARXIV}/e-print/{versioned_id}"
    html_url = f"{ARXIV}/html/{versioned_id}"
    folder = versioned_id.replace("/", "_")
    target = LITERATURE / folder
    if target.exists():
        return existing_result(target, versioned_id, source_url, call_number, started)
    try:
        body, headers, final_url, cache_status, cache_key = cached_eprint(versioned_id, source_url)
    except (RuntimeError, ValueError, OSError):
        # An inaccessible/PDF-only source still gets the HTML fallback, never a PDF download.
        body, headers, final_url, cache_status, cache_key = b'%PDF-', {}, source_url, 'failure', None
    source_sha256 = hashlib.sha256(body).hexdigest()
    returned_urls = [url for url in [resolve_url, final_url] if url]
    download_bytes = resolve_bytes + (len(body) if cache_status == "miss" else 0)
    LITERATURE.mkdir(mode=0o755, parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{folder}.partial-", dir=LITERATURE))
    source_format, files, extracted_bytes, main_tex, warning = None, [], 0, None, None
    try:
        if body.startswith(b"%PDF-"):
            # Never persist or return the PDF. Fetch arXiv's HTML rendering as text instead.
            html_body, html_headers, final_html_url, html_cache, _ = cached_response(
                "arxiv-html", versioned_id.casefold(), html_url, MAX_HTML_BYTES
            )
            if html_cache == "miss":
                download_bytes += len(html_body)
            if html_body.startswith(b"%PDF-"):
                raise RuntimeError("arXiv HTML fallback unexpectedly returned a PDF; discarded")
            content_type = html_headers.get("Content-Type", "").lower()
            if "html" not in content_type and b"<html" not in html_body[:1000].lower():
                raise RuntimeError(f"arXiv HTML fallback was not HTML ({content_type or 'unknown content type'})")
            source_sha256 = hashlib.sha256(html_body).hexdigest()
            (temp / "paper.html").write_bytes(html_body)
            files, extracted_bytes = ["paper.html"], len(html_body)
            source_format = "html-fallback"
            warning = "The e-print endpoint returned PDF-only source. The PDF was discarded; paper.html is the arXiv HTML rendering."
            returned_urls.append(final_html_url)
        else:
            unpacked = bounded_gunzip(body) if body.startswith(b"\x1f\x8b") else body
            if unpacked.startswith(b"PK\x03\x04"):
                raise RuntimeError("ZIP source archives are unsupported and were not extracted")
            if looks_like_tar(unpacked):
                files, extracted_bytes = write_tar_safely(unpacked, temp)
                source_format = "tar.gz" if body.startswith(b"\x1f\x8b") else "tar"
            else:
                if len(unpacked) > MAX_EXTRACTED_BYTES:
                    raise RuntimeError(f"single-file source exceeds cap {MAX_EXTRACTED_BYTES}")
                (temp / "main.tex").write_bytes(unpacked)
                files, extracted_bytes = ["main.tex"], len(unpacked)
                source_format = "gzip-tex" if body.startswith(b"\x1f\x8b") else "tex"
            main_tex = choose_main_tex(temp, files)
            if not main_tex:
                raise RuntimeError("source archive contains no .tex file")
        metadata = {
            "id": versioned_id,
            "version": version,
            "source_url": source_url,
            "final_source_url": final_url,
            "html_url": html_url if source_format == "html-fallback" else None,
            "source_format": source_format,
            "download_bytes": download_bytes,
            "extracted_bytes": extracted_bytes,
            "files": files,
            "archive_cache_status": cache_status,
            "archive_cache_key": cache_key,
        }
        (temp / ".arxiv-source.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        files = sorted(files + [".arxiv-source.json"])
        (temp / ".source-bytes.json").write_text(json.dumps({"id": versioned_id, "sha256": source_sha256}))
        temp.rename(target)
        make_read_only(target)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    prefix = relative_to_run(target)
    paths = [f"{prefix}/{name}" for name in files]
    main_path = f"{prefix}/{main_tex}" if main_tex else (f"{prefix}/paper.html" if source_format == "html-fallback" else None)
    return {
        "backend": "arxiv-source",
        "tool": "arxiv_fetch",
        "id": versioned_id,
        "requested_id": raw_id,
        "source_sha256": source_sha256,
        "version": version,
        "arxiv_call_cap": CALL_CAP,
        "arxiv_call": call_number,
        "source_url": source_url,
        "html_url": html_url if source_format == "html-fallback" else None,
        "returned_urls": list(dict.fromkeys(returned_urls)),
        "bytes": extracted_bytes,
        "download_bytes": download_bytes,
        "cache_status": cache_status,
        "cache_scope": "shared",
        "cache_key": cache_key,
        "resolve_cache_status": resolve_cache,
        "resolve_cache_key": resolve_key,
        "network_fetches": (1 if cache_status == "miss" else 0) + (1 if resolve_cache == "miss" else 0),
        "files": paths,
        "main_tex_path": main_path if source_format != "html-fallback" else None,
        "main_text_path": main_path,
        "source_format": source_format,
        "pdf_discarded": source_format == "html-fallback",
        "validated_by_local_index": local is not None,
        "warning": warning,
        "success": True,
        "error": None,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


TOOLS = [
    {
        "name": "arxiv_search",
        "description": "Model-free scholarly search. Free text uses raw OpenAlex metadata and returns arXiv ids; id:<arXiv-id> alone uses the arXiv id_list API for exact resolution.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A free-text scholarly query, or exactly id:<arXiv-id> for version resolution"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "arxiv_local_search",
        "description": "Free unlimited offline search of the read-only local arXiv metadata index. Prefer this for broad sweeps; it uses no network and no OpenAlex quota.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A free-text metadata query, or exactly id:<arXiv-id>"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "arxiv_fetch",
        "description": "Fetch a pinned arXiv LaTeX source version, safely unpack it read-only under literature/, and return its file list and main .tex path. PDFs are never returned or saved.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "arXiv id with or without version"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
]


def handle(request):
    global calls
    request_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ds41-arxiv", "version": "1.1.0"},
            },
        })
        return
    if method in ("notifications/initialized", "notifications/cancelled"):
        return
    if method == "ping":
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        return
    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        return
    if method != "tools/call":
        if request_id is not None:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})
        return
    name = str(params.get("name") or "")
    arguments = params.get("arguments") or {}
    is_local = name == "arxiv_local_search"
    if not is_local:
        calls += 1
    call_number = None if is_local else calls
    identity = {"query": arguments.get("query")} if name in {"arxiv_search", "arxiv_local_search"} else {"id": arguments.get("id")}
    base = {
        "backend": "local-arxiv-fts5" if is_local else ("scholarly-index" if name == "arxiv_search" else "arxiv-source"),
        "tool": name,
        **identity,
        "arxiv_call_cap": CALL_CAP,
        "arxiv_call": call_number,
        "success": False,
    }
    if name not in {"arxiv_search", "arxiv_local_search", "arxiv_fetch"}:
        tool_result(request_id, {**base, "error": f"Unknown tool: {name}"}, True)
        return
    if not is_local and call_number > CALL_CAP:
        tool_result(request_id, {
            **base,
            "refused": True,
            "error": f"The per-episode network/source arXiv tool limit of {CALL_CAP} was hit; call {call_number} was refused. The local arXiv index is still available through arxiv_local_search.",
        }, True)
        return
    started = time.monotonic()
    try:
        if name in {"arxiv_search", "arxiv_local_search"}:
            query = str(arguments.get("query") or "").strip()
            if not query:
                raise ValueError("query must be non-empty")
            limit = max(1, min(10, int(arguments.get("limit") or 5)))
            payload = search_local(query, limit, call_number) if is_local else search_arxiv(query, limit, call_number)
        else:
            raw_id = str(arguments.get("id") or "").strip()
            if not raw_id:
                raise ValueError("id must be non-empty")
            payload = fetch_arxiv(raw_id, call_number)
        tool_result(request_id, payload)
    except Exception as exc:
        tool_result(request_id, {
            **base,
            "error": str(exc),
            "duration_ms": round((time.monotonic() - started) * 1000),
        }, True)


def main():
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if isinstance(request, dict):
                handle(request)
        except Exception as exc:
            request_id = request.get("id") if isinstance(locals().get("request"), dict) else None
            if request_id is not None:
                send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}})


if __name__ == "__main__":
    main()
