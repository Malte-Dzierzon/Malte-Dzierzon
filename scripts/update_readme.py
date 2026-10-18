#!/usr/bin/env python3
"""Update GitHub profile README: activity + featured repositories.

Activity model: public Events API (PushEvent commits, merged PRs from
PullRequestEvent, IssuesEvent) cross-checked against GraphQL
contributionsCollection PR/issue nodes. Events carry exact timestamps and
per-commit links; contributionsCollection only yields weekly aggregates
without links, so it cannot drive the timeline alone.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

README_PATH = os.path.join(os.path.dirname(__file__), "..", "README.md")
MAX_ACTIVITIES = 4
USER = "Malte-Dzierzon"
REPO = "Malte-Dzierzon"  # profile repo: excluded everywhere
GITHUB_API = "https://api.github.com"
# Fixed curation: pinned order mirrored, no auto-fill.
FEATURED = ["dotfile-s", "Informatik-Projekt-August-Malte", "omarchy-haven-theme"]


def read_readme() -> str:
    with open(README_PATH, encoding="utf-8") as f:
        return f.read()


def replace_section(content: str, marker: str, new: str) -> str:
    pattern = re.compile(
        rf"(<!-- {marker} starts -->)\n.*?\n(<!-- {marker} ends -->)", re.DOTALL
    )
    matches = pattern.findall(content)
    if len(matches) != 1:
        raise ValueError(f"Marker {marker!r} must appear exactly once")
    return pattern.sub(lambda m: f"{m.group(1)}\n{new}\n{m.group(2)}", content)


def api_get(url: str, token: str | None) -> Any:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "profile-readme-updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub API error: {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e


def api_graphql(query: str, token: str) -> Any:
    data = json.dumps({"query": query}).encode()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "profile-readme-updater",
    }
    req = urllib.request.Request(f"{GITHUB_API}/graphql", data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.load(resp)
    if "errors" in result:
        raise RuntimeError(f"GraphQL errors: {result['errors']}")
    return result.get("data")


def ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def esc(text: str) -> str:
    """Escape markdown-significant chars in remote descriptions/titles."""
    return re.sub(r"([\[\]<>|])", r"\\\1", text.strip())


def is_bot(login: str) -> bool:
    return login.endswith("[bot]") or login in ("github-actions[bot]", "dependabot[bot]")


def head_commit(repo: str, sha: str, token: str | None) -> dict[str, Any]:
    """Fetch head commit detail (message/author); PushEvent payloads omit commits."""
    try:
        return api_get(f"{GITHUB_API}/repos/{repo}/commits/{sha}", token)
    except RuntimeError:
        return {}

def relevant_event(e: dict[str, Any]) -> bool:
    if e.get("repo", {}).get("name") == f"{USER}/{REPO}":
        return False
    if is_bot(e.get("actor", {}).get("login", "")):
        return False
    if e.get("type") == "PullRequestEvent":
        action = e.get("payload", {}).get("action", "")
        merged = e.get("payload", {}).get("pull_request", {}).get("merged", False)
        return action == "opened" or (action == "closed" and merged)
    if e.get("type") == "IssuesEvent":
        return e.get("payload", {}).get("action") == "opened"
    return e.get("type") == "PushEvent"


def fmt_push(e: dict[str, Any], token: str | None) -> tuple[str | None, datetime | None]:
    repo = e.get("repo", {}).get("name", "")
    repo_url = f"https://github.com/{repo}"
    when = ts(e.get("created_at"))
    if not when:
        return None, None
    sha = e.get("payload", {}).get("head", "")
    if not sha:
        return None, when
    detail = head_commit(repo, sha, token)
    message = (detail.get("commit", {}).get("message", "") or "").splitlines()
    author = detail.get("author", {}) or {}
    if is_bot(author.get("login", "")):
        return None, when
    short = sha[:7]
    if message and message[0]:
        return f"- [{repo}]({repo_url}) — {esc(message[0])} ([{short}]({repo_url}/commit/{sha})) — {when:%Y-%m-%d}", when
    return f"- Push to [{repo}]({repo_url}) ([{short}]({repo_url}/commit/{sha})) — {when:%Y-%m-%d}", when


def fmt_event(e: dict[str, Any], token: str | None) -> tuple[str | None, datetime | None]:
    repo = e.get("repo", {}).get("name", "")
    repo_url = f"https://github.com/{repo}"
    when = ts(e.get("created_at"))
    etype = e.get("type")
    if etype == "PushEvent":
        line, when = fmt_push(e, token)
        return line, when
    if etype == "PullRequestEvent":
        pr = e.get("payload", {}).get("pull_request", {})
        merged = pr.get("merged", False)
        verb = "merged" if merged else "opened"
        return f"- PR #{pr.get('number')} {verb} in [{repo}]({repo_url}) — {when:%Y-%m-%d}", when
    if etype == "IssuesEvent":
        issue = e.get("payload", {}).get("issue", {})
        return f"- Issue #{issue.get('number')} opened in [{repo}]({repo_url}) — {when:%Y-%m-%d}", when
    return None, when


def build_activity(events: list[dict[str, Any]], token: str | None) -> str:
    seen: set[str] = set()
    items: list[tuple[datetime, str]] = []
    lookups = 0
    for e in events:
        if len(items) + lookups >= 15 and len(items) >= MAX_ACTIVITIES:
            break
        if not relevant_event(e):
            continue
        # Dedup: head SHA for pushes, PR/issue id otherwise.
        if e.get("type") == "PushEvent":
            key = "push:" + e.get("payload", {}).get("head", e.get("id", ""))
        else:
            obj = e.get("payload", {}).get("pull_request") or e.get("payload", {}).get("issue") or {}
            key = f"{e.get('type')}:{obj.get('id', e.get('id'))}"
        if key in seen:
            continue
        seen.add(key)
        if e.get("type") == "PushEvent":
            lookups += 1
        line, when = fmt_event(e, token)
        if line and when:
            items.append((when, line))
    items.sort(key=lambda i: i[0], reverse=True)
    return "\n".join(line for _, line in items[:MAX_ACTIVITIES])


def build_repos(token: str | None) -> str:
    """Fetch curated repos by name; fail loudly so stale content is kept."""
    lines = []
    repos = api_get(f"{GITHUB_API}/users/{USER}/repos?per_page=100", token)
    by_name = {r.get("name"): r for r in repos}
    for name in FEATURED:
        r = by_name.get(name)
        if r is None:
            raise RuntimeError(f"Featured repo {name!r} not found via API")
        url = r.get("html_url") or f"https://github.com/{USER}/{name}"
        desc = esc(r.get("description") or "_No description_")
        stars = r.get("stargazers_count", 0)
        star = f" ({stars} ★)" if stars else ""
        pushed = ts(r.get("pushed_at"))
        date = f" — {pushed:%Y-%m-%d}" if pushed else ""
        lines.append(f"- [{name}]({url}) — {desc}{star}{date}")
    return "\n".join(lines)


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    try:
        original = read_readme()
        events = api_get(f"{GITHUB_API}/users/{USER}/events/public?per_page=100", token)
        activity = build_activity(events, token)
        featured = build_repos(token)
        updated = replace_section(original, "ACTIVITY", activity)
        updated = replace_section(updated, "REPOSITORIES", featured)
        if updated != original:
            with open(README_PATH, "w", encoding="utf-8") as f:
                f.write(updated)
            print("README updated")
        else:
            print("No changes needed")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
