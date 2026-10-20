"""Regression tests for the profile README generator.

All external API access is faked (FakeGw routes URL substrings to canned
values, or raises ApiError); HTTP-layer tests monkeypatch urllib. No network.
"""

from __future__ import annotations

import json
import re
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

import update_readme as ur

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sha(char: str = "a") -> str:
    return char * 40


README = """\
# Xealom

<table>
<!-- ACTIVITY starts -->
- stale activity line
<!-- ACTIVITY ends -->
<!-- REPOSITORIES starts -->
- stale repo line
<!-- REPOSITORIES ends -->
</table>
"""


def write_readme(tmp_path: Path, content: str = README) -> Path:
    p = tmp_path / "README.md"
    p.write_text(content, encoding="utf-8")
    return p


def write_config(tmp_path: Path, entries: list) -> Path:
    p = tmp_path / "featured.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    return p


def repo(name: str, rid: int, desc: str = "desc", stars: int = 0,
         pushed: str = "2026-01-02T03:04:05Z") -> dict:
    return {
        "id": rid,
        "name": name,
        "html_url": f"https://github.com/Malte-Dzierzon/{name}",
        "description": desc,
        "stargazers_count": stars,
        "pushed_at": pushed,
    }


def push_event(commit_sha: str, created: str, actor: str = "Malte-Dzierzon",
               slug: str = "user/proj") -> dict:
    return {
        "id": f"evt-{commit_sha[:7]}",
        "type": "PushEvent",
        "actor": {"login": actor},
        "repo": {"name": slug},
        "payload": {"head": commit_sha},
        "created_at": created,
    }


DETAIL = {
    "author": {"login": "Malte-Dzierzon"},
    "commit": {"message": "feat: something important"},
}


class FakeGw:
    """Routes URL substrings to canned values; substrings in fail_on raise."""

    def __init__(self, responses: dict[str, object] | None = None,
                 fail_on: list[str] | None = None):
        self.responses = responses or {}
        self.fail_on = fail_on or []
        self.calls: list[str] = []

    def get(self, url: str, token: str | None = None):
        self.calls.append(url)
        for pat in self.fail_on:
            if pat in url:
                raise ur.ApiError(f"HTTP 404 Not Found for {url}")
        for pat, value in self.responses.items():
            if pat in url:
                return value
        raise ur.ApiError(f"unrouted GET {url}")


class FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.github.com/x", code, "boom", headers or {}, None)


# ---------------------------------------------------------------------------
# Featured repositories: rename, deletion, unavailability
# ---------------------------------------------------------------------------

def test_featured_rename_does_not_break_run(tmp_path):
    readme = write_readme(tmp_path)
    cfg = write_config(tmp_path, [{"name": "proj", "id": 424242}])
    gw = FakeGw({
        "/users/Malte-Dzierzon/repos?per_page=100": [repo("comet-proj", 424242, desc="moved")],
    })
    assert ur.run_update(config_path=cfg, readme_path=readme, gw=gw)
    out = readme.read_text()
    assert "[comet-proj](https://github.com/Malte-Dzierzon/comet-proj)" in out
    assert "[proj]" not in out
    ur.ensure_sections(out)


def test_featured_deleted_repo_skips_with_warning(tmp_path, capsys):
    readme = write_readme(tmp_path)
    cfg = write_config(tmp_path, [{"name": "alive", "id": 1}, {"name": "gone", "id": 2}])
    gw = FakeGw({"/users/Malte-Dzierzon/repos?per_page=100": [repo("alive", 1)]})
    assert ur.run_update(config_path=cfg, readme_path=readme, gw=gw)
    out = readme.read_text()
    assert "[alive](" in out
    assert "gone" not in out
    ur.ensure_sections(out)
    err = capsys.readouterr().err
    assert "WARNING" in err and "gone" in err


def test_nameonly_config_resolves_via_redirect(tmp_path):
    # Real urllib follows the rename redirect of /repos/{owner}/{old-name};
    # the fake models the endpoint it lands on.
    readme = write_readme(tmp_path)
    cfg = write_config(tmp_path, [{"name": "old-name"}])
    gw = FakeGw({
        "/users/Malte-Dzierzon/repos?per_page=100": [repo("unrelated", 9)],
        "/repos/Malte-Dzierzon/old-name": repo("new-name", 7, desc="after rename"),
    })
    assert ur.run_update(config_path=cfg, readme_path=readme, gw=gw)
    out = readme.read_text()
    assert "[new-name](https://github.com/Malte-Dzierzon/new-name)" in out


