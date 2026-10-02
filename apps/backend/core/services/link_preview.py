"""Rich link previews for chat messages, fetched server-side (never by a client).

A user-supplied URL fetched by the backend is an SSRF primitive, so every hop
(including each redirect) is resolved here, refused unless EVERY address is
public, and then connected to the address we checked -- the hostname is never
re-resolved by the HTTP stack, so DNS rebinding between check and connect has
nothing to rebind. TLS still verifies against the real hostname (SNI).

X/Twitter posts use the public syndication JSON (the no-auth source X's own
embed renders from) and fall back to the public oEmbed endpoint. Neither's
HTML is ever returned: we extract text fields and the clients draw their own
card.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import re
import socket
import time
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx
import metadata_parser
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

logger = logging.getLogger(__name__)

MAX_BYTES = 1_000_000
TIMEOUT_S = 5.0
MAX_REDIRECTS = 3
USER_AGENT = "LightbulbLinkPreview/1.0 (+https://thelightbulb.company)"

# Tests swap this for an httpx.MockTransport.
_TRANSPORT: httpx.AsyncBaseTransport | None = None


class LinkPreview(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)

    url: str
    kind: str  # "link" | "tweet"
    title: str
    description: str | None = None
    site_name: str | None = None
    image_url: str | None = None
    author: str | None = None
    handle: str | None = None
    avatar_url: str | None = None
    published_at: str | None = None
    # The raw syndication tweet (the shape react-tweet's getTweet returns), so the web can
    # render react-tweet's own card. Data only -- it carries no HTML.
    tweet: dict | None = None


class Refused(Exception):
    """The URL (or a hop it redirected to) is not something we will fetch."""


# ---- SSRF-safe fetch -------------------------------------------------------


def _public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # is_global excludes private, loopback, link-local (169.254.169.254 IMDS),
    # CGNAT, ULA (fd00:ec2::254 IMDS), reserved and unspecified ranges.
    return ip.is_global and not ip.is_multicast


async def _resolve(host: str) -> str:
    """Every address the host resolves to must be public; returns the one we connect to."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise Refused(f"unresolvable host {host!r}") from exc
    ips = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    if not ips or not all(_public(ip) for ip in ips):
        raise Refused(f"non-public address for {host!r}")
    return str(ips[0])


