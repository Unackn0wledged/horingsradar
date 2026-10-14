import httpx
import pytest
from conftest import fixture_text
from fakes import FakeSite, make_fetcher

from horingsradar.http import Fetcher, FetchError, RobotsDisallowed, RobotsRules


def test_robots_rules_from_regjeringen():
    rules = RobotsRules(fixture_text("robots.txt"), "Horingsradar/0.1")
    base = "https://www.regjeringen.no"
    assert rules.allowed(f"{base}/no/dokument/hoyringar/id1763/")
    assert rules.allowed(f"{base}/no/dokument/hoyringar/id1763/?page=2")
    assert not rules.allowed(f"{base}/no/dokument/hoyringar/id1763/?sortby=1")
    assert not rules.allowed(f"{base}/Utilities/RSSEngine/rss.aspx")
    assert not rules.allowed(f"{base}/no/api/foo")
    assert not rules.allowed(f"{base}/contentassets/abc/data.xml")
    assert rules.allowed(f"{base}/contentassets/abc/notat.pdf")
    assert rules.allowed(f"{base}/no/dokumenter/x/id3014731/?showSvar=true")


def test_robots_longest_match_and_allow():
    rules = RobotsRules(
        "User-agent: *\nDisallow: /a\nAllow: /a/b\n\nUser-agent: other\nDisallow: /"
    )
    assert not rules.allowed("https://x/a/c")
    assert rules.allowed("https://x/a/b/c")
    assert rules.allowed("https://x/z")


def test_fetcher_throttles_and_sends_user_agent(settings):
    site = FakeSite()
    now = [100.0]
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    fetcher = Fetcher(settings, client=site.client(), clock=lambda: now[0], sleep=sleep)
    fetcher.get("https://www.regjeringen.no/no/dokument/hoyringar/id1763/")
    fetcher.get("https://www.regjeringen.no/no/dokument/hoyringar/id1763/?page=2")
    # robots.txt + 2 pages = 3 requests, each at least 2 s apart
    assert len(site.requests) == 3
    assert waits and all(w == pytest.approx(2.0) for w in waits)
    assert all(r.headers["user-agent"] == settings.user_agent for r in site.requests)


def test_fetcher_cache_by_age_and_conditional_get(settings):
    site = FakeSite()
    fetcher = make_fetcher(settings, site)
    url = "https://www.regjeringen.no/no/dokument/hoyringar/id1763/"
    first = fetcher.get(url, max_age=3600)
    second = fetcher.get(url, max_age=3600)
    assert not first.from_cache and second.from_cache
    assert len([r for r in site.requests if r.url.path.endswith("id1763/")]) == 1

    pdf = "https://www.regjeringen.no/contentassets/abc/notat.pdf"
    a = fetcher.get(pdf)
    b = fetcher.get(pdf)  # revalidated with If-None-Match -> 304
    assert a.content == b.content and b.from_cache
    assert site.requests[-1].headers.get("if-none-match") == '"fixture-etag"'


def test_fetcher_respects_robots(settings):
    fetcher = make_fetcher(settings, FakeSite())
    with pytest.raises(RobotsDisallowed):
        fetcher.get("https://www.regjeringen.no/no/dokument/hoyringar/id1763/?sortby=1")


def test_fetcher_raises_on_404_and_retries_5xx(settings):
    site = FakeSite()
    calls = {"n": 0}
    url = "https://www.regjeringen.no/contentassets/x/flaky.pdf"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/contentassets/x/flaky.pdf":
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(503)
        return site.handler(request)

    fetcher = Fetcher(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda s: None,
    )
    assert fetcher.get(url).status == 200
    assert calls["n"] == 3
    with pytest.raises(FetchError) as err:
        fetcher.get("https://www.regjeringen.no/missing")
    assert err.value.status == 404


def test_min_request_interval_is_enforced():
    from horingsradar.config import load_settings

    with pytest.raises(ValueError):
        load_settings(request_interval_seconds=1.0)
