"""Weekly repo radar: trending GitHub repos vs. your project profiles, one Claude call per batch."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
PROJECTS_FILE = ROOT / "projects.md"
SEEN_FILE = ROOT / "seen.json"
LAST_RUN_FILE = ROOT / "last_run.json"

MODEL = "claude-sonnet-5-5"
BATCH_SIZE = 40
CONFIDENCE_THRESHOLD = 3
SEEN_TTL_DAYS = 90
MIN_TRENDING_REPOS = 10
HN_MAX_PAGES = 5
HN_MIN_POINTS = 80
README_CHARS = 1500

TRENDING_URLS = [
    "https://github.com/trending?since=weekly",
    "https://github.com/trending/python?since=weekly",
    "https://github.com/trending/typescript?since=weekly",
]
TRENDSHIFT_URL = "https://trendshift.io/weekly"
USER_AGENT = "repo-radar (+https://github.com/ziki17/repo-radar)"
# First path segments on github.com that are site pages, not repo owners.
RESERVED_OWNERS = {
    "about", "apps", "blog", "codespaces", "collections", "contact", "customer-stories",
    "enterprise", "events", "explore", "features", "issues", "login", "marketplace", "new",
    "notifications", "orgs", "pricing", "pulls", "readme", "resources", "search", "security",
    "settings", "site", "solutions", "sponsors", "topics", "trending", "user-attachments", "users",
}  # fmt: skip
GIST_ID = re.compile(r"^[0-9a-f]{32}$")
CATEGORIES = ["security", "replace", "pain", "capability"]
NOT_RETURNED = "not returned by model"

RESEND_URL = "https://api.resend.com/emails"
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
EMAIL_FROM = os.environ.get("EMAIL_FROM")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL")

Candidate = dict[str, Any]


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_count(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0


# ─── Collect: GitHub Trending ────────────────────────────────────────────────


def parse_trending(page_html: str) -> list[Candidate]:
    soup = BeautifulSoup(page_html, "html.parser")
    repos: list[Candidate] = []
    for row in soup.select("article.Box-row"):
        link = row.select_one("h2 a[href]")
        if not link:
            continue
        parts = [p for p in link["href"].strip("/").split("/") if p]
        if len(parts) != 2:
            continue
        desc = row.select_one("p")
        stars_link = row.select_one('a[href$="/stargazers"]')
        week = next(
            (s for s in row.select("span") if re.search(r"stars? this week", s.get_text())), None
        )
        repos.append(
            {
                "repo": "/".join(parts),
                "description": " ".join(desc.get_text().split()) if desc else "",
                "stars": parse_count(stars_link.get_text()) if stars_link else 0,
                "stars_this_week": parse_count(week.get_text()) if week else 0,
            }
        )
    return repos


def fetch_trending(session: requests.Session) -> list[Candidate]:
    by_repo: dict[str, Candidate] = {}
    for url in TRENDING_URLS:
        resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
        for repo in parse_trending(resp.text):
            by_repo.setdefault(repo["repo"].lower(), repo)
    repos = list(by_repo.values())
    if len(repos) < MIN_TRENDING_REPOS:
        raise RuntimeError(
            f"GitHub Trending parser found only {len(repos)} repos; markup probably changed"
        )
    return repos


# ─── Collect: Hacker News ────────────────────────────────────────────────────


def repo_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    if parts.netloc.lower() not in {"github.com", "www.github.com"}:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2:
        return None
    owner, name = segments[0], segments[1].removesuffix(".git")
    if owner.lower() in RESERVED_OWNERS or GIST_ID.match(name.lower()):
        return None
    return f"{owner}/{name}"


def filter_hn_hits(hits: Iterable[dict[str, Any]]) -> list[Candidate]:
    by_repo: dict[str, Candidate] = {}
    for hit in hits:
        repo = repo_from_url(hit.get("url"))
        if not repo:
            continue
        entry = {
            "repo": repo,
            "hn_points": hit.get("points") or 0,
            "hn_comments": hit.get("num_comments") or 0,
            "hn_url": f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
            "hn_title": hit.get("title") or "",
        }
        prev = by_repo.get(repo.lower())
        if not prev or entry["hn_points"] > prev["hn_points"]:
            by_repo[repo.lower()] = entry
    return list(by_repo.values())


def hn_search_url(start: int, end: int, page: int) -> str:
    # Algolia silently ignores numericFilters if > and < are not percent-encoded.
    return (
        "https://hn.algolia.com/api/v1/search?tags=story"
        f"&numericFilters=created_at_i%3E{start},created_at_i%3C{end},points%3E{HN_MIN_POINTS}"
        f"&hitsPerPage=200&page={page}"
    )


def fetch_hn(session: requests.Session, days: int, now: datetime) -> list[Candidate]:
    end = int(now.timestamp())
    start = int((now - timedelta(days=days)).timestamp())
    hits: list[dict[str, Any]] = []
    try:
        for page in range(HN_MAX_PAGES):
            resp = session.get(hn_search_url(start, end, page), timeout=30)
            resp.raise_for_status()
            page_hits = resp.json().get("hits", [])
            if not page_hits:
                break
            hits.extend(page_hits)
    except (requests.RequestException, ValueError) as e:
        # HN is a secondary source; trending alone is enough for a useful run.
        log(f"  ! HN fetch failed, continuing without it: {e}")
        return []
    log(f"  HN: {len(hits)} stories > {HN_MIN_POINTS} points")
    return filter_hn_hits(hits)


# ─── Collect: Trendshift ─────────────────────────────────────────────────────

TRENDSHIFT_WEEK_PATH = re.compile(r"^/weekly/(\d{4})/(\d+)$")


def parse_trendshift(page_html: str, page_url: str) -> list[Candidate]:
    """Read the schema.org ItemList that Trendshift embeds as JSON-LD, skipping malformed items."""
    soup = BeautifulSoup(page_html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        data = json.loads(script.string or "")
        if isinstance(data, dict) and data.get("@type") == "ItemList":
            break
    else:
        raise ValueError("no JSON-LD ItemList on the page")
    elements = data.get("itemListElement")
    repos: list[Candidate] = []
    for element in elements if isinstance(elements, list) else []:
        if not isinstance(element, dict) or not isinstance(element.get("item"), dict):
            continue
        item, rank = element["item"], element.get("position")
        url = item.get("codeRepository")
        repo = repo_from_url(url) if isinstance(url, str) else None
        if not repo or not isinstance(rank, int) or rank < 1:
            continue
        entry: Candidate = {"repo": repo, "trendshift_rank": rank, "trendshift_url": page_url}
        description = item.get("description")
        # Only a real description may override Trending's in the merge.
        if isinstance(description, str) and description.strip():
            entry["description"] = description
        repos.append(entry)
    return sorted(repos, key=lambda c: c["trendshift_rank"])


def trendshift_week_url(page_html: str) -> str | None:
    """URL of the last completed week, from the "Previous week" link on /weekly.

    /weekly shows the running week, so on Monday morning it has only hours of data. Its week
    numbers are Trendshift's own, not ISO weeks, so the link is the only reliable source.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    link = soup.select_one('a[aria-label="Previous week"][href]')
    match = TRENDSHIFT_WEEK_PATH.match(link["href"]) if link else None
    return f"{TRENDSHIFT_URL}/{match[1]}/{match[2]}" if match else None


