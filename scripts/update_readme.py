#!/usr/bin/env python3
"""
Update GitHub profile README with recent public activity and repositories.

Uses GitHub REST API to fetch user events, commits, and repositories, filters for meaningful
development activity, and injects into README between markers.
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
ACTIVITY_MARKER = "ACTIVITY"
REPOSITORIES_MARKER = "REPOSITORIES"
MAX_ACTIVITIES = 4
MAX_REPOSITORIES = 4
GITHUB_API = "https://api.github.com"
USER = "Malte-Dzierzon"
REPO = "Malte-Dzierzon"  # profile repo name, to exclude self-updates


def read_readme() -> str:
    with open(README_PATH, "r", encoding="utf-8") as f:
        return f.read()


def write_readme(content: str) -> None:
    with open(README_PATH, "w", encoding="utf-8") as f:
        f.write(content)


def validate_markers(content: str) -> None:
    """Ensure each marker pair exists exactly once."""
    for marker in (ACTIVITY_MARKER, REPOSITORIES_MARKER):
        starts = content.count(f"<!-- {marker} starts -->")
        ends = content.count(f"<!-- {marker} ends -->")
        if starts != 1 or ends != 1:
            raise ValueError(f"Marker {marker!r} must appear exactly once (starts={starts}, ends={ends})")


def replace_section(content: str, marker: str, new_content: str) -> str:
    """Replace content between <!-- MARKER starts --> and <!-- MARKER ends -->."""
    pattern = re.compile(
        rf"<!-- {re.escape(marker)} starts -->.*?<!-- {re.escape(marker)} ends -->",
        re.DOTALL,
    )
    replacement = f"<!-- {marker} starts -->\n{new_content}\n<!-- {marker} ends -->"
    return pattern.sub(replacement, content)


def fetch_json(url: str, token: str | None, accept: str = "application/vnd.github+json") -> Any:
    """Fetch and parse JSON from GitHub API."""
    headers = {"Accept": accept, "User-Agent": "profile-readme-updater"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                return json.load(resp)
            else:
                raise RuntimeError(f"GitHub API returned {resp.status}")
    except urllib.error.HTTPError as e:
        if e.code == 403:
            reset = e.headers.get("X-RateLimit-Reset")
            wait = int(reset) - int(datetime.now(timezone.utc).timestamp()) if reset else 60
            raise RuntimeError(f"Rate limited. Reset in ~{wait}s") from e
        raise RuntimeError(f"GitHub API error: {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e


def fetch_events(token: str | None) -> list[dict[str, Any]]:
    """Fetch public events for the user from GitHub API."""
    url = f"{GITHUB_API}/users/{USER}/events/public?per_page=100"
    return fetch_json(url, token)


def fetch_graphql(query: str, token: str) -> Any:
    """POST a GraphQL query. Raises on network/API errors."""
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


def fetch_pinned_repositories(token: str | None) -> list[dict[str, Any]]:
    """Fetch pinned repositories via GraphQL. Returns [] without token or on error."""
    if not token:
        return []
    query = (
        f"query {{ user(login: \"{USER}\") "
        "{ pinnedItems(first: 6, types: REPOSITORY) "
        "{ nodes { ... on Repository "
        "{ name description url stargazers { totalCount } pushedAt } } } } }"
    )
    try:
        data = fetch_graphql(query, token)
        nodes = data["user"]["pinnedItems"]["nodes"]
    except Exception:
        return []
    repos = []
    for n in nodes:
        repos.append({
            "name": n.get("name", ""),
            "html_url": n.get("url", f"https://github.com/{USER}/{n.get('name', '')}"),
            "description": n.get("description"),
            "stargazers_count": (n.get("stargazers") or {}).get("totalCount", 0),
            "pushed_at": n.get("pushedAt", ""),
        })
    return [r for r in repos if r["name"] != REPO]


def fetch_repositories(token: str | None) -> list[dict[str, Any]]:
    """Fetch public repositories for the user from GitHub API."""
    # Fetch all repos without relying on API sort; we'll sort explicitly
    url = f"{GITHUB_API}/users/{USER}/repos?per_page=100"
    return fetch_json(url, token)


def fetch_commits(token: str | None) -> list[dict[str, Any]]:
    """Fetch recent commits by the user across all public repositories."""
    # Search commits by author, excluding the profile repo
    url = (
        f"{GITHUB_API}/search/commits"
        f"?q=author:{USER}+-repo:{USER}/{REPO}"
        f"&per_page=30&sort=committer-date&order=desc"
    )
    # Commits search requires a preview Accept header
    accept = "application/vnd.github.cloak-preview"
    data = fetch_json(url, token, accept=accept)
    return data.get("items", [])



def sort_repositories_by_stars(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort repositories by stars desc, then pushed_at desc."""
    def key(repo: dict[str, Any]) -> tuple:
        ts = parse_iso_timestamp(repo.get("pushed_at"))
        ts = ts if ts else datetime.min.replace(tzinfo=timezone.utc)
        return (repo.get("stargazers_count", 0), ts)

    return sorted(repos, key=key, reverse=True)