def test_all_featured_unresolved_keeps_previous_section(tmp_path, capsys):
    readme = write_readme(tmp_path)
    cfg = write_config(tmp_path, [{"name": "vanished"}])
    gw = FakeGw({
        "/users/Malte-Dzierzon/repos?per_page=100": [],
        "/events/public?per_page=100": [],
    })
    assert not ur.run_update(config_path=cfg, readme_path=readme, gw=gw)
    out = readme.read_text()
    assert "- stale repo line" in out and "- stale activity line" in out
    err = capsys.readouterr().err
    assert err.count("WARNING") >= 2


def test_featured_repo_missing_fields_skips_line(tmp_path, capsys):
    readme = write_readme(tmp_path)
    cfg = write_config(tmp_path, [{"name": "weird", "id": 1}])
    gw = FakeGw({"/users/Malte-Dzierzon/repos?per_page=100": [{"id": 1}]})
    assert not ur.run_update(config_path=cfg, readme_path=readme, gw=gw)
    assert "- stale repo line" in readme.read_text()
    assert "no usable name" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Activity feed: dedupe, ordering, malformed events
# ---------------------------------------------------------------------------

def test_activity_dedupes_and_orders_deterministically():
    e_new = push_event(sha("a"), "2026-02-01T10:00:00Z")
    e_old = push_event(sha("b"), "2026-01-31T10:00:00Z")
    events = [e_new, e_new, e_old, e_new]  # duplicates + unordered
    gw = FakeGw({
        "/events/public?per_page=100": events,
        f"/commits/{sha('a')}": {"author": {"login": "Malte-Dzierzon"},
                                 "commit": {"message": "msg for aaaa"}},
        f"/commits/{sha('b')}": {"author": {"login": "Malte-Dzierzon"},
                                 "commit": {"message": "msg for bbbb"}},
    })
    section = ur.build_activity(gw)
    lines = (section or "").split("\n")
    assert len(lines) == 2
    assert "msg for aaaa" in lines[0] and lines[0].startswith("- [user/proj]")
    assert "msg for bbbb" in lines[1]
    # deterministic across re-runs
    again = ur.build_activity(FakeGw(gw.responses))
    assert again == section


def test_activity_malformed_events_skipped_with_warning(capsys):
    events = [
        {"id": "e1", "type": "PushEvent", "actor": {"login": "Malte-Dzierzon"},
         "repo": {"name": "user/p"}, "payload": {"head": sha("a")},
         "created_at": "not-a-date"},
        {"id": "e2", "type": "PushEvent", "actor": {"login": "Malte-Dzierzon"},
         "repo": {"name": "user/p"}, "payload": {"head": sha("b")},
         "created_at": "2026-02-01T10:00:00Z"},
        {"id": "e3", "type": "PullRequestEvent", "actor": {"login": "Malte-Dzierzon"},
         "repo": {"name": "user/p"}, "payload": {"action": "opened", "pull_request": {}},
         "created_at": "2026-02-01T10:00:00Z"},
        {"id": "e4", "type": "IssuesEvent", "actor": {"login": "Malte-Dzierzon"},
         "repo": {"name": "user/p"}, "payload": {"action": "opened", "issue": {}},
         "created_at": "2026-02-01T10:00:00Z"},
        {"id": "e5", "type": "PushEvent", "actor": {"login": "Malte-Dzierzon"},
         "repo": {"name": "user/p"}, "payload": {"head": "main"},
         "created_at": "2026-02-01T10:00:00Z"},
        "junk-not-a-dict",
    ]
    gw = FakeGw({
        "/events/public?per_page=100": events,
        f"/commits/{sha('b')}": {"author": {"login": "someone[bot]"},
                                 "commit": {"message": "bot commit"}},
    })
    section = ur.build_activity(gw)
    assert section is None
    err = capsys.readouterr().err
    assert "created_at" in err
    assert "skipped" in err


def test_bot_actor_and_profile_repo_excluded():
    bot_event = {"id": "1", "type": "PushEvent", "actor": {"login": "dependabot[bot]"},
                 "repo": {"name": "user/p"}, "payload": {"head": sha()}}
    own = {"id": "2", "type": "PushEvent", "actor": {"login": "Malte-Dzierzon"},
           "repo": {"name": ur.PROFILE_REPO}, "payload": {"head": sha()}}
    assert not ur.relevant_event(bot_event)
    assert not ur.relevant_event(own)
    assert not ur.relevant_event("junk")


