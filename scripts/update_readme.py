#!/usr/bin/env python3
"""Update the GitHub profile README: activity feed + featured repositories.

Design rules (kept deliberately small):

- `featured.json` (repo root) lists the featured repositories. Entries pin the
  immutable repository `id` (rename-proof); `name` is a fallback that can still
  resolve a rename, because /repos/{owner}/{old-name} keeps redirecting after
  a rename. Renames and deletions therefore never require edits to this file.
- Every request goes through Gateway.get(): at most 3 attempts, exponential
  backoff for transient failures (429, 403 secondary rate limit, 5xx, network
  errors, malformed bodies), honoring Retry-After. Definitive errors
  (400/401/404, permission 403) fail immediately without retry.
- Section generation failures are recoverable: the affected section keeps its
  previous content and a warning is emitted. Structural problems (missing or
  duplicate README markers, invalid config) are critical and abort before
  anything is written.
- Output is generated fully in memory, validated, and only written atomically
  (temp file + os.replace) when it differs from the current README. Identical
  input data produces byte-identical output.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API = "https://api.github.com"
USER = "Malte-Dzierzon"
PROFILE_REPO = f"{USER}/{USER}"
MAX_ACTIVITIES = 4   # lines shown in the activity panel
MAX_LOOKUPS = 15     # commit-detail fetches per run (rate-limit budget)
PAGES_CAP = 5        # pages per list endpoint
ATTEMPTS = 3         # attempts per request
RETRY_BASE = 1.0
RETRY_MAX = 8.0

SCRIPT_DIR = Path(__file__).resolve().parent
README_PATH = SCRIPT_DIR.parent / "README.md"
FEATURED_CONFIG = SCRIPT_DIR.parent / "featured.json"

SHA_RE = re.compile(r"[0-9a-f]{40}")
SLUG_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
ESCAPES = {"\\": "\\\\", "[": "\\[", "]": "\\]", "<": "\\<", ">": "\\>", "|": "\\|"}
BACKSLASH_RE = re.compile(r"[\[\]<>|\\]")
SPACE_RE = re.compile(r"\s+")

SECTION_MARKERS = ("ACTIVITY", "REPOSITORIES")


class CriticalError(Exception):
    """Structural defect; abort the run without writing anything."""


class ApiError(Exception):
    """A request failed; either definitively or after retries."""


class _Transient(Exception):
    def __init__(self, msg: str, after: float | None):
        super().__init__(msg)
        self.after = after


class _Definitive(Exception):
    pass


def warn(msg: str) -> None:
    """Surface recoverable failures without failing the run."""
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") == "true" else "WARNING: "
    print(f"{prefix}{msg}", file=sys.stderr, flush=True)


def esc(text: Any) -> str:
    """Escape markdown-significant characters.

    Escaping '<' also neutralizes raw-HTML injections ('<svg>'), comment
    starts ('<!--') and tags: GFM renders the escaped char literally.
    """
    if not isinstance(text, str):
        return ""
    t = BACKSLASH_RE.sub(lambda m: ESCAPES[m.group(0)], text)
    return SPACE_RE.sub(" ", t).strip()


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def is_bot(login: Any) -> bool:
    return isinstance(login, str) and login.endswith("[bot]")


def _retry_after(headers: Any) -> float | None:
    try:
        return max(0.0, float(headers.get("Retry-After")))
    except (AttributeError, TypeError, ValueError):
        return None


def _request(url: str, token: str | None, timeout: int = 30) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "profile-readme-updater",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 429 or e.code == 403 or 500 <= e.code < 600:
            raise _Transient(f"HTTP {e.code} {e.reason}", _retry_after(e.headers)) from None
        raise _Definitive(f"HTTP {e.code} {e.reason} for {url}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise _Transient(f"network error: {e}", None) from None
    with resp:
        raw = resp.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise _Transient(f"malformed body: {e}", None) from None


class Gateway:
    """Minimal api.github.com client: bounded attempts, exp backoff.

    Inject `fetch(url, token)` and `sleep(seconds)` for tests.
    """

    def __init__(self, token: str | None = None, fetch: Any = None, sleep: Any = None):
        self._token = token
        self._fetch = fetch or _request
        self._sleep = sleep or time.sleep

    def get(self, url: str) -> Any:
        last: _Transient | None = None
        for attempt in range(ATTEMPTS):
            try:
                return self._fetch(url, self._token)
            except _Definitive as e:
                raise ApiError(str(e)) from None
            except _Transient as e:
                last = e
                if attempt + 1 < ATTEMPTS:
                    delay = min(RETRY_MAX, max(RETRY_BASE * 2**attempt, e.after or 0.0))
                    warn(f"transient failure: {e}; retrying in {delay:.1f}s")
                    self._sleep(delay)
        raise ApiError(f"{last} (after {ATTEMPTS} attempts)")


def replace_section(content: str, marker: str, new: str) -> str:
    """Swap content between a marker pair; preserve everything else byte-exact."""
    pattern = re.compile(
        rf"(<!-- {marker} starts -->\n).*?(\n<!-- {marker} ends -->)", re.DOTALL
    )
    matches = pattern.findall(content)
    if len(matches) != 1:
        raise CriticalError(f"section {marker!r}: marker pair must occur exactly once")
    return pattern.sub(lambda m: m.group(1) + new + m.group(2), content)


def ensure_sections(content: str) -> None:
    for marker in SECTION_MARKERS:
        for tag in ("starts", "ends"):
            n = content.count(f"<!-- {marker} {tag} -->")
            if n != 1:
                raise CriticalError(
                    f"README marker '<!-- {marker} {tag} -->' occurs {n} times (expected 1)"
                )


def fetch_pages(gw: Gateway, base: str) -> list[dict]:
    """Collect up to PAGES_CAP 100-entry pages; malformed entries are dropped."""
    items: list[dict] = []
    for page in range(1, PAGES_CAP + 1):
        batch = gw.get(f"{base}&page={page}")
        if not isinstance(batch, list):
            if page == 1:
                raise ApiError(f"{base}: expected a JSON list, got {type(batch).__name__}")
            break
        items.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
    return items


# ---------------------------------------------------------------------------
# Activity feed
# ---------------------------------------------------------------------------

def relevant_event(ev: Any) -> bool:
    if not isinstance(ev, dict):
        return False
    slug = (ev.get("repo") or {}).get("name")
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug) or slug == PROFILE_REPO:
        return False
    if is_bot((ev.get("actor") or {}).get("login")):
        return False
    payload = ev.get("payload") or {}
    etype = ev.get("type")
    if etype == "PushEvent":
        return isinstance(payload.get("head"), str)
    if etype == "PullRequestEvent":
        pr = payload.get("pull_request") or {}
        return payload.get("action") == "opened" or (
            payload.get("action") == "closed" and pr.get("merged") is True
        )
    if etype == "IssuesEvent":
        return payload.get("action") == "opened"
    return False


def event_key(ev: dict) -> str | None:
    """Dedupe identity; None means 'not renderable' (caller warns + skips)."""
    payload = ev.get("payload") or {}
    etype = ev.get("type")
    if etype == "PushEvent":
        head = payload.get("head")
        return f"push:{head}" if isinstance(head, str) and SHA_RE.fullmatch(head) else None
    obj = payload.get("pull_request") or payload.get("issue") or {}
    ident = obj.get("id")
    return f"{etype}:{ident}" if isinstance(ident, int) else None


def fmt_commit_line(slug: str, head: str, when: datetime, detail: dict) -> str | None:
    if is_bot(((detail or {}).get("author") or {}).get("login")):
        return None
    subject = ((detail or {}).get("commit") or {}).get("message") or ""
    subject = subject.split("\n", 1)[0].strip()
    url = f"https://github.com/{slug}"
    short = head[:7]
    msg = esc(subject)
    if msg:
        return f"- [{slug}]({url}) — {msg} ([{short}]({url}/commit/{head})) — {when:%Y-%m-%d}"
    return f"- Push to [{slug}]({url}) ([{short}]({url}/commit/{head})) — {when:%Y-%m-%d}"


def format_event(ev: dict, gw: Gateway) -> str | None:
    """Render one event; returns None (with warning) when unrenderable."""
    slug = (ev.get("repo") or {}).get("name")
    when = parse_ts(ev.get("created_at"))
    if when is None:
        warn(f"event {ev.get('id')}: missing/malformed created_at; skipped")
        return None
    payload = ev.get("payload") or {}
    etype = ev.get("type")
    url = f"https://github.com/{slug}"
    if etype == "PushEvent":
        head = payload.get("head")
        if not isinstance(head, str) or not SHA_RE.fullmatch(head):
            warn(f"event {ev.get('id')}: head is not a commit SHA; skipped")
            return None
        try:
            detail = gw.get(f"{API}/repos/{slug}/commits/{head}")
        except ApiError as e:
            warn(f"{slug}: head commit {head[:7]} unavailable ({e}); event skipped")
            return None
        if not isinstance(detail, dict):
            warn(f"{slug}: head commit {head[:7]} malformed; event skipped")
            return None
        return fmt_commit_line(slug, head, when, detail)
    if etype == "PullRequestEvent":
        pr = payload.get("pull_request") or {}
        number = pr.get("number")
        if not isinstance(number, int):
            warn(f"event {ev.get('id')}: PR number missing; skipped")
            return None
        verb = "merged" if pr.get("merged") is True else "opened"
        return f"- PR #{number} {verb} in [{slug}]({url}) — {when:%Y-%m-%d}"
    issue = payload.get("issue") or {}
    number = issue.get("number")
    if not isinstance(number, int):
        warn(f"event {ev.get('id')}: issue number missing; skipped")
        return None
    return f"- Issue #{number} opened in [{slug}]({url}) — {when:%Y-%m-%d}"


def build_activity(gw: Gateway) -> str | None:
    """Build the activity section; None keeps the previous section content."""
    events = fetch_pages(gw, f"{API}/users/{USER}/events/public?per_page=100")
    lines: list[tuple[datetime, str, str]] = []
    seen: set[str] = set()
    lookups = 0
    skipped = 0
    for ev in events:
        if len(lines) + lookups >= MAX_LOOKUPS and len(lines) >= MAX_ACTIVITIES:
            break
        if not isinstance(ev, dict) or not relevant_event(ev):
            continue
        key = event_key(ev)
        if key is None:
            skipped += 1
            continue
        if key in seen:
            continue
        seen.add(key)
        if ev.get("type") == "PushEvent":
            lookups += 1
        try:
            line = format_event(ev, gw)
        except ApiError:
            line = None  # format_event already warned
        if line:
            when = parse_ts(ev.get("created_at"))
            lines.append((when, key, line))
    if skipped:
        warn(f"activity: {skipped} event(s) lacked renderable identifiers; skipped")
    if not lines:
        warn("activity: no renderable public events; section kept from previous run")
        return None
    lines.sort(key=lambda i: (i[0], i[1]), reverse=True)
    return "\n".join(line for _, _, line in lines[:MAX_ACTIVITIES])


# ---------------------------------------------------------------------------
# Featured repositories
# ---------------------------------------------------------------------------

class RepoRef:
    __slots__ = ("name", "repo_id")

    def __init__(self, name: str | None, repo_id: int | None):
        self.name = name
        self.repo_id = repo_id

    def __repr__(self) -> str:
        return f"RepoRef(name={self.name!r}, repo_id={self.repo_id!r})"


def repo_ref(entry: Any, index: int) -> RepoRef:
    if not isinstance(entry, dict):
        raise CriticalError(f"featured.json entry #{index} is not an object: {entry!r}")
    rid = entry.get("id")
    name = entry.get("name")
    has_id = isinstance(rid, int) and not isinstance(rid, bool)
    has_name = isinstance(name, str) and bool(NAME_RE.fullmatch(name.strip()))
    if not (has_id or has_name):
        raise CriticalError(
            f"featured.json entry #{index} needs 'id' (int) or valid 'name': {entry!r}"
        )
    return RepoRef(name.strip() if has_name else None, rid if has_id else None)


def load_featured(path: Path) -> list[RepoRef]:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except OSError as e:
        raise CriticalError(f"featured.json unreadable: {e}") from None
    except json.JSONDecodeError as e:
        raise CriticalError(f"featured.json is not valid JSON: {e}") from None
    if not isinstance(raw, list):
        raise CriticalError("featured.json must be a JSON array of objects")
    refs = [repo_ref(entry, i) for i, entry in enumerate(raw)]
    if not refs:
        raise CriticalError("featured.json lists no repositories")
    return refs


def repo_line(r: dict) -> str | None:
    name = r.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        warn(f"featured repo malformed (no usable name): {name!r}; skipped")
        return None
    url = r.get("html_url")
    if not isinstance(url, str) or not url.startswith("https://github.com/"):
        url = f"https://github.com/{USER}/{name}"
    desc = esc(r.get("description")) or "_No description_"
    stars = r.get("stargazers_count")
    star = (
        f" ({stars} ★)"
        if isinstance(stars, int) and not isinstance(stars, bool) and stars > 0
        else ""
    )
    pushed = parse_ts(r.get("pushed_at"))
    date = f" — {pushed:%Y-%m-%d}" if pushed else ""
    return f"- [{name}]({url}) — {desc}{star}{date}"


def resolve_repo(ref: RepoRef, gw: Gateway, pool: list[dict]) -> dict | None:
    """Resolve by immutable id first; fall back to name (redirect-following).

    /repos/{owner}/{name} keeps redirecting after a rename, so a name-only
    config survives renames too; a deleted/private repo yields None.
    """
    if ref.repo_id is not None:
        for r in pool:
            if r.get("id") == ref.repo_id:
                return r
        try:
            by_id = gw.get(f"{API}/repositories/{ref.repo_id}")
        except ApiError as e:
            warn(f"featured {ref!r}: id lookup failed ({e})")
            return None
        return by_id if isinstance(by_id, dict) else None
    for r in pool:
        if r.get("name") == ref.name:
            return r
    try:
        by_name = gw.get(f"{API}/repos/{USER}/{ref.name}")
    except ApiError as e:
        warn(f"featured {ref!r}: unavailable ({e})")
        return None
    return by_name if isinstance(by_name, dict) else None


def build_featured(gw: Gateway, config_path: Path) -> str | None:
    """Build the featured section, or None to keep previous content."""
    refs = load_featured(config_path)
    pool = fetch_pages(gw, f"{API}/users/{USER}/repos?per_page=100")
    lines: list[str] = []
    missing = 0
    for ref in refs:
        r = resolve_repo(ref, gw, pool)
        if r is None:
            missing += 1
            warn(f"featured repository {ref!r} not found (deleted, private, or renamed without id)")
            continue
        line = repo_line(r)
        if line:
            lines.append(line)
    if not lines:
        warn("featured repositories: nothing resolvable; section kept from previous run")
        return None
    if missing:
        warn(f"featured repositories: {missing} of {len(refs)} unresolved")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_update(
    token: str | None = None,
    readme_path: Path | None = None,
    config_path: Path | None = None,
    gw: Gateway | None = None,
) -> bool:
    """Build, validate, and atomically swap the README. True iff changed."""
    readme = Path(readme_path) if readme_path else README_PATH
    config = Path(config_path) if config_path else FEATURED_CONFIG
    original = readme.read_text(encoding="utf-8")
    ensure_sections(original)
    gateway = gw or Gateway(token=token)

    updated = original
    for marker in SECTION_MARKERS:
        builder = build_activity if marker == "ACTIVITY" else (lambda g: build_featured(g, config))
        try:
            new = builder(gateway)
        except CriticalError:
            raise  # structural/config defects abort before any write
        except ApiError as e:
            warn(f"{marker}: data unavailable, keeping previous content ({e})")
            continue
        except Exception as e:  # noqa: BLE001 surfaced; never corrupt the README
            warn(f"{marker}: unexpected failure ({type(e).__name__}: {e}); section kept")
            continue
        if new is None:
            continue  # builder warned and chose to keep previous content
        updated = replace_section(updated, marker, new)

    if updated == original:
        return False
    ensure_sections(updated)
    tmp = readme.with_name(readme.name + ".tmp")
    tmp.write_text(updated, encoding="utf-8")
    os.replace(tmp, readme)
    return True


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    try:
        changed = run_update(token)
        print("README updated" if changed else "No changes needed")
        return 0
    except CriticalError as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        return 1
    except Exception as e:  # boundary: fail loudly, never half-write
        print(f"ERROR: unexpected {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