def _check_url(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Refused("http(s) URLs only")
    if parts.username or parts.password:
        raise Refused("credentials in URL")
    try:
        port = parts.port
    except ValueError as exc:
        raise Refused("bad port") from exc
    if port not in (None, 80, 443):
        raise Refused("non-default port")
    return parts.scheme, parts.hostname, port


async def safe_get(url: str, *, accept: str) -> tuple[str, bytes, str]:
    """GET a public http(s) URL. Returns (final_url, body[:MAX_BYTES], content_type)."""
    async with asyncio.timeout(TIMEOUT_S):
        async with httpx.AsyncClient(
            transport=_TRANSPORT, timeout=TIMEOUT_S, follow_redirects=False, trust_env=False
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                scheme, host, port = _check_url(url)
                ip = await _resolve(host)
                parts = urlsplit(url)
                ip_host = f"[{ip}]" if ":" in ip else ip
                pinned = urlunsplit(
                    (scheme, ip_host + (f":{port}" if port else ""), parts.path or "/", parts.query, "")
                )
                request = client.build_request(
                    "GET",
                    pinned,
                    headers={"Host": parts.netloc, "User-Agent": USER_AGENT, "Accept": accept},
                    extensions={"sni_hostname": host},
                )
                response = await client.send(request, stream=True)
                try:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise Refused("redirect without location")
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise Refused(f"status {response.status_code}")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) >= MAX_BYTES:
                            break
                    return url, bytes(body[:MAX_BYTES]), response.headers.get("content-type", "")
                finally:
                    await response.aclose()
            raise Refused("too many redirects")


# ---- HTML metadata ---------------------------------------------------------


def _clip(value: str | None, limit: int) -> str | None:
    value = " ".join((value or "").split())
    return (value[: limit - 1] + "…" if len(value) > limit else value) or None


def _https_url(base: str, value: str | None) -> str | None:
    """Absolute https URL or nothing: an http image is mixed content on web and blocked by iOS ATS."""
    if not value:
        return None
    absolute = urljoin(base, value.strip())
    return absolute if urlsplit(absolute).scheme == "https" else None


def _domain(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.")


def _decode(body: bytes, content_type: str) -> str:
    match = re.search(r"charset=([\w-]+)", content_type, re.I)
    try:
        return body.decode(match.group(1) if match else "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _first(parsed, field: str, strategies: list[str]) -> str | None:
    found = parsed.get_metadatas(field, strategy=strategies) or {}
    for strategy in strategies:
        for value in found.get(strategy) or []:
            if isinstance(value, str) and value.strip():
                return value
    return None


class _Parser(metadata_parser.MetadataParser):
    # The stdlib html.parser backend: lxml is not installed, and the stock fallback logs an ERROR per page.
    def make_soup(self, html, **kwargs_bs):
        return BeautifulSoup(html, "html.parser", **kwargs_bs)


def parse_html(url: str, body: str) -> LinkPreview | None:
    # metadata_parser (OpenGraph / Twitter card / <meta> / <title>) is handed the HTML we
    # already fetched through safe_get and never a URL, so it has nothing to fetch itself.
    parsed = _Parser(html=body, search_head_only=False, require_valid_netloc=False).parsed_result
    title = _clip(_first(parsed, "title", ["og", "twitter", "page"]), 300)
    if not title:
        return None
    image = parsed.get_metadatas("image:secure_url", strategy=["og"]) or {}
    return LinkPreview(
        url=url,
        kind="link",
        title=title,
        description=_clip(_first(parsed, "description", ["og", "twitter", "meta"]), 500),
        site_name=_clip(_first(parsed, "site_name", ["og"]), 100) or _domain(url),
        image_url=_https_url(
            url,
            (image.get("og") or [None])[0]
            or _first(parsed, "image", ["og", "twitter"])
            or _first(parsed, "image:src", ["twitter"]),
        ),
    )


# ---- X / Twitter -----------------------------------------------------------

_TWEET_HOSTS = {"twitter.com", "x.com", "mobile.twitter.com", "mobile.x.com", "www.twitter.com", "www.x.com"}
_TWEET_PATH = re.compile(r"^/(?:[A-Za-z0-9_]{1,15}|i/web|i)/status(?:es)?/(\d{1,25})")


def tweet_id(url: str) -> str | None:
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in _TWEET_HOSTS:
        return None
    match = _TWEET_PATH.match(parts.path)
    return match.group(1) if match else None


def parse_syndication(url: str, data: dict) -> LinkPreview | None:
    user = data.get("user") or {}
    text = data.get("text")
    if data.get("__typename") not in (None, "Tweet") or not text or not user.get("screen_name"):
        return None
    start, end = (data.get("display_text_range") or [0, len(text)])[:2]
    media = (data.get("mediaDetails") or [{}])[0].get("media_url_https") or (data.get("photos") or [{}])[0].get("url")
    avatar = user.get("profile_image_url_https") or ""
    return LinkPreview(
        url=url,
        kind="tweet",
        title=_clip(user.get("name"), 100) or user["screen_name"],
        description=_clip(html.unescape(text[start:end]), 1000),
        site_name="X",
        image_url=_https_url(url, media),
        author=_clip(user.get("name"), 100),
        handle=user["screen_name"],
        avatar_url=_https_url(url, avatar.replace("_normal.", "_bigger.")),
        published_at=data.get("created_at"),
        tweet=data,
    )


def parse_oembed(url: str, data: dict) -> LinkPreview | None:
    author = data.get("author_name")
    handle = urlsplit(data.get("author_url") or "").path.strip("/") or None
    soup = BeautifulSoup(data.get("html") or "", "html.parser")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()
    paragraph, links = soup.find("p"), soup.find_all("a")
    text = paragraph.get_text().strip() if paragraph else ""
    date = links[-1].get_text().strip() if links else ""
    if not author or not text:
        return None
    try:
        published = datetime.strptime(date, "%B %d, %Y").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        published = None
    return LinkPreview(
        url=url,
        kind="tweet",
        title=_clip(author, 100) or author,
        description=_clip(text, 1000),
        site_name="X",
        author=_clip(author, 100),
        handle=handle,
        published_at=published,
    )


async def _json(url: str) -> dict:
    _, body, _ = await safe_get(url, accept="application/json")
    data = json.loads(body)
    return data if isinstance(data, dict) else {}


async def _tweet_preview(url: str, status_id: str) -> LinkPreview | None:
    try:
        # The syndication endpoint ignores the token's value but requires the param.
        preview = parse_syndication(
            url, await _json(f"https://cdn.syndication.twimg.com/tweet-result?id={status_id}&lang=en&token=a")
        )
        if preview:
            return preview
    except (Refused, httpx.HTTPError, TimeoutError, ValueError) as exc:
        logger.info("link_preview syndication failed id=%s: %s", status_id, exc)
    return parse_oembed(
        url, await _json(f"https://publish.twitter.com/oembed?omit_script=true&dnt=true&url={quote(url, safe='')}")
    )


# ---- entry point + cache ---------------------------------------------------

_TTL_S = 24 * 3600
_MISS_TTL_S = 3600
_MAX_ENTRIES = 2048
# ponytail: per-task in-process LRU (2-4 backend tasks each fetch a URL once a day);
# move to a DynamoDB TTL row if outbound fetch volume or cold-task misses matter.
_cache: OrderedDict[str, tuple[float, LinkPreview | None]] = OrderedDict()


async def _fetch_preview(url: str) -> LinkPreview | None:
    status_id = tweet_id(url)
    if status_id:
        return await _tweet_preview(url, status_id)
    final_url, body, content_type = await safe_get(url, accept="text/html,application/xhtml+xml")
    if "html" not in content_type.lower():
        return None
    preview = parse_html(final_url, _decode(body, content_type))
    return preview.model_copy(update={"url": url}) if preview else None


async def get_preview(url: str) -> LinkPreview | None:
    url = url.strip()
    now = time.monotonic()
    hit = _cache.get(url)
    if hit and hit[0] > now:
        _cache.move_to_end(url)
        return hit[1]
    try:
        _check_url(url)
        preview = await _fetch_preview(url)
    except (Refused, httpx.HTTPError, TimeoutError, ValueError) as exc:
        logger.info("link_preview refused/failed url=%s: %s", url[:200], exc)
        preview = None
    _cache[url] = (now + (_TTL_S if preview else _MISS_TTL_S), preview)
    _cache.move_to_end(url)
    while len(_cache) > _MAX_ENTRIES:
        _cache.popitem(last=False)
    return preview
