#!/usr/bin/env python3
"""
Update GitHub profile README with recent public activity.

Uses GitHub REST API to fetch user events, filters for meaningful
development activity, and injects into README between markers.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

README_PATH = os.path.join(os.path.dirname(__file__), "..", "README.md")
ACTIVITY_MARKER = "ACTIVITY"
MAX_ACTIVITIES = 5
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
    for marker in (ACTIVITY_MARKER,):
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


def fetch_events(token: str | None) -> list[dict[str, Any]]:
    """Fetch public events for the user from GitHub API."""
    url = f"{GITHUB_API}/users/{USER}/events/public?per_page=100"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "profile-readme-updater"}
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
            # Rate limited
            reset = e.headers.get("X-RateLimit-Reset")
            wait = int(reset) - int(datetime.now(timezone.utc).timestamp()) if reset else 60
            raise RuntimeError(f"Rate limited. Reset in ~{wait}s") from e
        raise RuntimeError(f"GitHub API error: {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e


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


def format_event(event: dict[str, Any]) -> str | None:
    """Format a single event as a markdown line."""
    etype = event.get("type")
    repo = event.get("repo", {})
    repo_name = repo.get("name", "")
    repo_url = f"https://github.com/{repo_name}"
    created = event.get("created_at", "")
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        date_str = dt.strftime("%Y-%m-%d")
    except Exception:
        date_str = created[:10] if created else "?"

    payload = event.get("payload", {})

    if etype == "PushEvent":
        commits = payload.get("commits", [])
        if not commits:
            return None
        # Use the most recent commit message
        msg = commits[-1].get("message", "").split("\n")[0].strip()
        msg = msg[:80]  # truncate long messages
        commit_sha = commits[-1].get("sha", "")[:7]
        commit_url = f"{repo_url}/commit/{commit_sha}"
        return f"- [{repo_name}]({repo_url}) — {msg} ([{commit_sha}]({commit_url})) — {date_str}"

    elif etype == "CreateEvent":
        ref_type = payload.get("ref_type")
        ref = payload.get("ref", "")
        if ref_type == "repository":
            return f"- Created [{repo_name}]({repo_url}) — {date_str}"
        elif ref_type == "branch":
            return f"- Created branch `{ref}` in [{repo_name}]({repo_url}) — {date_str}"
        elif ref_type == "tag":
            tag_url = f"{repo_url}/releases/tag/{ref}"
            return f"- Tagged [{ref}]({tag_url}) in [{repo_name}]({repo_url}) — {date_str}"
        return None

    elif etype == "ReleaseEvent":
        action = payload.get("action", "published")
        release = payload.get("release", {})
        tag = release.get("tag_name", "")
        name = release.get("name", tag)
        url = release.get("html_url", repo_url)
        return f"- {action.capitalize()} [{name}]({url}) in [{repo_name}]({repo_url}) — {date_str}"

    elif etype == "PullRequestEvent":
        action = payload.get("action", "")
        pr = payload.get("pull_request", {})
        title = pr.get("title", "")
        number = pr.get("number", "")
        url = pr.get("html_url", "")
        if action in ("opened", "closed", "merged"):
            verb = "merged" if action == "closed" and pr.get("merged") else action
            title_part = f" [{title}]({url})" if title and url else f" #{number}"
            return f"- PR{title_part} {verb} in [{repo_name}]({repo_url}) — {date_str}"
        return None

    elif etype == "IssuesEvent":
        action = payload.get("action", "")
        issue = payload.get("issue", {})
        title = issue.get("title", "")
        number = issue.get("number", "")
        url = issue.get("html_url", "")
        if action in ("opened", "closed"):
            return f"- Issue #{number} {action}: [{title}]({url}) in [{repo_name}]({repo_url}) — {date_str}"
        return None

    elif etype == "ForkEvent":
        forkee = payload.get("forkee", {})
        fork_name = forkee.get("full_name", "")
        fork_url = forkee.get("html_url", "")
        return f"- Forked [{fork_name}]({fork_url}) from [{repo_name}]({repo_url}) — {date_str}"

    return None


def build_activity_section(events: list[dict[str, Any]]) -> str:
    """Build the activity markdown section."""
    meaningful = [e for e in events if is_meaningful_event(e)]
    formatted = []
    for e in meaningful:
        line = format_event(e)
        if line:
            formatted.append(line)
        if len(formatted) >= MAX_ACTIVITIES:
            break

    if not formatted:
        return "_No recent public activity._"

    return "\n".join(formatted)


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")

    try:
        readme = read_readme()
        validate_markers(readme)

        events = fetch_events(token)
        activity_md = build_activity_section(events)

        readme = replace_section(readme, ACTIVITY_MARKER, activity_md)

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