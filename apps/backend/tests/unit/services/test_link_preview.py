"""Link previews: HTML metadata parse, the tweet path, and SSRF refusal."""

import json

import httpx
import pytest

from core.services import link_preview as lp

ARTICLE = """<!doctype html><html><head>
<title>Fallback title</title>
<meta property="og:title" content="Big &amp; Bold News">
<meta property="og:description" content="  A short
  summary. ">
<meta property="og:site_name" content="The Paper">
<meta property="og:image" content="/img/hero.jpg">
</head><body><p>ignored</p></body></html>"""

PUBLIC_IP = "93.184.215.14"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    lp._cache.clear()

    async def resolve(host):
        if host in ("internal.test", "localhost"):
            raise lp.Refused("non-public")
        return PUBLIC_IP

    monkeypatch.setattr(lp, "_resolve", resolve)
    yield
    monkeypatch.setattr(lp, "_TRANSPORT", None)


def _serve(monkeypatch, handler):
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    monkeypatch.setattr(lp, "_TRANSPORT", httpx.MockTransport(wrapped))
    return calls


def test_parse_html_prefers_opengraph_and_absolutizes_image():
    preview = lp.parse_html("https://news.example.com/a", ARTICLE)
    assert preview.title == "Big & Bold News"
    assert preview.description == "A short summary."
    assert preview.site_name == "The Paper"
    assert preview.image_url == "https://news.example.com/img/hero.jpg"
    body = preview.model_dump()
    assert body["siteName"] == "The Paper" and body["kind"] == "link"


def test_parse_html_falls_back_to_title_and_domain_and_drops_http_image():
    preview = lp.parse_html(
        "https://www.example.com/",
        '<html><head><title> Plain </title><meta name="twitter:image" content="http://x/i.png"></head></html>',
    )
    assert preview.title == "Plain"
    assert preview.site_name == "example.com"
    assert preview.image_url is None
    assert lp.parse_html("https://example.com/", "<head></head>") is None


async def test_get_preview_pins_the_checked_ip_and_keeps_the_hostname(monkeypatch):
    calls = _serve(monkeypatch, lambda r: httpx.Response(200, html=ARTICLE))
    preview = await lp.get_preview("https://news.example.com/a")
    assert preview and preview.title == "Big & Bold News"
    assert calls[0].url.host == PUBLIC_IP
    assert calls[0].headers["host"] == "news.example.com"
    assert calls[0].extensions["sni_hostname"] == "news.example.com"
    # cached: a second call does not refetch
    await lp.get_preview("https://news.example.com/a")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/",
        "http://localhost/",
        "http://internal.test/",
        "http://user:pw@example.com/",
        "http://example.com:8080/",
    ],
)
async def test_refused_urls_never_reach_the_network(monkeypatch, url):
    calls = _serve(monkeypatch, lambda r: httpx.Response(200, html=ARTICLE))
    assert await lp.get_preview(url) is None
    assert calls == []


async def test_redirect_to_a_private_host_is_refused(monkeypatch):
    calls = _serve(
        monkeypatch, lambda r: httpx.Response(302, headers={"location": "http://internal.test/latest/meta-data/"})
    )
    assert await lp.get_preview("https://news.example.com/r") is None
    assert len(calls) == 1


async def test_redirect_loop_is_capped(monkeypatch):
    calls = _serve(monkeypatch, lambda r: httpx.Response(302, headers={"location": "/again"}))
    assert await lp.get_preview("https://news.example.com/loop") is None
    assert len(calls) == lp.MAX_REDIRECTS + 1


def test_public_address_check():
    import ipaddress

    for bad in (
        "127.0.0.1",
        "10.1.2.3",
        "169.254.169.254",
        "100.64.0.1",
        "::1",
        "fd00:ec2::254",
        "::ffff:127.0.0.1",
        "0.0.0.0",
        "224.0.0.1",
    ):
        assert not lp._public(ipaddress.ip_address(bad)), bad
    assert lp._public(ipaddress.ip_address("8.8.8.8"))