def fetch_trendshift(session: requests.Session) -> list[Candidate]:
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = session.get(TRENDSHIFT_URL, headers=headers, timeout=30)
        resp.raise_for_status()
        url = trendshift_week_url(resp.text)
        if url:
            resp = session.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
        else:
            url = TRENDSHIFT_URL
            log("  ! Trendshift: no previous-week link, using the running week instead")
        repos = parse_trendshift(resp.text, url)
    except Exception as e:
        # Trendshift is a secondary source, like HN: nothing here may stop the run.
        log(f"  ! Trendshift fetch failed, continuing without it: {type(e).__name__}: {e}")
        return []
    if not repos:
        log(f"  ! Trendshift: no repos parsed from {url}; markup probably changed")
    log(f"  Trendshift: {len(repos)} repos from {url}")
    return repos


# ─── Merge, filter, rank ─────────────────────────────────────────────────────


def merge_candidates(
    trending: list[Candidate], trendshift: list[Candidate], hn: list[Candidate]
) -> list[Candidate]:
    """Later sources win on shared fields: Trendshift's description over Trending's, HN last."""
    merged: dict[str, Candidate] = {}
    for item in [*trending, *trendshift, *hn]:
        key = item["repo"].lower()
        existing = merged.get(key, {})
        merged[key] = {**existing, **item, "repo": existing.get("repo", item["repo"])}
    return list(merged.values())