def test_push_branch_head_not_sha_skips(capsys):
    ev = push_event("main", "2026-02-01T10:00:00Z")
    ev["payload"]["head"] = "main"  # branch name, not SHA
    line = ur.format_event(ev, FakeGw({}))
    assert line is None
    assert "not a commit SHA" in capsys.readouterr().err


def test_commit_markdown_is_escaped():
    detail = {"author": {"login": "Malte-Dzierzon"},
              "commit": {"message": "x -> [y](http://e.l) <svg> <!--"}}
    line = ur.fmt_commit_line("user/proj", sha(), datetime(2026, 1, 1, tzinfo=timezone.utc), detail)
    assert line is not None
    assert "\\[" in line and "\\]" in line and "\\<" in line
    # no UNESCAPED tag/comment openings (< not preceded by a backslash)
    assert not re.search(r"(?<![\\])<(?:!--|svg)", line)


def test_esc_basics():
    assert ur.esc("a\nb\tc  d") == "a b c d"
    assert ur.esc(None) == ""
    assert ur.esc(3.14) == ""
    s = ur.esc("pipe | gone")
    assert "\\|" in s


# ---------------------------------------------------------------------------
# Endpoint-level failures: recoverable, section kept
# ---------------------------------------------------------------------------

def test_events_endpoint_failure_keeps_activity(tmp_path, capsys):
    readme = write_readme(tmp_path)
    cfg = write_config(tmp_path, [{"name": "keepme", "id": 5}])
    gw = FakeGw({
        "/users/Malte-Dzierzon/repos?per_page=100": [repo("keepme", 5)],
    }, fail_on=["/events/public"])
    assert ur.run_update(config_path=cfg, readme_path=readme, gw=gw)
    out = readme.read_text()
    assert "- stale activity line" in out
    assert "[keepme](" in out
    err = capsys.readouterr().err
    assert "WARNING" in err and "ACTIVITY" in err


def test_empty_events_keeps_activity_section(tmp_path, capsys):
    readme = write_readme(tmp_path)
    cfg = write_config(tmp_path, [{"name": "r", "id": 8}])
    gw = FakeGw({
        "/events/public?per_page=100": [],
        "/users/Malte-Dzierzon/repos?per_page=100": [repo("r", 8)],
    })
    assert ur.run_update(config_path=cfg, readme_path=readme, gw=gw)
    assert "- stale activity line" in readme.read_text()
    assert "WARNING" in capsys.readouterr().err


def test_events_non_list_raises_api_error():
    gw = FakeGw({"/events/public?per_page=100": {"message": "surprise"}})
    with pytest.raises(ur.ApiError):
        ur.build_activity(gw)


def test_repos_non_list_keeps_featured(tmp_path, capsys):
    readme = write_readme(tmp_path)
    cfg = write_config(tmp_path, [{"name": "r", "id": 3}])
    gw = FakeGw({
        "/users/Malte-Dzierzon/repos?per_page=100": {"message": "nope"},
        "/events/public?per_page=100": [],
    })
    assert not ur.run_update(config_path=cfg, readme_path=readme, gw=gw)
    assert "- stale repo line" in readme.read_text()
    assert "expected a JSON list" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# README structure: markers and config are critical
# ---------------------------------------------------------------------------

def test_missing_marker_is_critical_and_writes_nothing(tmp_path):
    readme = write_readme(tmp_path, "# no markers here\n")
    cfg = write_config(tmp_path, [{"name": "r", "id": 1}])
    gw = FakeGw({"/users/Malte-Dzierzon/repos?per_page=100": [repo("r", 1)]})
    with pytest.raises(ur.CriticalError):
        ur.run_update(config_path=cfg, readme_path=readme, gw=gw)
    assert readme.read_text() == "# no markers here\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_duplicated_marker_is_critical(tmp_path):
    content = README.replace(
        "<!-- ACTIVITY ends -->", "<!-- ACTIVITY ends -->\n<!-- ACTIVITY ends -->"
    )
    readme = write_readme(tmp_path, content)
    cfg = write_config(tmp_path, [{"name": "r", "id": 1}])
    gw = FakeGw({"/users/Malte-Dzierzon/repos?per_page=100": [repo("r", 1)]})
    with pytest.raises(ur.CriticalError):
        ur.run_update(config_path=cfg, readme_path=readme, gw=gw)
    assert readme.read_text() == content


