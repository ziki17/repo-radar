import json
from datetime import date

import pytest

import radar

TRENDING_HTML = """
<html><body>
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/acme/rocket" class="Link">acme / rocket</a></h2>
  <p class="col-9 color-fg-muted my-1 pr-4">Fast   rockets for everyone</p>
  <div class="f6 color-fg-muted mt-2">
    <span itemprop="programmingLanguage">Python</span>
    <a href="/acme/rocket/stargazers" class="Link">12,345</a>
    <a href="/acme/rocket/forks" class="Link">678</a>
    <span class="d-inline-block float-sm-right">2,001 stars this week</span>
  </div>
</article>
<article class="Box-row">
  <h2><a href="/Owner/NoDesc">Owner / NoDesc</a></h2>
  <div>
    <a href="/Owner/NoDesc/stargazers">99</a>
    <span class="d-inline-block float-sm-right">12 stars this week</span>
  </div>
</article>
<article class="Box-row">
  <h2><a href="/sponsors/someone">not a repo</a></h2>
</article>
</body></html>
"""


def cand(repo: str, **kw) -> dict:
    return {"repo": repo, **kw}


def test_parse_trending_extracts_repo_description_and_stars():
    rows = radar.parse_trending(TRENDING_HTML)
    assert [r["repo"] for r in rows[:2]] == ["acme/rocket", "Owner/NoDesc"]
    assert rows[0]["description"] == "Fast rockets for everyone"
    assert rows[0]["stars"] == 12345
    assert rows[0]["stars_this_week"] == 2001
    assert rows[1]["description"] == ""
    assert rows[1]["stars_this_week"] == 12


class FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        pass


class FakeSession:
    def __init__(self, text: str):
        self.text = text

    def get(self, *args, **kwargs):
        return FakeResponse(self.text)


def test_fetch_trending_raises_when_parser_finds_fewer_than_ten_repos():
    with pytest.raises(RuntimeError, match="markup probably changed"):
        radar.fetch_trending(FakeSession(TRENDING_HTML))


def test_fetch_trending_accepts_ten_or_more_repos():
    rows = "".join(
        f'<article class="Box-row"><h2><a href="/o/r{i}">x</a></h2></article>' for i in range(10)
    )
    assert len(radar.fetch_trending(FakeSession(rows))) == 10


def test_filter_hn_hits_keeps_only_github_repo_urls():
    hits = [
        {
            "url": "https://github.com/acme/rocket?utm=hn#readme",
            "points": 300,
            "objectID": "1",
            "num_comments": 40,
            "title": "Show HN: Rocket",
        },
        {"url": "https://github.com/acme/rocket/tree/main", "points": 120, "objectID": "2"},
        {"url": "https://gist.github.com/bob/0123456789abcdef0123456789abcdef", "points": 90},
        {"url": "https://github.com/bob/0123456789abcdef0123456789abcdef", "points": 90},
        {"url": "https://github.com/acme", "points": 100},
        {"url": "https://github.com/sponsors/acme", "points": 100},
        {"url": "https://example.com/acme/rocket", "points": 500},
        {"url": None, "points": 500},
    ]
    kept = radar.filter_hn_hits(hits)
    assert len(kept) == 1
    assert kept[0] == {
        "repo": "acme/rocket",
        "hn_points": 300,
        "hn_comments": 40,
        "hn_url": "https://news.ycombinator.com/item?id=1",
        "hn_title": "Show HN: Rocket",
    }


def test_hn_search_url_percent_encodes_comparison_operators():
    url = radar.hn_search_url(100, 200, 3)
    assert "created_at_i%3E100" in url and "created_at_i%3C200" in url and "points%3E80" in url
    assert ">" not in url and "<" not in url
    assert url.endswith("page=3")