def filter_seen(cands: list[Candidate], seen: dict[str, Any], today: date) -> list[Candidate]:
    cutoff = today - timedelta(days=SEEN_TTL_DAYS)
    recent = {
        repo.lower()
        for repo, entry in seen.items()
        if date.fromisoformat(entry.get("last_eval", "1970-01-01")) > cutoff
    }
    return [c for c in cands if c["repo"].lower() not in recent]


def rank_and_cut(cands: list[Candidate], limit: int) -> list[Candidate]:
    """Three tiers, each sorted on its own signal, filled in order until `limit`:

    1. HN repos by points (they are rare and already vetted by votes).
    2. Trendshift repos by rank (momentum sustained over the week, not a one-day spike).
    3. The rest of trending by weekly stars.

    A repo in several sources sits in its highest tier. The signals are not comparable: weekly
    trending stars run into the thousands while HN points are in the hundreds, so a combined
    score would push every HN-only repo below the cut.
    """
    hn = sorted(
        (c for c in cands if c.get("hn_url")),
        key=lambda c: (-c.get("hn_points", 0), c["repo"].lower()),
    )
    rest = [c for c in cands if not c.get("hn_url")]
    trendshift = sorted(
        (c for c in rest if c.get("trendshift_rank")),
        key=lambda c: (c["trendshift_rank"], c["repo"].lower()),
    )
    trending = sorted(
        (c for c in rest if not c.get("trendshift_rank")),
        key=lambda c: (-c.get("stars_this_week", 0), c["repo"].lower()),
    )
    return [*hn, *trendshift, *trending][:limit]


# ─── Enrich via GitHub API ───────────────────────────────────────────────────

BADGE_LINK = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)")
MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
HTML_TAG = re.compile(r"<[^>]+>")


def clean_readme(text: str) -> str:
    text = BADGE_LINK.sub("", text)
    text = MD_IMAGE.sub("", text)
    text = HTML_TAG.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)[:README_CHARS]


def github_headers(token: str | None, accept: str = "application/vnd.github+json") -> dict:
    headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def enrich(session: requests.Session, cand: Candidate, token: str | None) -> Candidate:
    base = f"https://api.github.com/repos/{cand['repo']}"
    out = dict(cand)
    try:
        resp = session.get(base, headers=github_headers(token), timeout=20)
        if resp.ok:
            meta = resp.json()
            full_name = meta.get("full_name") or cand["repo"]
            if full_name.lower() != cand["repo"].lower():
                out["original_repo"] = cand["repo"]
            out["repo"] = full_name
            out["description"] = meta.get("description") or cand.get("description", "")
            out["stars"] = meta.get("stargazers_count", cand.get("stars", 0))
            out["language"] = meta.get("language")
            out["topics"] = meta.get("topics", [])
            out["pushed_at"] = meta.get("pushed_at")
        else:
            log(f"  ! {cand['repo']}: repo API {resp.status_code}")
        resp = session.get(
            f"{base}/readme",
            headers=github_headers(token, "application/vnd.github.raw"),
            timeout=20,
        )
        if resp.ok:
            out["readme"] = clean_readme(resp.text)
    except requests.RequestException as e:
        log(f"  ! {cand['repo']}: {e}")
    return out