def test_invalid_config_is_critical(tmp_path):
    readme = write_readme(tmp_path)
    cfg = write_config(tmp_path, ["not-a-dict"])
    gw = FakeGw({"/users/Malte-Dzierzon/repos?per_page=100": [repo("r", 1)]})
    with pytest.raises(ur.CriticalError):
        ur.run_update(config_path=cfg, readme_path=readme, gw=gw)


def test_config_without_entries_is_critical(tmp_path):
    readme = write_readme(tmp_path)
    cfg = write_config(tmp_path, [])
    with pytest.raises(ur.CriticalError):
        ur.load_featured(cfg)


# ---------------------------------------------------------------------------
# HTTP layer: mapping, retry, backoff, Retry-After
# ---------------------------------------------------------------------------

def test_request_maps_404_to_definitive(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: (_ for _ in ()).throw(http_error(404)))
    with pytest.raises(ur._Definitive):
        ur._request("https://api.github.com/x", None)


def test_request_maps_429_to_transient_with_retry_after(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout: (_ for _ in ()).throw(http_error(429, {"Retry-After": "7"})))
    with pytest.raises(ur._Transient) as info:
        ur._request("https://api.github.com/x", None)
    assert info.value.after == 7.0


def test_request_maps_403_and_500_to_transient(monkeypatch):
    for code in (403, 500, 502):
        monkeypatch.setattr(
            urllib.request, "urlopen",
            lambda req, timeout, c=code: (_ for _ in ()).throw(http_error(c)))
        with pytest.raises(ur._Transient):
            ur._request("https://api.github.com/x", None)


def test_request_maps_urlerror_to_transient(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError("conn refused")))
    with pytest.raises(ur._Transient):
        ur._request("https://api.github.com/x", None)


def test_request_malformed_body_is_transient(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: FakeResp(b"<html>proxy</html>"))
    with pytest.raises(ur._Transient):
        ur._request("https://api.github.com/x", None)
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: FakeResp(b"[1, 2]"))
    assert ur._request("https://api.github.com/x", None) == [1, 2]


def test_gateway_retries_transient_then_succeeds():
    sleeps: list[float] = []
    calls: list[str] = []

    def fetch(url, token):
        calls.append(url)
        state = len(calls)
        if state < 3:
            raise ur._Transient(f"HTTP 429 Too Many Requests", 2.0)
        return {"ok": True}

    gw = ur.Gateway(fetch=fetch, sleep=sleeps.append)
    assert gw.get("https://api.github.com/x") == {"ok": True}
    assert len(calls) == 3
    assert sleeps == [2.0, 2.0]  # Retry-After honored over base backoff


def test_gateway_exhausts_transient_errors():
    sleeps: list[float] = []
    calls: list[str] = []

    def fetch(url, token):
        calls.append(url)
        raise ur._Transient("HTTP 500 Internal Server Error", None)

    gw = ur.Gateway(fetch=fetch, sleep=sleeps.append)
    with pytest.raises(ur.ApiError) as info:
        gw.get("https://api.github.com/x")
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]  # exponential backoff
    assert "after 3 attempts" in str(info.value)


def test_gateway_definitive_error_does_not_retry():
    calls: list[str] = []

    def fetch(url, token):
        calls.append(url)
        raise ur._Definitive("HTTP 404 Not Found")

    gw = ur.Gateway(fetch=fetch, sleep=lambda s: (_ for _ in ()).throw(AssertionError("no sleep")))
    with pytest.raises(ur.ApiError):
        gw.get("https://api.github.com/x")
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# End-to-end idempotency
# ---------------------------------------------------------------------------

def test_idempotent_double_run(tmp_path):
    readme = write_readme(tmp_path)
    cfg = write_config(tmp_path, [{"name": "a", "id": 1}, {"name": "b", "id": 2}])
    responses = {
        "/events/public?per_page=100": [push_event(sha("a"), "2026-02-01T10:00:00Z")],
        "/users/Malte-Dzierzon/repos?per_page=100": [repo("a", 1), repo("b", 2)],
        f"/commits/{sha('a')}": DETAIL,
    }
    assert ur.run_update(config_path=cfg, readme_path=readme, gw=FakeGw(responses))
    first = readme.read_text()
    assert "- [user/proj](https://github.com/user/proj) — feat: something important" in first
    assert "- [a](https://github.com/Malte-Dzierzon/a) — desc" in first
    ur.ensure_sections(first)
    assert not ur.run_update(config_path=cfg, readme_path=readme, gw=FakeGw(responses))
    assert readme.read_text() == first
    assert not list(tmp_path.glob("*.tmp"))