def parse_iso_timestamp(ts: str | None) -> datetime | None:
    """Parse ISO 8601 timestamp to UTC datetime. Returns None on failure."""
    if not ts:
        return None
    try:
        # Handle Z suffix and optional timezone offset
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def is_meaningful_event(event: dict[str, Any]) -> bool:
    """Filter for genuinely interesting development activity."""
    etype = event.get("type")
    # Skip profile repo self-updates
    repo_name = event.get("repo", {}).get("name", "")
    if repo_name == f"{USER}/{REPO}":
        return False
    # Skip bot commits
    actor = event.get("actor", {}).get("login", "")
    if actor.endswith("[bot]") or actor in ("github-actions[bot]", "dependabot[bot]"):
        return False
    # Keep these event types
    return etype in (
        "PushEvent",
        "CreateEvent",
        "ReleaseEvent",
        "PullRequestEvent",
        "IssuesEvent",
        "ForkEvent",
    )


def extract_commit_sha_from_event(event: dict[str, Any]) -> str | None:
    """Extract the commit SHA from a PushEvent if available."""
    if event.get("type") != "PushEvent":
        return None
    commits = event.get("payload", {}).get("commits", [])
    if not commits:
        return None
    sha = commits[-1].get("sha", "")
    return sha[:7] if sha else None