# ─── Match with Claude ───────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are screening open-source repositories against a developer's project \
profiles. Only report a repo as a finding if it fits one of the four categories defined in the \
profiles (security, replace, pain, capability) for a specific project listed there. Be strict: \
most repos match nothing, and an empty findings list is a normal outcome. A candidate's \
"trendshift_rank" is its position (1 = top) in Trendshift's weekly list: the repo kept gaining \
stars across the whole week rather than spiking for a day. It says nothing about fit with the \
projects. Confidence is \
1-5, where 5 means "clearly worth an hour this week" and 1 means a weak, speculative fit. Every \
input repo must appear exactly once, either in findings or in skipped. Keep "why" under 60 \
words, "next_step" to one concrete action under 30 words, and skip reasons under 15 words."""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "project": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "why": {"type": "string"},
                    "next_step": {"type": "string"},
                    "confidence": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                },
                "required": ["repo", "project", "category", "why", "next_step", "confidence"],
                "additionalProperties": False,
            },
        },
        "skipped": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"repo": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["repo", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["findings", "skipped"],
    "additionalProperties": False,
}

LLM_FIELDS = ["repo", "description", "language", "topics", "stars", "stars_this_week", "readme"]


def candidate_payload(cand: Candidate) -> dict[str, Any]:
    payload = {k: cand[k] for k in LLM_FIELDS if cand.get(k) not in (None, "", [])}
    if cand.get("hn_title"):
        payload["hn_title"] = cand["hn_title"]
        payload["hn_points"] = cand.get("hn_points", 0)
    if cand.get("trendshift_rank"):
        payload["trendshift_rank"] = cand["trendshift_rank"]
    return payload


def build_user_message(projects_md: str, batch: list[Candidate]) -> str:
    candidates = json.dumps([candidate_payload(c) for c in batch], ensure_ascii=False, indent=1)
    return (
        f"<project_profiles>\n{projects_md}\n</project_profiles>\n\n"
        f"<candidates>\n{candidates}\n</candidates>\n\n"
        f"Screen all {len(batch)} candidates."
    )


def call_claude(client: Any, projects_md: str, batch: list[Candidate]) -> dict[str, Any]:
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_message(projects_md, batch)}],
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        # Retries a safety-classifier refusal (likely on "security" items) on another model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude refused the batch: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Claude output hit max_tokens; reduce batch size")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise RuntimeError("model returned no text block")
    return json.loads(text)


def reconcile(batch: list[Candidate], result: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """Map model output back onto the input batch: each input repo lands in exactly one list."""
    canonical = {c["repo"].lower(): c["repo"] for c in batch}
    findings: list[dict] = []
    skipped: list[dict] = []
    placed: set[str] = set()
    for f in result.get("findings", []):
        key = str(f.get("repo", "")).lower()
        if key not in canonical or key in placed:
            continue
        placed.add(key)
        confidence = max(1, min(5, int(f.get("confidence", 1))))
        findings.append({**f, "repo": canonical[key], "confidence": confidence})
    for s in result.get("skipped", []):
        key = str(s.get("repo", "")).lower()
        if key not in canonical or key in placed:
            continue
        placed.add(key)
        skipped.append({"repo": canonical[key], "reason": s.get("reason", "")})
    for key, repo in canonical.items():
        if key not in placed:
            skipped.append({"repo": repo, "reason": NOT_RETURNED})
    return findings, skipped


def match_candidates(
    call: Callable[[list[Candidate]], dict[str, Any]], cands: list[Candidate]
) -> tuple[list[dict], list[dict]]:
    findings: list[dict] = []
    skipped: list[dict] = []
    for i in range(0, len(cands), BATCH_SIZE):
        batch = cands[i : i + BATCH_SIZE]
        f, s = reconcile(batch, call(batch))
        log(f"  batch {i // BATCH_SIZE + 1}: {len(batch)} repos, {len(f)} findings")
        findings.extend(f)
        skipped.extend(s)
    return findings, skipped


def split_actionable(findings: list[dict]) -> tuple[list[dict], list[dict]]:
    actionable = [f for f in findings if f["confidence"] >= CONFIDENCE_THRESHOLD]
    weak = [f for f in findings if f["confidence"] < CONFIDENCE_THRESHOLD]
    return actionable, weak


# ─── Email ───────────────────────────────────────────────────────────────────


def iso_week(today: date) -> str:
    year, week, _ = today.isocalendar()
    return f"{year}-W{week:02d}"


def email_subject(n: int, today: date) -> str:
    return f"repo-radar: {n or 'no'} findings for week {iso_week(today)}"


def render_finding(f: dict, cand: Candidate) -> str:
    e = html.escape
    repo_url = f"https://github.com/{f['repo']}"
    stats = f"&#9733; {cand.get('stars', 0):,}"
    if cand.get("stars_this_week"):
        stats += f" (+{cand['stars_this_week']:,} this week)"
    if cand.get("hn_url"):
        stats += f' &middot; <a href="{e(cand["hn_url"])}">HN {cand.get("hn_points", 0)} pts</a>'
    if cand.get("trendshift_rank"):
        ts_url = e(cand.get("trendshift_url") or TRENDSHIFT_URL)
        stats += f' &middot; <a href="{ts_url}">Trendshift #{cand["trendshift_rank"]}</a>'
    return (
        '<div style="margin:0 0 14px 0;padding:8px 10px;border-left:3px solid #2c6bed;">'
        f'<a href="{e(repo_url)}" style="font-weight:bold;">{e(f["repo"])}</a> '
        f'<span style="background:#eef;padding:1px 6px;font-size:12px;">{e(f["category"])}</span> '
        f'<span style="color:#666;font-size:12px;">confidence {f["confidence"]}/5</span><br>'
        f'<span style="color:#666;font-size:12px;">{stats}</span>'
        f'<p style="margin:6px 0;">{e(f["why"])}</p>'
        f'<p style="margin:6px 0;"><b>Next step:</b> {e(f["next_step"])}</p>'
        "</div>"
    )


def render_email(
    actionable: list[dict], also: list[dict], cands: dict[str, Candidate], today: date
) -> str:
    by_project: dict[str, list[dict]] = {}
    for f in sorted(actionable, key=lambda f: -f["confidence"]):
        by_project.setdefault(f["project"], []).append(f)
    parts = [
        '<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;'
        'max-width:720px;color:#222;line-height:1.4;">',
        f"<h2>repo-radar, week {iso_week(today)}</h2>",
    ]
    if not by_project:
        parts.append("<p>No actionable findings this week.</p>")
    for project, items in by_project.items():
        parts.append(f'<h3 style="margin-bottom:6px;">{html.escape(project)}</h3>')
        parts.extend(render_finding(f, cands.get(f["repo"].lower(), {})) for f in items)
    parts.append(
        f'<h4 style="color:#888;margin-top:28px;">Also evaluated ({len(also)})</h4>'
        '<div style="color:#888;font-size:12px;font-family:monospace;">'
    )
    parts.extend(f"{html.escape(s['repo'])} &mdash; {html.escape(s['reason'])}<br>" for s in also)
    parts.append("</div></body></html>")
    return "\n".join(parts)


def send_email(session: requests.Session, subject: str, body: str) -> bool:
    """Send the report via the Resend HTTP API. Returns True only when Resend accepted it."""
    if not RESEND_API_KEY or not RECIPIENT_EMAIL or not EMAIL_FROM:
        log("Email not configured. Set RESEND_API_KEY, RECIPIENT_EMAIL and EMAIL_FROM.")
        return False
    try:
        resp = session.post(
            RESEND_URL,
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"from": EMAIL_FROM, "to": [RECIPIENT_EMAIL], "subject": subject, "html": body},
            timeout=15,
        )
    except requests.RequestException as e:
        log(f"Failed to send email: {type(e).__name__}")
        return False
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if 200 <= resp.status_code < 300 and isinstance(data, dict) and data.get("id"):
        log(f"Email sent to {RECIPIENT_EMAIL} (Resend id {data['id']})")
        return True
    message = data.get("message") if isinstance(data, dict) else None
    log(f"Failed to send email: Resend {resp.status_code}: {message or 'no message'}")
    return False


# ─── Optional events endpoint ────────────────────────────────────────────────


def post_event(session: requests.Session, event: dict[str, Any]) -> None:
    """POST a run or finding event to DASHBOARD_URL, if configured. Never fails the run."""
    url, token = os.environ.get("DASHBOARD_URL"), os.environ.get("DASHBOARD_TOKEN")
    if not url or not token:
        return
    try:
        resp = session.post(
            f"{url.rstrip('/')}/api/events",
            json={"agent": "repo-radar", **event},
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code != 201:
            log(f"  dashboard rejected {event.get('kind')} event: {resp.status_code}")
    except requests.RequestException as e:
        log(f"  dashboard unreachable: {e}")


def finding_event(f: dict, cand: Candidate) -> dict[str, Any]:
    return {
        "kind": "finding",
        "status": "ok",
        "ref": f["repo"],
        "url": f"https://github.com/{f['repo']}",
        "project": f["project"],
        "title": f"{f['repo']}: {f['category']}",
        "summary": f["next_step"],
        "data": {"why": f["why"], "confidence": f["confidence"], "stars": cand.get("stars", 0)},
    }


# ─── State ───────────────────────────────────────────────────────────────────


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {}


def write_json(path: Path, data: Any) -> None:
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as tmp:
        tmp.write(text)
    os.replace(tmp.name, path)


def update_seen(
    seen: dict[str, Any],
    findings: list[dict],
    skipped: list[dict],
    today: date,
    aliases: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Record evaluated repos. `aliases` maps a renamed repo (lowercase) to its pre-rename name,
    which is recorded too so either name is filtered next time."""
    out = dict(seen)
    stamp = today.isoformat()
    aliases = aliases or {}

    def put(name: str, entry: dict[str, Any]) -> None:
        prior = next((v for k, v in out.items() if k.lower() == name.lower()), {})
        out.pop(next((k for k in out if k.lower() == name.lower()), name), None)
        out[name] = {**entry, "first_seen": prior.get("first_seen", stamp)}

    def record(repo: str, verdict: str, reason: str, project: str | None) -> None:
        entry = {"last_eval": stamp, "verdict": verdict, "reason": reason[:120], "project": project}
        for name in [repo, aliases.get(repo.lower())]:
            if name:
                put(name, entry)

    for f in findings:
        actionable = f["confidence"] >= CONFIDENCE_THRESHOLD
        reason = f["why"] if actionable else f"low confidence ({f['confidence']}): {f['why']}"
        record(f["repo"], "match" if actionable else "skip", reason, f["project"])
    for s in skipped:
        if s["reason"] != NOT_RETURNED:
            record(s["repo"], "skip", s["reason"], None)
    return out