# Trimmed from the live /weekly page: nav links plus the JSON-LD ItemList, out of rank order.
TRENDSHIFT_NAV = '<a aria-label="Previous week" href="/weekly/2026/41"><svg></svg></a>'
TRENDSHIFT_HTML = (
    '<html><body><a href="/weekly">Weekly</a><a href="/weekly/2026/42">Permalink</a>'
    + TRENDSHIFT_NAV
    + '<script type="application/ld+json">{"@context":"https://schema.org","@type":"ItemList",'
    '"numberOfItems":3,"itemListElement":['
    '{"@type":"ListItem","position":3,"item":{"@type":"SoftwareSourceCode","name":"ts/third",'
    '"description":"Voice cloning & TTS","codeRepository":"https://github.com/ts/third"}},'
    '{"@type":"ListItem","position":2,"item":{"@type":"SoftwareSourceCode","name":"ts/away",'
    '"description":"Not on GitHub","codeRepository":"https://gitlab.com/ts/away"}},'
    '{"@type":"ListItem","position":1,"item":{"@type":"SoftwareSourceCode","name":"ts/first",'
    '"description":"Reverse engineer anything","codeRepository":"https://github.com/ts/first",'
    '"programmingLanguage":"TypeScript"}}]}</script></body></html>'
)
TRENDSHIFT_WEEK_URL = "https://trendshift.io/weekly/2026/41"


def item_list(elements: str) -> str:
    return (
        '<script type="application/ld+json">'
        f'{{"@type":"ItemList","itemListElement":{elements}}}</script>'
    )


def test_parse_trendshift_reads_json_ld_in_rank_order_and_skips_non_github():
    url = TRENDSHIFT_WEEK_URL
    assert radar.parse_trendshift(TRENDSHIFT_HTML, url) == [
        {
            "repo": "ts/first",
            "description": "Reverse engineer anything",
            "trendshift_rank": 1,
            "trendshift_url": url,
        },
        {
            "repo": "ts/third",
            "description": "Voice cloning & TTS",
            "trendshift_rank": 3,
            "trendshift_url": url,
        },
    ]


def test_parse_trendshift_skips_malformed_items_and_empty_descriptions():
    page = item_list(
        '["junk",'
        '{"position":"1","item":{"codeRepository":"https://github.com/a/str-pos"}},'
        '{"position":null,"item":{"codeRepository":"https://github.com/a/null-pos"}},'
        '{"position":2,"item":"https://github.com/a/item-str"},'
        '{"position":3,"item":{"codeRepository":["https://github.com/a/list"]}},'
        '{"position":4,"item":{"codeRepository":"https://github.com/a/ok","description":""}}]'
    )
    assert radar.parse_trendshift(page, "u") == [
        {"repo": "a/ok", "trendshift_rank": 4, "trendshift_url": "u"}
    ]
    assert radar.parse_trendshift(item_list('{"position":1}'), "u") == []


def test_trendshift_week_url_follows_only_the_previous_week_link():
    assert radar.trendshift_week_url(TRENDSHIFT_HTML) == TRENDSHIFT_WEEK_URL
    assert radar.trendshift_week_url(TRENDSHIFT_HTML.replace(TRENDSHIFT_NAV, "")) is None