def format_event(event: dict[str, Any]) -> tuple[str | None, datetime | None, str | None]:
    """
    Format a single event as a markdown line.
    Returns (markdown_line, timestamp_utc, commit_sha_short_or_None).
    """
    etype = event.get("type")
    repo = event.get("repo", {})
    repo_name = repo.get("name", "")
    repo_url = f"https://github.com/{repo_name}"
    created = event.get("created_at", "")
    ts = parse_iso_timestamp(created)

    payload = event.get("payload", {})

    if etype == "PushEvent":
        commits = payload.get("commits", [])
        if not commits:
            return None, ts, None
        msg = commits[-1].get("message", "").split("\n")[0].strip()
        msg = msg[:80]
        commit_sha = commits[-1].get("sha", "")[:7]
        commit_url = f"{repo_url}/commit/{commit_sha}"
        line = f"- [{repo_name}]({repo_url}) — {msg} ([{commit_sha}]({commit_url})) — {ts.strftime('%Y-%m-%d') if ts else '?'}"
        return line, ts, commit_sha

    elif etype == "CreateEvent":
        ref_type = payload.get("ref_type")
        ref = payload.get("ref", "")
        if ref_type == "repository":
            line = f"- Created [{repo_name}]({repo_url}) — {ts.strftime('%Y-%m-%d') if ts else '?'}"
        elif ref_type == "branch":
            line = f"- Created branch `{ref}` in [{repo_name}]({repo_url}) — {ts.strftime('%Y-%m-%d') if ts else '?'}"
        elif ref_type == "tag":
            tag_url = f"{repo_url}/releases/tag/{ref}"
            line = f"- Tagged [{ref}]({tag_url}) in [{repo_name}]({repo_url}) — {ts.strftime('%Y-%m-%d') if ts else '?'}"
        else:
            return None, ts, None
        return line, ts, None

    elif etype == "ReleaseEvent":
        action = payload.get("action", "published")
        release = payload.get("release", {})
        tag = release.get("tag_name", "")
        name = release.get("name", tag)
        url = release.get("html_url", repo_url)
        line = f"- {action.capitalize()} [{name}]({url}) in [{repo_name}]({repo_url}) — {ts.strftime('%Y-%m-%d') if ts else '?'}"
        return line, ts, None

    elif etype == "PullRequestEvent":
        action = payload.get("action", "")
        pr = payload.get("pull_request", {})
        title = pr.get("title", "")
        number = pr.get("number", "")
        url = pr.get("html_url", "")
        if action in ("opened", "closed", "merged"):
            verb = "merged" if action == "closed" and pr.get("merged") else action
            title_part = f" [{title}]({url})" if title and url else f" #{number}"
            line = f"- PR{title_part} {verb} in [{repo_name}]({repo_url}) — {ts.strftime('%Y-%m-%d') if ts else '?'}"
            return line, ts, None
        return None, ts, None

    elif etype == "IssuesEvent":
        action = payload.get("action", "")
        issue = payload.get("issue", {})
        title = issue.get("title", "")
        number = issue.get("number", "")
        url = issue.get("html_url", "")
        if action in ("opened", "closed"):
            line = f"- Issue #{number} {action}: [{title}]({url}) in [{repo_name}]({repo_url}) — {ts.strftime('%Y-%m-%d') if ts else '?'}"
            return line, ts, None
        return None, ts, None

    elif etype == "ForkEvent":
        forkee = payload.get("forkee", {})
        fork_name = forkee.get("full_name", "")
        fork_url = forkee.get("html_url", "")
        line = f"- Forked [{fork_name}]({fork_url}) from [{repo_name}]({repo_url}) — {ts.strftime('%Y-%m-%d') if ts else '?'}"
        return line, ts, None

    return None, ts, None


def format_commit(commit: dict[str, Any]) -> tuple[str | None, datetime | None, str | None]:
    """
    Format a commit from search API as a markdown line.
    Returns (markdown_line, timestamp_utc, commit_sha_short).
    """
    sha = commit.get("sha", "")
    commit_url = commit.get("html_url", "")
    repo = commit.get("repository", {})
    repo_name = repo.get("full_name", "")
    repo_url = repo.get("html_url", f"https://github.com/{repo_name}")
    message = commit.get("commit", {}).get("message", "").split("\n")[0].strip()
    message = message[:80]
    date_str = commit.get("commit", {}).get("committer", {}).get("date", "")
    ts = parse_iso_timestamp(date_str)

    # Exclude profile repo
    if repo_name == f"{USER}/{REPO}":
        return None, ts, None

    short_sha = sha[:7]
    line = f"- [{repo_name}]({repo_url}) — {message} ([{short_sha}]({commit_url})) — {ts.strftime('%Y-%m-%d') if ts else '?'}"
    return line, ts, short_sha


