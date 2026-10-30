"""Tracking parameters, CJK-adjacent URLs, fork ordering, caption-confirmed continuation text,
bounded expansion and profile capture time."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_browser import html, no_dns
from test_self_thread_sequence import media, node

from threads_agent.agent import execute, validate_parse
from threads_agent.browser import Browser
from threads_agent.core import Failure, Run, field
from threads_agent.extraction import build_data, caption_index, public_snapshot
from threads_agent.model import extract_urls
from threads_agent.security import NavigationGuard, check_url

AUTHOR = "fixture_author"
URL = f"https://www.threads.com/@{AUTHOR}/post/ROOT"
FIXTURE = Path(__file__).parent / "fixtures/thread.json"


# --------------------------------------------------------------------------- tracking parameters
@pytest.mark.parametrize(
    "raw,expected",
    [
        (URL + "?xmt=AQF0abc", URL),
        (URL + "?xmt=AQF0abc&foo=1", URL + "?foo=1"),
        (URL + "?utm_source=x&igshid=y&fbclid=z", URL),
        ("https://www.threads.com/share/ABC/?xmt=1", "https://www.threads.com/share/ABC/"),
    ],
)
def test_tracking_parameters_are_stripped_but_other_query_kept(raw, expected):
    assert check_url(raw) == expected


class CDP:
    def __init__(self):
        self.calls = []

    async def send(self, name, args):
        self.calls.append((name, args))


def document(url, status=200, location=None):
    event = {"requestId": "doc", "frameId": "main", "request": {"url": url}, "responseStatusCode": status}
    if location:
        event["responseHeaders"] = [{"name": "Location", "value": location}]
    return event


def test_native_redirect_with_tracking_parameter_is_released_once():
    b = Browser(Run(), dns=no_dns)
    b.guard = NavigationGuard("https://www.threads.com/share/START/")
    b.main_frame_id = "main"
    b.document_cdp = CDP()

    async def test():
        await b._document_response(document("https://www.threads.com/share/START/", 302, URL + "?xmt=AQF0abc"))
        assert b.document_cdp.calls[-1][0] == "Fetch.continueResponse"
        assert b.guard.current == URL and b.navigation_ticket == URL
        # Chromium then requests the raw Location; the normalized form is what the ticket authorizes.
        await b._document_response(document(URL + "?xmt=AQF0abc"))
        assert b.document_cdp.calls[-1][0] == "Fetch.continueResponse" and b.navigation_ticket is None
        assert b.statuses[-1]["url"] == URL
        # A second, unticketed arrival of the same URL is a loop, not a free pass.
        await b._document_response(document(URL + "?xmt=OTHER"))
        assert b.document_cdp.calls[-1][0] == "Fetch.continueResponse"

    asyncio.run(test())


def test_route_accepts_ticketed_navigation_with_tracking_parameter():
    class Request:
        url = URL + "?xmt=AQF0abc"
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

        async def abort(self):
            raise AssertionError("ticketed navigation must not be aborted")

    b = Browser(Run(), dns=no_dns)
    b.guard = NavigationGuard(URL)
    b.page = SimpleNamespace(main_frame="main")
    b.navigation_ticket = URL
    route = Route()
    asyncio.run(b._route(route))
    assert route.continued and b.failure is None and b.navigation_ticket is None


# --------------------------------------------------------------------------- CJK next to the URL
@pytest.mark.parametrize(
    "message",
    [
        "貼文內容" + URL + "的內容",
        "請抓" + URL + "，謝謝",
        "看這篇：" + URL + "。",
        URL + "】全部",
        "抓 " + URL + "　全部",
    ],
)
def test_cjk_text_next_to_url_is_not_part_of_the_url(message):
    assert extract_urls(message) == [URL]


def test_model_url_with_trailing_prose_is_normalized_but_other_target_is_not():
    parsed = {
        "url": URL + "的內容",
        "requested_fields": ["post_text"],
        "unsupported_fields": [],
        "invalid_reason": None,
    }
    assert validate_parse(parsed, "貼文內容" + URL + "的內容") == (URL, ["post_text"])
    parsed["url"] = "https://www.threads.com/@other/post/X"
    with pytest.raises(Failure, match="model_url_mismatch"):
        validate_parse(parsed, "貼文內容 " + URL)


def test_glued_cjk_request_completes_with_fixture():
    result, _ = asyncio.run(execute("貼文內容" + URL + "的內容", mode="mock", fixture=FIXTURE))
    assert result["status"] == "complete" and result["source_url"] == URL


# --------------------------------------------------------------------------- fork ordering
def record(code, text, parent=None, taken_at=None):
    r = {
        "code": code,
        "id": f"{code}-ID",
        "pk": f"{code}-PK",
        "user": {"username": AUTHOR, "full_name": "Fixture"},
        "caption": {"text": text},
        "is_caption_truncated": False,
        "text_post_app_info": {"reply_to_post": {"code": parent}} if parent else {},
    }
    if taken_at is not None:
        r["taken_at"] = taken_at
    return r


def snap(records):
    data = {"thread_items": [{"post": r} for r in records]}
    return public_snapshot({"canonical": URL, "scripts": [json.dumps(data)]}, URL, URL, [], "fixture-time")


def test_fork_under_root_is_ordered_by_taken_at_and_stays_partial():
    s = snap(
        [
            record("ROOT", "main"),
            record("B", "second", parent="ROOT", taken_at=200),
            record("A", "first", parent="ROOT", taken_at=100),
            record("A2", "first-child", parent="A", taken_at=150),
        ]
    )
    cont = build_data(s, ["continuations"])[0]["continuations"]
    assert cont["status"] == "partial" and cont["reason_code"] == "thread_incomplete"
    assert [x["post_id"] for x in cont["value"]] == ["A", "A2", "B"]
    assert [x["order"] for x in cont["value"]] == [1, 2, 3]
    assert [x["parent_post_id"] for x in cont["value"]] == ["ROOT", "A", "ROOT"]
    assert [x["order_inferred"] for x in cont["value"]] == ["taken_at", None, "taken_at"]
    assert cont["value"][0]["order_evidence"]["kind"] == "parent_chain_siblings_by_taken_at"
    assert cont["value"][1]["order_evidence"]["kind"] == "explicit_parent_chain"
    assert cont["value"][2]["order_evidence"]["post_ids"] == ["ROOT", "B"]


@pytest.mark.parametrize("stamps", [(None, None), (100, 100), (100, None)])
def test_fork_without_distinct_timestamps_is_not_guessed(stamps):
    s = snap([record("ROOT", "main"), record("A", "x", "ROOT", stamps[0]), record("B", "y", "ROOT", stamps[1])])
    assert build_data(s, ["continuations"])[0]["continuations"]["value"] is None


def test_unique_chain_is_still_complete_with_timestamps_present():
    s = snap([record("ROOT", "main", taken_at=1), record("A", "x", "ROOT", 2), record("A2", "y", "A", 3)])
    cont = build_data(s, ["continuations"])[0]["continuations"]
    assert cont["status"] == "ok" and [x["order_inferred"] for x in cont["value"]] == [None, None]


def test_self_thread_fork_with_explicit_parents_falls_back_to_edge_position():
    # Both edges explicitly reply to the root; the explicit parent outranks the sequence guess,
    # and the fork is ordered by edge position only because no timestamps exist.
    public = media([node("N1", "one", explicit_parent="ROOT"), node("N2", "two", explicit_parent="ROOT")])
    s = public_snapshot({"canonical": URL, "scripts": [json.dumps(public)]}, URL, URL, [], "fixture-time")
    n2 = next(p for p in s["posts"] if p["id"] == "N2")
    assert n2["parent"] == "ROOT" and not n2.get("evidence_conflict") and n2["relationship_inferred"] is None
    cont = build_data(s, ["continuations"])[0]["continuations"]
    assert cont["status"] == "partial" and [x["post_id"] for x in cont["value"]] == ["N1", "N2"]
    assert cont["value"][1]["order_evidence"]["kind"] == "parent_chain_siblings_by_self_thread_index"


# --------------------------------------------------------------------------- caption-confirmed text
def unknown_truncation(post):
    """Live self_thread nodes carry no is_caption_truncated flag; completeness must come from elsewhere."""
    del post["is_caption_truncated"]
    return post


def card(code, text, header=False, reply_to=None, tag="div"):
    """A div card is found by its own timestamp permalink; an article card (one that may carry a
    reply-context link, which is a second permalink) is found by the article pass."""
    link = '<a href="' + URL.replace("ROOT", code) + '"><time>1h</time></a>'
    head = '<div><span dir="auto">Fixture</span><span dir="auto">·</span></div>' if header else ""
    reply = '<a rel="in-reply-to" href="' + URL.replace("ROOT", reply_to) + '">Replying to</a>' if reply_to else ""
    return f"<{tag}>" + head + link + reply + '<div><span dir="auto">' + text + f"</span></div></{tag}>"


def test_caption_index_maps_unique_permalinks_only():
    public = media([node("N1", "one")])
    other = json.dumps({"media": record("N1", "different text")})
    index = caption_index([json.dumps(public), other])
    assert index[f"/@{AUTHOR}/post/ROOT"] == "Main post" and f"/@{AUTHOR}/post/N1" not in index


def test_reply_card_with_name_rows_confirms_caption_text_completeness():
    public = media([unknown_truncation(node("N1", "Continuation body text"))])
    page = (
        '<meta charset="utf-8"><link rel="canonical" href="'
        + URL
        + '"><script type="application/json">'
        + json.dumps(public)
        + "</script>"
        + card("ROOT", "Main post")
        + card("N1", "Continuation body text", header=True)
    )

    async def transport(target):
        return html(page)

    result, trace = asyncio.run(
        execute(
            URL,
            mode="none",
            fields=["post_text", "continuations"],
            browser_factory=lambda run: Browser(run, transport=transport, dns=no_dns, settle_ms=100),
        )
    )
    cont = result["data"]["continuations"]
    assert cont["status"] == "ok" and cont["value"][0]["text_complete"] is True
    assert result["status"] == "complete"
    post = next(e for e in trace["events"] if e["type"] == "tool_call" and e["name"] == "read_post")["output"]
    n1 = next(p for p in post["posts"] if p["id"] == "N1")
    assert n1["text_completeness_source"] == "permalink_time_scoped_caption_match"


def test_reply_card_whose_text_differs_from_caption_is_not_confirmed():
    public = media([unknown_truncation(node("N1", "Continuation body text"))])
    page = (
        '<link rel="canonical" href="'
        + URL
        + '"><script type="application/json">'
        + json.dumps(public)
        + "</script>"
        + card("ROOT", "Main post")
        + card("N1", "Continuation body text and more", header=True)
    )

    async def transport(target):
        return html(page)

    result, _ = asyncio.run(
        execute(
            URL,
            mode="none",
            fields=["continuations"],
            browser_factory=lambda run: Browser(run, transport=transport, dns=no_dns, settle_ms=100),
        )
    )
    cont = result["data"]["continuations"]
    assert cont["status"] == "partial" and cont["value"][0]["text_complete"] is False


# --------------------------------------------------------------------------- bounded expansion
def test_more_replies_button_is_pressed_and_the_new_continuation_is_read():
    page = (
        '<link rel="canonical" href="'
        + URL
        + '">'
        + card("ROOT", "Main post")
        + '<button id="more">Show more replies</button>'
        + '<template id="late">'
        + card("C2", "Late continuation", reply_to="ROOT", tag="article")
        + "</template>"
        + "<script>document.getElementById('more').addEventListener('click',()=>{"
        + "document.getElementById('more').remove();"
        + "document.body.appendChild(document.getElementById('late').content.cloneNode(true));});</script>"
    )

    async def transport(target):
        return html(page)

    result, trace = asyncio.run(
        execute(
            URL,
            mode="none",
            fields=["continuations"],
            browser_factory=lambda run: Browser(run, transport=transport, dns=no_dns, settle_ms=100),
        )
    )
    expansion = next(e for e in trace["events"] if e["type"] == "thread_expansion")["output"]
    assert expansion["clicks"] == 1
    cont = result["data"]["continuations"]
    assert [x["post_id"] for x in cont["value"]] == ["C2"] and cont["status"] == "ok"
    assert not any(i["reason_code"] == "thread_incomplete" for i in result["issues"])
    assert result["status"] == "complete"


def test_static_page_expansion_stops_when_nothing_grows():
    page = '<link rel="canonical" href="' + URL + '">' + card("ROOT", "Main post")

    async def transport(target):
        return html(page)

    result, trace = asyncio.run(
        execute(
            URL,
            mode="none",
            fields=["post_text", "continuations"],
            browser_factory=lambda run: Browser(run, transport=transport, dns=no_dns, settle_ms=100),
        )
    )
    expansion = next(e for e in trace["events"] if e["type"] == "thread_expansion")["output"]
    assert expansion == {"rounds": 1, "clicks": 0, "scrolls": 1, "stopped": "no_growth"}
    assert result["data"]["post_text"]["status"] == "ok"


# --------------------------------------------------------------------------- profile capture time
def test_profile_capture_time_does_not_overwrite_post_capture_time():
    b = Browser(Run(), dns=no_dns)
    b.run.source = URL
    b.run.captured_at = "post-time"
    b.snapshot = {"canonical_url": URL, "author": AUTHOR}

    async def navigate(url, profile=None):
        return {
            "canonical": f"https://www.threads.com/@{AUTHOR}",
            "scripts": [json.dumps({"username": AUTHOR, "full_name": "Fixture", "follower_count": 12})],
        }

    b.navigate = navigate
    snapshot = asyncio.run(b.read_profile())
    assert b.run.captured_at == "post-time" and snapshot["follower_count"] == 12
    assert isinstance(snapshot["profile_captured_at"], str)


def test_result_reports_profile_capture_time_separately():
    r = Run()
    r.requested = ["display_name"]
    r.snapshot = {"canonical_url": URL, "profile_captured_at": "profile-time"}
    r.captured_at = "post-time"
    result, _ = r.finish({"display_name": field("ok", "Fixture")})
    assert result["captured_at"] == "post-time" and result["profile_captured_at"] == "profile-time"
