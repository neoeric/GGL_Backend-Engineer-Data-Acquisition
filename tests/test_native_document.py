import asyncio
from types import SimpleNamespace

import pytest

from threads_agent.browser import Browser
from threads_agent.core import Run
from threads_agent.security import NavigationGuard

BASE = "https://www.threads.com"


async def no_dns(url):
    pass


class CDP:
    def __init__(self):
        self.calls = []

    async def send(self, name, args):
        self.calls.append((name, args))


def browser():
    b = Browser(Run(), dns=no_dns)
    b.guard = NavigationGuard(BASE + "/share/START/")
    b.main_frame_id = "main"
    b.document_cdp = CDP()
    return b


def response(url, status=200, location=None):
    event = {"requestId": "doc", "frameId": "main", "request": {"url": url}, "responseStatusCode": status}
    if location:
        event["responseHeaders"] = [{"name": "Location", "value": location}]
    return event


def test_native_redirects_checked_before_release_with_shared_limit():
    async def test():
        b = browser()
        for i in range(5):
            await b._document_response(response(b.guard.current, 302, "/share/HOP" + str(i) + "/"))
            assert b.document_cdp.calls[-1][0] == "Fetch.continueResponse"
        await b._document_response(response(b.guard.current, 302, "/share/SIX/"))
        assert b.failure.code == "redirect_limit_exceeded"
        assert b.document_cdp.calls[-1][0] == "Fetch.failRequest"
        assert b.guard.hops == 5

    asyncio.run(test())


@pytest.mark.parametrize(
    "target",
    ["https://127.0.0.1/private", "https://evil.test/share/X/", "http://www.threads.com/share/X/", "../START/"],
)
def test_native_unsafe_or_loop_location_is_not_released(target):
    async def test():
        b = browser()
        await b._document_response(response(b.guard.current, 302, target))
        assert b.failure.code in {"redirect_target_blocked", "redirect_loop"}
        assert [x[0] for x in b.document_cdp.calls] == ["Fetch.failRequest"]

    asyncio.run(test())


def test_document_uses_native_continue_and_does_not_api_fetch():
    class Request:
        url = BASE + "/share/START/"
        method = "GET"
        resource_type = "document"
        frame = "main"

        def is_navigation_request(self):
            return True

    class Route:
        request = Request()

        def __init__(self):
            self.continued = False

        async def continue_(self):
            self.continued = True

        async def fetch(self, **kw):
            raise AssertionError("Native document must not use APIRequest")

    async def test():
        b = browser()
        b.page = SimpleNamespace(main_frame="main")
        b.navigation_ticket = b.guard.current
        route = Route()
        await b._route(route)
        assert route.continued and b.failure is None

    asyncio.run(test())


def test_chromium_document_response_interception_stops_unsafe_redirect():
    class SyntheticNetwork(Browser):
        async def _route(self, route):
            # Fulfill the initial document locally; no request is sent to Threads.
            if route.request.url == BASE + "/share/START/":
                await route.fulfill(status=302, headers={"location": "https://blocked.invalid/"}, body="")
            else:
                await route.abort()

    async def test():
        b = SyntheticNetwork(Run(), dns=no_dns)
        try:
            with pytest.raises(Exception):
                await b.navigate(BASE + "/share/START/")
            assert b.failure.code == "redirect_target_blocked"
            assert b.statuses == [{"url": BASE + "/share/START/", "status": 302}]
        finally:
            await b.close()

    asyncio.run(test())