def build_activity_section(events: list[dict[str, Any]], commits: list[dict[str, Any]]) -> str:
    """Build the activity markdown section from events and commits."""
    meaningful = [e for e in events if is_meaningful_event(e)]

    # Collect all items with their timestamps and SHAs for proper sorting and dedup
    items: list[tuple[str, datetime, str | None]] = []

    # Format events
    for e in meaningful:
        line, ts, sha = format_event(e)
        if line and ts:
            items.append((line, ts, sha))

    # Format commits
    for c in commits:
        line, ts, sha = format_commit(c)
        if line and ts:
            items.append((line, ts, sha))

    # Sort by timestamp descending (newest first)
    items.sort(key=lambda x: x[1], reverse=True)

    # Deduplicate by commit SHA (prefer event entry over commit search if same SHA)
    seen_shas = set()
    deduped = []
    for line, ts, sha in items:
        if sha and sha in seen_shas:
            continue
        if sha:
            seen_shas.add(sha)
        deduped.append((line, ts))

    # Suppress branch creation noise unless it's the only thing
    has_other = any(not l.startswith("- Created branch") for l, _ in deduped)
    if has_other:
        deduped = [(l, ts) for l, ts in deduped if not l.startswith("- Created branch")]

    # Take top MAX_ACTIVITIES
    top_items = deduped[:MAX_ACTIVITIES]

    if not top_items:
        return "_No recent public activity._"

    return "\n".join(line for line, _ in top_items)


def filter_repositories(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter out forks, archived repos, and the profile repo itself."""
    filtered = []
    for r in repos:
        if r.get("fork"):
            continue
        if r.get("archived"):
            continue
        if r.get("name") == REPO:
            continue
        filtered.append(r)
    return filtered


def sort_repositories_by_pushed_at(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort repositories by pushed_at descending (newest first)."""
    def get_push_time(repo: dict[str, Any]) -> datetime:
        ts = parse_iso_timestamp(repo.get("pushed_at"))
        # Use epoch as fallback so repos with missing/invalid timestamps sort to bottom
        return ts if ts else datetime.min.replace(tzinfo=timezone.utc)

    return sorted(repos, key=get_push_time, reverse=True)


def format_repository(repo: dict[str, Any]) -> str:
    """Format a single repository as a markdown line."""
    name = repo.get("name", "")
    url = repo.get("html_url", f"https://github.com/{USER}/{name}")
    description = repo.get("description") or "_No description_"
    stars = repo.get("stargazers_count", 0)
    pushed_at = repo.get("pushed_at", "")
    ts = parse_iso_timestamp(pushed_at)
    date_str = ts.strftime("%Y-%m-%d") if ts else "?"

    parts = []
    parts.append(f"[{name}]({url})")
    if description:
        parts.append(f"— {description}")
    if stars:
        parts.append(f"⭐ {stars}")
    parts.append(f"— {date_str}")

    return f"- {' '.join(parts)}"


def build_repositories_section(repos: list[dict[str, Any]], pinned: list[dict[str, Any]]) -> str:
    """Build the repositories markdown section: pinned first, stars fallback fills up."""
    chosen = list(pinned[:MAX_REPOSITORIES])
    if len(chosen) < MAX_REPOSITORIES:
        filtered = filter_repositories(repos)
        pinned_names = {r.get("name") for r in chosen}
        rest = [r for r in filtered if r.get("name") not in pinned_names]
        chosen += sort_repositories_by_stars(rest)[:MAX_REPOSITORIES - len(chosen)]
    if not chosen:
        return "_No public repositories._"

    formatted = []
    for r in chosen:
        formatted.append(format_repository(r))

    return "\n".join(formatted)


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")

    try:
        readme = read_readme()
        validate_markers(readme)

        # Fetch events, repositories, commits, and pinned repos
        events = fetch_events(token)
        repos = fetch_repositories(token)
        commits = fetch_commits(token)
        pinned = fetch_pinned_repositories(token)

        activity_md = build_activity_section(events, commits)
        repos_md = build_repositories_section(repos, pinned)

        readme = replace_section(readme, ACTIVITY_MARKER, activity_md)
        readme = replace_section(readme, REPOSITORIES_MARKER, repos_md)

        if readme != open(README_PATH, "r", encoding="utf-8").read():
            write_readme(readme)
            print("README updated")
            return 0
        else:
            print("No changes needed")
            return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        # Fail safely - don't destroy existing README
        return 1


if __name__ == "__main__":
    sys.exit(main())