async def test_resolve_refuses_a_host_with_any_private_address(monkeypatch):
    monkeypatch.undo()  # real _resolve

    class Loop:
        async def getaddrinfo(self, *a, **k):
            return [(0, 0, 0, "", ("8.8.8.8", 0)), (0, 0, 0, "", ("10.0.0.5", 0))]

    monkeypatch.setattr(lp.asyncio, "get_running_loop", lambda: Loop())
    with pytest.raises(lp.Refused):
        await lp._resolve("mixed.example.com")


SYNDICATION = {
    "__typename": "Tweet",
    "created_at": "2006-03-21T20:50:14.000Z",
    "display_text_range": [0, 24],
    "text": "just setting up my twttr https://t.co/abc",
    "user": {"name": "jack", "screen_name": "jack", "profile_image_url_https": "https://pbs.twimg.com/p/a_normal.jpg"},
    "mediaDetails": [{"media_url_https": "https://pbs.twimg.com/media/x.jpg"}],
}


def test_tweet_id_matches_x_and_twitter_status_urls_only():
    assert lp.tweet_id("https://x.com/jack/status/20") == "20"
    assert lp.tweet_id("https://mobile.twitter.com/jack/statuses/20?s=1") == "20"
    assert lp.tweet_id("https://x.com/jack") is None
    assert lp.tweet_id("https://evilx.com/jack/status/20") is None


async def test_tweet_uses_syndication(monkeypatch):
    calls = _serve(monkeypatch, lambda r: httpx.Response(200, json=SYNDICATION))
    preview = await lp.get_preview("https://x.com/jack/status/20")
    assert calls[0].headers["host"] == "cdn.syndication.twimg.com"
    assert "id=20" in str(calls[0].url)
    assert preview.kind == "tweet"
    assert preview.description == "just setting up my twttr"
    assert (preview.author, preview.handle) == ("jack", "jack")
    assert preview.avatar_url == "https://pbs.twimg.com/p/a_bigger.jpg"
    assert preview.image_url == "https://pbs.twimg.com/media/x.jpg"
    assert preview.published_at == "2006-03-21T20:50:14.000Z"
    assert preview.tweet["user"]["screen_name"] == "jack"


async def test_tweet_falls_back_to_oembed_text_only(monkeypatch):
    oembed = {
        "author_name": "jack",
        "author_url": "https://x.com/jack",
        "html": '<blockquote class="twitter-tweet"><p lang="en">hello <b>world</b><script>x</script></p>&mdash; jack (@jack) '
        '<a href="https://x.com/jack/status/20">March 21, 2006</a></blockquote><script src="https://platform.twitter.com/widgets.js"></script>',
    }

    def handler(request):
        if request.headers["host"] == "cdn.syndication.twimg.com":
            return httpx.Response(200, json={})
        return httpx.Response(200, content=json.dumps(oembed).encode(), headers={"content-type": "application/json"})

    _serve(monkeypatch, handler)
    preview = await lp.get_preview("https://twitter.com/jack/status/20")
    assert preview.kind == "tweet"
    assert preview.description == "hello world"  # text only: tags and script stripped, never raw HTML
    assert "<" not in preview.description
    assert preview.handle == "jack"
    assert preview.published_at.startswith("2006-03-21")


def test_route_returns_camelcase_card_or_404(monkeypatch):
    from unittest.mock import MagicMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.auth import get_current_user
    from routers import messaging

    app = FastAPI()
    app.include_router(messaging.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: MagicMock(org_id="org_1", user_id="user_1")
    monkeypatch.setattr(messaging, "resolve_owner_id", lambda auth: "org_1")

    async def fake(url):
        return lp.LinkPreview(url=url, kind="link", title="T", site_name="s.com") if "ok" in url else None

    monkeypatch.setattr(lp, "get_preview", fake)
    client = TestClient(app)
    body = client.get("/api/v1/messaging/link-preview", params={"url": "https://ok.example/"}).json()
    assert body["siteName"] == "s.com" and body["imageUrl"] is None
    assert client.get("/api/v1/messaging/link-preview", params={"url": "https://no.example/"}).status_code == 404