# ─── Main ────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="no email, dashboard or seen.json")
    p.add_argument("--max-candidates", type=int, default=80)
    p.add_argument("--days", type=int, default=7)
    return p.parse_args(argv)


def run(args: argparse.Namespace, session: requests.Session) -> int:
    now = datetime.now(UTC)
    today = now.date()
    log(f"repo-radar {now:%Y-%m-%d %H:%M} UTC{' (dry run)' if args.dry_run else ''}")
    if not args.dry_run and not EMAIL_FROM:
        raise RuntimeError(
            "EMAIL_FROM is not set; set it to a sender on a domain verified in Resend, "
            "e.g. 'Repo Radar <radar@example.com>'"
        )

    log("1. Collecting")
    trending = fetch_trending(session)
    log(f"  trending: {len(trending)} repos")
    trendshift = fetch_trendshift(session)
    log(f"  trendshift: {len(trendshift)} repos")
    hn = fetch_hn(session, args.days, now)
    log(f"  HN: {len(hn)} GitHub repos")
    merged = merge_candidates(trending, trendshift, hn)
    log(f"  merged: {len(merged)} unique repos")

    seen = load_json(SEEN_FILE)
    fresh = filter_seen(merged, seen, today)
    log(f"2. Seen filter: {len(merged) - len(fresh)} dropped, {len(fresh)} left")

    ranked = rank_and_cut(fresh, args.max_candidates)
    token = os.environ.get("GITHUB_TOKEN")
    log(f"3. Enriching {len(ranked)} repos via GitHub API ({'auth' if token else 'anonymous'})")
    cands = [enrich(session, c, token) for c in ranked]
    for i, c in enumerate(cands[:10], 1):
        log(
            f"  {i:2}. {c['repo']}  "
            f"(hn={c.get('hn_points', 0)}, week={c.get('stars_this_week', 0)}) "
            f"lang={c.get('language')} readme={len(c.get('readme', ''))}ch"
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        if not args.dry_run:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        log("4. Matching skipped: ANTHROPIC_API_KEY not set (collection smoke test)")
        findings, skipped = [], [{"repo": c["repo"], "reason": "not evaluated"} for c in cands]
    else:
        import anthropic

        client = anthropic.Anthropic()
        projects_md = PROJECTS_FILE.read_text()
        log(f"4. Matching {len(cands)} repos with {MODEL}")
        findings, skipped = match_candidates(
            lambda batch: call_claude(client, projects_md, batch), cands
        )

    actionable, weak = split_actionable(findings)
    log(f"5. {len(findings)} findings, {len(actionable)} actionable (confidence >= 3)")

    by_key = {c["repo"].lower(): c for c in cands}
    also = [
        {"repo": f["repo"], "reason": f"low confidence ({f['confidence']}): {f['why'][:80]}"}
        for f in weak
    ] + skipped
    body = render_email(actionable, also, by_key, today)
    subject = email_subject(len(actionable), today)

    if args.dry_run:
        log("6. Dry run: email would be sent")
        log(f"--- {subject} ---")
        print(body)
        print("--- findings ---")
        print(json.dumps({"findings": findings, "skipped": skipped}, indent=2))
        return 0

    sent = send_email(session, subject, body)
    log(f"6. Email: {'sent' if sent else 'FAILED'}")

    email_failed = not sent
    write_json(LAST_RUN_FILE, {"date": today.isoformat(), "findings": findings, "skipped": skipped})
    if email_failed:
        # Persisting now would hide these findings for 90 days although nobody saw them.
        log("7. seen.json NOT updated because the email was not sent")
    else:
        aliases = {c["repo"].lower(): c["original_repo"] for c in cands if c.get("original_repo")}
        updated = update_seen(seen, findings, skipped, today, aliases)
        write_json(SEEN_FILE, updated)
        log(f"7. seen.json: {len(updated) - len(seen)} new, {len(updated)} total")

    for f in actionable:
        post_event(session, finding_event(f, by_key.get(f["repo"].lower(), {})))
    post_event(
        session,
        {
            "kind": "run",
            "status": "fail" if email_failed else "ok",
            "title": f"Scanned {len(cands)} repos, {len(actionable)} findings",
            "data": {
                "trending": len(trending),
                "trendshift": len(trendshift),
                "hn": len(hn),
                "evaluated": len(cands),
                "findings": len(findings),
                "actionable": len(actionable),
                "email_sent": sent,
            },
        },
    )
    return 1 if email_failed else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()
    session = requests.Session()
    try:
        code = run(args, session)
    except Exception as e:
        log(f"FAILED: {type(e).__name__}: {e}")
        if not args.dry_run:
            post_event(
                session,
                {
                    "kind": "run",
                    "status": "fail",
                    "title": f"Run failed: {type(e).__name__}",
                    "summary": str(e)[:500],
                },
            )
        return 1
    log(f"Done in {time.monotonic() - started:.0f}s")
    return code


if __name__ == "__main__":
    sys.exit(main())