class TrendshiftSession:
    """Serves pages by URL and records requests; an Exception value is raised instead."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return FakeResponse(page)


def test_fetch_trendshift_reads_the_completed_week_not_the_running_one():
    running = TRENDSHIFT_NAV + item_list(
        '[{"position":1,"item":{"codeRepository":"https://github.com/running/week"}}]'
    )
    session = TrendshiftSession(
        {radar.TRENDSHIFT_URL: running, TRENDSHIFT_WEEK_URL: TRENDSHIFT_HTML}
    )
    repos = radar.fetch_trendshift(session)
    assert session.urls == [radar.TRENDSHIFT_URL, TRENDSHIFT_WEEK_URL]
    assert [r["repo"] for r in repos] == ["ts/first", "ts/third"]


def test_fetch_trendshift_falls_back_to_running_week_without_a_week_link(capsys):
    session = TrendshiftSession({radar.TRENDSHIFT_URL: TRENDSHIFT_HTML.replace(TRENDSHIFT_NAV, "")})
    repos = radar.fetch_trendshift(session)
    assert [r["repo"] for r in repos] == ["ts/first", "ts/third"]
    assert repos[0]["trendshift_url"] == radar.TRENDSHIFT_URL
    assert session.urls == [radar.TRENDSHIFT_URL]
    assert "! Trendshift: no previous-week link" in capsys.readouterr().out


@pytest.mark.parametrize(
    "week_page",
    [
        radar.requests.ConnectionError("down"),
        RuntimeError("unexpected"),
        "<html><body>no structured data</body></html>",
        '<script type="application/ld+json">{"@type": "ItemList",</script>',
        item_list('[{"position":"1","item":"https://github.com/a/b"}]'),
    ],
    ids=["network-error", "other-error", "no-item-list", "bad-json", "unexpected-shape"],
)
def test_fetch_trendshift_returns_empty_list_on_failure(week_page, capsys):
    session = TrendshiftSession(
        {radar.TRENDSHIFT_URL: TRENDSHIFT_HTML, TRENDSHIFT_WEEK_URL: week_page}
    )
    assert radar.fetch_trendshift(session) == []
    assert "  ! Trendshift" in capsys.readouterr().out


def test_fetch_trendshift_returns_empty_list_when_index_page_is_down():
    session = TrendshiftSession({radar.TRENDSHIFT_URL: radar.requests.ConnectionError("down")})
    assert radar.fetch_trendshift(session) == []


def test_filter_seen_exact_cutoff_day_is_re_evaluated():
    today = date(2026, 10, 5)
    seen = {
        "a/day89": {"last_eval": "2026-07-08"},
        "a/day90": {"last_eval": "2026-07-07"},
    }
    kept = radar.filter_seen([cand("a/day89"), cand("a/day90")], seen, today)
    assert [c["repo"] for c in kept] == ["a/day90"]


def test_repo_from_url_normalises_git_suffix_and_host_case():
    assert radar.repo_from_url("https://GitHub.COM/Acme/Rocket.git") == "Acme/Rocket"
    assert radar.repo_from_url("https://www.github.com/acme/rocket/") == "acme/rocket"
    for reserved in ["readme", "codespaces", "resources", "solutions", "user-attachments"]:
        assert radar.repo_from_url(f"https://github.com/{reserved}/x") is None


def test_filter_seen_drops_only_repos_evaluated_within_90_days():
    today = date(2026, 10, 5)
    seen = {
        "acme/Recent": {"last_eval": "2026-08-01"},
        "acme/old": {"last_eval": "2026-07-06"},
    }
    cands = [cand("acme/recent"), cand("acme/old"), cand("acme/new")]
    assert [c["repo"] for c in radar.filter_seen(cands, seen, today)] == ["acme/old", "acme/new"]


def test_merge_combines_all_sources_case_insensitively():
    trending = [cand("Acme/Rocket", stars=10, stars_this_week=5, description="d")]
    trendshift = [cand("ACME/rocket", description="ts", trendshift_rank=4), cand("t/s")]
    hn = [cand("acme/rocket", hn_points=200, hn_url="u"), cand("x/y", hn_points=90)]
    merged = {c["repo"]: c for c in radar.merge_candidates(trending, trendshift, hn)}
    assert set(merged) == {"Acme/Rocket", "t/s", "x/y"}
    assert merged["Acme/Rocket"]["stars_this_week"] == 5
    assert merged["Acme/Rocket"]["description"] == "ts"
    assert merged["Acme/Rocket"]["trendshift_rank"] == 4
    assert merged["Acme/Rocket"]["hn_points"] == 200


def test_rank_and_cut_keeps_hn_repos_first_then_fills_with_trending():
    cands = [
        cand("t/huge", stars_this_week=20000),
        cand("t/small", stars_this_week=10),
        cand("h/low", hn_points=90, hn_url="u1"),
        cand("t/big", stars_this_week=5000),
        cand("h/both", hn_points=150, hn_url="u2", stars_this_week=300),
        cand("h/top", hn_points=900, hn_url="u3"),
    ]
    ranked = [c["repo"] for c in radar.rank_and_cut(cands, 5)]
    assert ranked == ["h/top", "h/both", "h/low", "t/huge", "t/big"]
    assert [c["repo"] for c in radar.rank_and_cut(cands, 2)] == ["h/top", "h/both"]


def test_rank_and_cut_puts_trendshift_between_hn_and_trending():
    cands = [
        cand("t/huge", stars_this_week=20000),
        cand("ts/second", trendshift_rank=2, stars_this_week=50000),
        cand("h/low", hn_points=90, hn_url="u1"),
        cand("ts/first", trendshift_rank=1),
        cand("h/ts", hn_points=150, hn_url="u2", trendshift_rank=3),
    ]
    ranked = [c["repo"] for c in radar.rank_and_cut(cands, 5)]
    assert ranked == ["h/ts", "h/low", "ts/first", "ts/second", "t/huge"]


def test_candidate_payload_includes_trendshift_rank():
    assert radar.candidate_payload(cand("a/b", trendshift_rank=7))["trendshift_rank"] == 7
    assert "trendshift_rank" not in radar.candidate_payload(cand("a/b"))


def test_threshold_splits_findings():
    findings = [{"repo": "a/b", "confidence": 3}, {"repo": "c/d", "confidence": 2}]
    actionable, weak = radar.split_actionable(findings)
    assert [f["repo"] for f in actionable] == ["a/b"]
    assert [f["repo"] for f in weak] == ["c/d"]


def finding(repo: str, project: str, confidence: int = 4, **kw) -> dict:
    return {
        "repo": repo,
        "project": project,
        "category": kw.get("category", "replace"),
        "why": kw.get("why", "Replaces the hand-rolled <thing>."),
        "next_step": kw.get("next_step", "Try it on one sample file."),
        "confidence": confidence,
    }


def test_render_email_groups_by_project_and_lists_skipped():
    actionable = [
        finding("acme/rocket", "invoice-app"),
        finding("x/y", "pipeline", category="security"),
        finding("p/q", "invoice-app", confidence=5),
    ]
    cands = {
        "acme/rocket": cand(
            "acme/rocket",
            stars=1200,
            stars_this_week=300,
            hn_points=250,
            hn_url="https://news.ycombinator.com/item?id=9",
        ),
    }
    also = [{"repo": "z/z", "reason": "unrelated game engine"}]
    body = radar.render_email(actionable, also, cands, date(2026, 10, 5))

    assert '<a href="https://github.com/acme/rocket"' in body
    assert "https://news.ycombinator.com/item?id=9" in body
    assert "+300 this week" in body
    assert body.count("<h3") == 2
    assert body.index(">invoice-app<") < body.index("p/q") < body.index("acme/rocket")
    assert "Also evaluated (1)" in body
    assert "z/z &mdash; unrelated game engine" in body
    assert "&lt;thing&gt;" in body and "<thing>" not in body
    assert "week 2026-W41" in body


def test_render_email_links_trendshift_rank():
    cands = {
        "acme/rocket": cand(
            "acme/rocket", stars=1200, trendshift_rank=3, trendshift_url=TRENDSHIFT_WEEK_URL
        )
    }
    body = radar.render_email([finding("acme/rocket", "pipeline")], [], cands, date(2026, 10, 5))
    assert f'<a href="{TRENDSHIFT_WEEK_URL}">Trendshift #3</a>' in body


def test_render_email_without_findings_still_lists_what_was_scanned():
    also = [{"repo": "z/z", "reason": "unrelated game engine"}, {"repo": "y/y", "reason": "toy"}]
    body = radar.render_email([], also, {}, date(2026, 10, 5))

    assert "No actionable findings this week." in body
    assert "<h3" not in body
    assert "Also evaluated (2)" in body
    assert "z/z &mdash; unrelated game engine" in body
    assert "week 2026-W41" in body


def test_email_subject_uses_iso_week():
    assert radar.email_subject(2, date(2026, 1, 1)) == "repo-radar: 2 findings for week 2026-W01"
    assert radar.email_subject(0, date(2026, 1, 1)) == "repo-radar: no findings for week 2026-W01"


def test_reconcile_turns_omitted_repos_into_skipped_and_ignores_unknown():
    batch = [cand("Acme/Rocket"), cand("b/c"), cand("d/e")]
    result = {
        "findings": [finding("acme/rocket", "homelab", confidence=9), finding("zz/zz", "x")],
        "skipped": [
            {"repo": "b/c", "reason": "toy project"},
            {"repo": "ACME/rocket", "reason": "dup"},
        ],
    }
    findings, skipped = radar.reconcile(batch, result)
    assert [(f["repo"], f["confidence"]) for f in findings] == [("Acme/Rocket", 5)]
    assert skipped == [
        {"repo": "b/c", "reason": "toy project"},
        {"repo": "d/e", "reason": "not returned by model"},
    ]


def test_match_candidates_batches_and_calls_model_per_batch(monkeypatch):
    monkeypatch.setattr(radar, "BATCH_SIZE", 2)
    calls = []

    def fake_call(batch):
        calls.append([c["repo"] for c in batch])
        return {"findings": [], "skipped": [{"repo": c["repo"], "reason": "no"} for c in batch]}

    cands = [cand(f"o/r{i}") for i in range(5)]
    findings, skipped = radar.match_candidates(fake_call, cands)
    assert calls == [["o/r0", "o/r1"], ["o/r2", "o/r3"], ["o/r4"]]
    assert findings == [] and len(skipped) == 5


def test_update_seen_shape_and_first_seen_preserved():
    seen = {
        "acme/rocket": {
            "first_seen": "2026-01-01",
            "last_eval": "2026-01-01",
            "verdict": "skip",
            "reason": "old",
            "project": None,
        }
    }
    findings = [
        finding("Acme/Rocket", "invoice-app", confidence=4, why="w" * 200),
        finding("low/conf", "pipeline", confidence=2, why="meh"),
    ]
    skipped = [
        {"repo": "b/c", "reason": "toy project"},
        {"repo": "never/seen", "reason": radar.NOT_RETURNED},
    ]
    out = radar.update_seen(seen, findings, skipped, date(2026, 10, 5))

    assert set(out) == {"Acme/Rocket", "low/conf", "b/c"}
    assert out["Acme/Rocket"] == {
        "first_seen": "2026-01-01",
        "last_eval": "2026-10-05",
        "verdict": "match",
        "reason": "w" * 120,
        "project": "invoice-app",
    }
    assert out["low/conf"]["verdict"] == "skip"
    assert out["low/conf"]["project"] == "pipeline"
    assert out["b/c"] == {
        "first_seen": "2026-10-05",
        "last_eval": "2026-10-05",
        "verdict": "skip",
        "reason": "toy project",
        "project": None,
    }


def test_update_seen_records_renamed_repo_under_both_names():
    findings = [finding("new/name", "pipeline", confidence=4)]
    out = radar.update_seen({}, findings, [], date(2026, 10, 5), {"new/name": "old/name"})
    assert set(out) == {"new/name", "old/name"}
    assert out["old/name"] == out["new/name"]


def test_write_json_sorted_with_trailing_newline(tmp_path):
    path = tmp_path / "seen.json"
    radar.write_json(path, {"b": 1, "a": {"z": 1, "y": 2}})
    assert path.read_text() == '{\n  "a": {\n    "y": 2,\n    "z": 1\n  },\n  "b": 1\n}\n'


def test_clean_readme_strips_badges_html_and_blank_lines():
    raw = (
        "[![CI](https://x/badge.svg)](https://x)\n![logo](logo.png)\n\n"
        '<p align="center"><b>Rocket</b></p>\n\n\nFast&nbsp;rockets &amp; more.\n'
    )
    assert radar.clean_readme(raw) == "Rocket\nFast rockets & more."
    assert len(radar.clean_readme("a" * 5000)) == 1500


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = {}

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def fake_client(stop_reason: str, text: str = ""):
    from types import SimpleNamespace as NS

    response = NS(stop_reason=stop_reason, stop_details=None, content=[NS(type="text", text=text)])
    return NS(beta=NS(messages=FakeMessages(response)))


def test_call_claude_sends_schema_and_parses_json():
    client = fake_client(
        "end_turn", '{"findings": [], "skipped": [{"repo": "a/b", "reason": "x"}]}'
    )
    result = radar.call_claude(client, "# profiles", [cand("a/b", readme="hi")])
    sent = client.beta.messages.kwargs
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["output_config"]["format"]["schema"] is radar.OUTPUT_SCHEMA
    assert "# profiles" in sent["messages"][0]["content"]
    assert '"repo": "a/b"' in sent["messages"][0]["content"]
    assert result["skipped"][0]["repo"] == "a/b"


def test_call_claude_raises_on_refusal():
    with pytest.raises(RuntimeError, match="refused"):
        radar.call_claude(fake_client("refusal"), "", [cand("a/b")])


def test_call_claude_raises_on_max_tokens():
    with pytest.raises(RuntimeError, match="max_tokens"):
        radar.call_claude(fake_client("max_tokens"), "", [cand("a/b")])


def test_call_claude_raises_without_text_block():
    client = fake_client("end_turn")
    client.beta.messages.response.content = []
    with pytest.raises(RuntimeError, match="no text block"):
        radar.call_claude(client, "", [cand("a/b")])


# ─── run() integration with fake HTTP and Anthropic ──────────────────────────

TRENDING_ROWS = "".join(
    f'<article class="Box-row"><h2><a href="/t/r{i}">x</a></h2>'
    f'<span class="d-inline-block float-sm-right">{100 - i} stars this week</span></article>'
    for i in range(10)
)
HN_HITS = [{"url": "https://github.com/h/tool", "points": 300, "objectID": "7", "title": "Tool"}]
RESEND_KEY = "re_secret_key"


class HttpResponse:
    def __init__(self, status: int = 200, text: str = "", data: object = None):
        self.status_code = status
        self.ok = status < 400
        self.text = text
        self.data = data

    def raise_for_status(self) -> None:
        if not self.ok:
            raise radar.requests.HTTPError(str(self.status_code))

    def json(self):
        if self.data is None:
            raise ValueError("no json")
        return self.data


class HttpSession:
    """Fake requests.Session: GitHub, Trendshift, HN, Resend and the dashboard."""

    posts: list[dict] = []
    resend = HttpResponse(200, data={"id": "email_123"})
    dashboard_down = False
    trendshift_down = False

    def get(self, url, **kwargs):
        if "github.com/trending" in url:
            return HttpResponse(text=TRENDING_ROWS)
        if url.startswith(radar.TRENDSHIFT_URL):
            if HttpSession.trendshift_down:
                raise radar.requests.ConnectionError("trendshift down")
            return HttpResponse(text=TRENDSHIFT_HTML)
        if "hn.algolia.com" in url:
            return HttpResponse(data={"hits": HN_HITS if url.endswith("page=0") else []})
        return HttpResponse(status=404)

    def post(self, url, **kwargs):
        HttpSession.posts.append({"url": url, **kwargs})
        if url == radar.RESEND_URL:
            return HttpSession.resend
        if HttpSession.dashboard_down:
            raise radar.requests.ConnectionError("dashboard down")
        return HttpResponse(201)


def posts_to(prefix: str) -> list[dict]:
    return [p for p in HttpSession.posts if p["url"].startswith(prefix)]


class Anthropic:
    error: Exception | None = None

    def __init__(self):
        from types import SimpleNamespace as NS

        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kwargs):
        from types import SimpleNamespace as NS

        if Anthropic.error:
            raise Anthropic.error
        result = {
            "findings": [finding("h/tool", "invoice-app", confidence=4)],
            "skipped": [{"repo": "t/r0", "reason": "unrelated"}],
        }
        return NS(
            stop_reason="end_turn",
            stop_details=None,
            content=[NS(type="text", text=json.dumps(result))],
        )


@pytest.fixture
def env(tmp_path, monkeypatch):
    import anthropic

    seen = tmp_path / "seen.json"
    seen.write_text("{}\n")
    (tmp_path / "projects.md").write_text("# profiles")
    monkeypatch.setattr(radar, "SEEN_FILE", seen)
    monkeypatch.setattr(radar, "LAST_RUN_FILE", tmp_path / "last_run.json")
    monkeypatch.setattr(radar, "PROJECTS_FILE", tmp_path / "projects.md")
    monkeypatch.setattr(radar.requests, "Session", HttpSession)
    monkeypatch.setattr(anthropic, "Anthropic", Anthropic)
    monkeypatch.setattr(radar, "RESEND_API_KEY", RESEND_KEY)
    monkeypatch.setattr(radar, "RECIPIENT_EMAIL", "me@example.com")
    monkeypatch.setattr(radar, "EMAIL_FROM", "Repo Radar <radar@example.com>")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("DASHBOARD_URL", "https://events.example.com")
    monkeypatch.setenv("DASHBOARD_TOKEN", "t")
    monkeypatch.setattr(HttpSession, "posts", [])
    monkeypatch.setattr(HttpSession, "resend", HttpResponse(200, data={"id": "email_123"}))
    monkeypatch.setattr(HttpSession, "dashboard_down", False)
    monkeypatch.setattr(HttpSession, "trendshift_down", False)
    monkeypatch.setattr(Anthropic, "error", None)
    return tmp_path


def test_run_dry_run_writes_no_files_and_posts_nothing(env):
    assert radar.main(["--dry-run"]) == 0
    assert (env / "seen.json").read_text() == "{}\n"
    assert not (env / "last_run.json").exists()
    assert HttpSession.posts == []


def test_run_leaves_seen_untouched_when_matching_fails(env):
    Anthropic.error = RuntimeError("API down")
    assert radar.main([]) == 1
    assert (env / "seen.json").read_text() == "{}\n"
    assert posts_to(radar.RESEND_URL) == []


def test_run_sends_resend_email_and_dashboard_events(env):
    assert radar.main([]) == 0

    [email] = posts_to(radar.RESEND_URL)
    assert email["headers"]["Authorization"] == f"Bearer {RESEND_KEY}"
    assert email["json"]["from"] == "Repo Radar <radar@example.com>"
    assert email["json"]["to"] == ["me@example.com"]
    assert email["json"]["subject"].startswith("repo-radar: 1 findings for week ")
    assert '<a href="https://github.com/h/tool"' in email["json"]["html"]
    assert "ts/first &mdash; not returned by model" in email["json"]["html"]

    events = [p["json"] for p in posts_to("https://events.example.com/api/events")]
    assert [e["kind"] for e in events] == ["finding", "run"]
    assert events[1]["data"]["email_sent"] is True
    assert events[1]["data"]["trendshift"] == 2

    seen = json.loads((env / "seen.json").read_text())
    # t/r1..t/r9 were omitted by the model, so they stay unrecorded.
    assert set(seen) == {"h/tool", "t/r0"}
    assert seen["h/tool"]["verdict"] == "match"


def test_run_sends_email_even_without_actionable_findings(env, monkeypatch):
    monkeypatch.setattr(radar, "CONFIDENCE_THRESHOLD", 5)
    assert radar.main([]) == 0

    [email] = posts_to(radar.RESEND_URL)
    assert email["json"]["subject"].startswith("repo-radar: no findings for week ")
    assert "No actionable findings this week." in email["json"]["html"]
    assert "h/tool &mdash; low confidence (4)" in email["json"]["html"]

    events = [p["json"] for p in posts_to("https://events.example.com/api/events")]
    assert [e["kind"] for e in events] == ["run"]
    assert events[0]["data"]["email_sent"] is True
    assert set(json.loads((env / "seen.json").read_text())) == {"h/tool", "t/r0"}


def test_run_fails_and_keeps_seen_when_resend_rejects(env, capsys):
    HttpSession.resend = HttpResponse(422, data={"message": "domain not verified"})
    assert radar.main([]) == 1
    assert (env / "seen.json").read_text() == "{}\n"
    out = capsys.readouterr().out
    assert "Resend 422: domain not verified" in out
    assert RESEND_KEY not in out
    events = [p["json"] for p in posts_to("https://events.example.com/api/events")]
    assert events[-1]["kind"] == "run" and events[-1]["status"] == "fail"


def test_run_fails_fast_without_email_from(env, monkeypatch, capsys):
    monkeypatch.setattr(radar, "EMAIL_FROM", None)
    gets = []
    monkeypatch.setattr(HttpSession, "get", lambda self, url, **kw: gets.append(url))
    assert radar.main([]) == 1
    out = capsys.readouterr().out
    assert "FAILED: RuntimeError: EMAIL_FROM is not set" in out
    assert "1. Collecting" not in out
    assert gets == []
    assert posts_to(radar.RESEND_URL) == []
    assert (env / "seen.json").read_text() == "{}\n"
    assert not (env / "last_run.json").exists()


def test_dry_run_does_not_need_email_from(env, monkeypatch):
    monkeypatch.setattr(radar, "EMAIL_FROM", None)
    assert radar.main(["--dry-run"]) == 0


def test_send_email_requires_2xx_with_id_and_config(monkeypatch):
    session = HttpSession()
    monkeypatch.setattr(HttpSession, "posts", [])
    monkeypatch.setattr(radar, "RESEND_API_KEY", RESEND_KEY)
    monkeypatch.setattr(radar, "RECIPIENT_EMAIL", "me@example.com")
    monkeypatch.setattr(radar, "EMAIL_FROM", "Repo Radar <radar@example.com>")

    monkeypatch.setattr(HttpSession, "resend", HttpResponse(200, data={}))
    assert radar.send_email(session, "s", "<p>x</p>") is False
    monkeypatch.setattr(HttpSession, "resend", HttpResponse(500, text="oops"))
    assert radar.send_email(session, "s", "<p>x</p>") is False
    monkeypatch.setattr(radar, "RESEND_API_KEY", None)
    assert radar.send_email(session, "s", "<p>x</p>") is False
    monkeypatch.setattr(radar, "RESEND_API_KEY", RESEND_KEY)
    monkeypatch.setattr(radar, "EMAIL_FROM", None)
    assert radar.send_email(session, "s", "<p>x</p>") is False
    assert len(HttpSession.posts) == 2


def test_run_succeeds_when_dashboard_is_down(env):
    HttpSession.dashboard_down = True
    assert radar.main([]) == 0
    assert len(posts_to(radar.RESEND_URL)) == 1
    assert len(posts_to("https://events.example.com/api/events")) == 2
    assert set(json.loads((env / "seen.json").read_text())) == {"h/tool", "t/r0"}


def test_run_succeeds_when_trendshift_is_down(env):
    HttpSession.trendshift_down = True
    assert radar.main([]) == 0
    [email] = posts_to(radar.RESEND_URL)
    assert "ts/first" not in email["json"]["html"]
    events = [p["json"] for p in posts_to("https://events.example.com/api/events")]
    assert events[-1]["data"]["trendshift"] == 0


def test_fetch_hn_returns_empty_list_on_network_error():
    class DownSession:
        def get(self, *args, **kwargs):
            raise radar.requests.ConnectionError("down")

    assert radar.fetch_hn(DownSession(), 7, radar.datetime.now(radar.UTC)) == []
