"""Real Chromium; synthetic in-memory HTTP responses, no platform requests."""

import asyncio
import socket

import pytest

from threads_agent.browser import Browser
from threads_agent.core import Failure, Run
from threads_agent.security import public_dns

BASE = "https://www.threads.com"


async def no_dns(url):
    pass


async def scenario(pages, start="/share/START/"):
    fetched = []

    async def transport(url):
        fetched.append(url)
        if url not in pages:
            raise AssertionError("Unexpected network target: " + url)
        return pages[url]

    r = Run()
    r.source = BASE + start
    b = Browser(r, transport=transport, dns=no_dns, settle_ms=150)
    failure = None
    try:
        doc = await b.navigate(r.source)
    except Failure as e:
        failure = e.code
        doc = None
    finally:
        await b.close()
    return failure, fetched, b.guard.chain, doc


def html(text):
    return (200, {"content-type": "text/html"}, text.encode())


DONE = html("<html><head><title>Synthetic terminal</title></head><body>fixture</body></html>")


def test_http_five_redirects_and_relative_location():
    pages = {}
    ids = ["START", "ONE", "TWO", "THREE", "FOUR", "FIVE"]
    for a, b in zip(ids, ids[1:]):
        pages[f"{BASE}/share/{a}/"] = (302, {"location": f"../{b}/"}, b"")
    pages[f"{BASE}/share/FIVE/"] = DONE
    failure, fetched, chain, doc = asyncio.run(scenario(pages))
    assert failure is None and len(fetched) == 6 and len(chain) == 6
    assert doc["title"] == "Synthetic terminal"


def test_sixth_redirect_is_not_fetched():
    ids = ["START", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX"]
    pages = {f"{BASE}/share/{a}/": (302, {"location": f"/share/{b}/"}, b"") for a, b in zip(ids, ids[1:])}
    failure, fetched, chain, _ = asyncio.run(scenario(pages))
    assert failure == "redirect_limit_exceeded"
    assert f"{BASE}/share/SIX/" not in fetched


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.test/share/X/",
        "https://127.0.0.1/share/X/",
        "http://www.threads.com/share/X/",
        "https://www.threads.com/login",
        "https://www.threads.com/@someone",
    ],
)
def test_http_unsafe_redirect_never_requested(target):
    pages = {BASE + "/share/START/": (302, {"location": target}, b"")}
    failure, fetched, _, _ = asyncio.run(scenario(pages))
    assert failure == "redirect_target_blocked" and fetched == [BASE + "/share/START/"]


@pytest.mark.parametrize("kind", ["meta", "js"])
def test_meta_and_js_actual_navigation_are_checked(kind):
    target = "https://127.0.0.1/private"
    content = (
        f'<meta http-equiv="refresh" content="0;url={target}">'
        if kind == "meta"
        else f'<script>location.href="{target}"</script>'
    )
    pages = {BASE + "/share/START/": html(content)}
    failure, fetched, _, _ = asyncio.run(scenario(pages))
    assert failure == "redirect_target_blocked" and fetched == [BASE + "/share/START/"]


def test_http_meta_js_share_same_budget():
    pages = {
        BASE + "/share/START/": (302, {"location": "/share/A/"}, b""),
        BASE + "/share/A/": html('<meta http-equiv="refresh" content="0;url=/share/B/">'),
        BASE + "/share/B/": html('<script>location.href="/share/C/"</script>'),
        BASE + "/share/C/": (302, {"location": "/share/D/"}, b""),
        BASE + "/share/D/": html('<script>location.href="/share/E/"</script>'),
        BASE + "/share/E/": html('<meta http-equiv="refresh" content="0;url=/share/F/">'),
    }
    failure, fetched, chain, _ = asyncio.run(scenario(pages))
    assert failure == "redirect_limit_exceeded"
    assert BASE + "/share/F/" not in fetched
    assert len(chain) == 6
    assert {x["kind"] for x in chain} == {"initial", "http_redirect", "browser_navigation"}


def test_browser_loop_stops_before_refetch():
    pages = {
        BASE + "/share/START/": (302, {"location": "/share/B/"}, b""),
        BASE + "/share/B/": html('<script>location.href="/share/START/"</script>'),
    }
    failure, fetched, _, _ = asyncio.run(scenario(pages))
    assert failure == "redirect_loop" and len(fetched) == 2


@pytest.mark.parametrize(
    "status,code", [(401, "login_required"), (403, "access_denied"), (429, "rate_limited"), (404, "share_unavailable")]
)
def test_platform_restrictions_stop(status, code):
    failure, fetched, _, _ = asyncio.run(scenario({BASE + "/share/START/": (status, {}, b"blocked")}))
    assert failure == code and len(fetched) == 1


def test_private_dns_stops_before_transport(monkeypatch):
    async def test():
        loop = asyncio.get_running_loop()

        async def fake(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]

        monkeypatch.setattr(loop, "getaddrinfo", fake)
        with pytest.raises(Failure, match="redirect_target_blocked"):
            await public_dns(BASE + "/share/START/")

    asyncio.run(test())
