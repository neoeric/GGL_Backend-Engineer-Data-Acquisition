import asyncio
import copy
import json
from pathlib import Path

import pytest
from test_browser import html, no_dns

from threads_agent.agent import execute
from threads_agent.browser import Browser
from threads_agent.extraction import build_data, public_snapshot

FIXTURE = Path(__file__).parent / "fixtures/observed-public-thread.json"


def observed():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_captured_public_dom_body_and_self_thread_to_agent_result():
    f = observed()
    url = f["canonical"]
    # Replay the recorded visible line breaks; original site styles were deliberately not saved.
    page = (
        '<meta charset="utf-8"><style>[dir="auto"]{white-space:pre-wrap}</style><link rel="canonical" href="'
        + url
        + '"><script type="application/json">'
        + json.dumps(f["public_data"])
        + "</script>"
        + f["visible_html"]
    )

    async def transport(target):
        assert target == url
        return html(page)

    result, trace = asyncio.run(
        execute(
            url,
            mode="none",
            fields=["post_text", "continuations"],
            browser_factory=lambda run: Browser(run, transport=transport, dns=no_dns, settle_ms=100),
        )
    )
    root = f["public_data"]["media"]
    child = root["text_post_app_info"]["self_thread"]["posts"]["edges"][0]["node"]
    main = result["data"]["post_text"]
    assert (
        main["status"] == "ok"
        and main["value"] == "雖然明天要上班了\n但給大家一個好消息\n我們又要抽 iPhone ㄌ\niPhone 18 Pro喔"
    )
    assert "▓" not in main["value"]
    cont = result["data"]["continuations"]
    assert cont["status"] == "partial" and [x["post_id"] for x in cont["value"]] == [child["code"]]
    assert cont["value"][0]["text"] == child["caption"]["text"]
    assert cont["value"][0]["parent_post_id"] == root["code"]
    assert "self_thread.posts.edges[0].node" in cont["value"][0]["order_evidence"]["relationship_source"]
    post = next(e for e in trace["events"] if e["type"] == "tool_call" and e["name"] == "read_post")["output"]
    selected = next(p for p in post["posts"] if p["id"] == root["code"])
    assert selected["text_source_difference"]["caption_text"] == root["caption"]["text"]
    assert selected["text_source_difference"]["visible_text"] == main["value"]
    decisions = next(e for e in trace["events"] if e["type"] == "validation")["output"]["classifications"]
    assert next(x for x in decisions if x["id"] == "Dd8sq8MH9pJ")["classification"] == "reader_reply"
    assert next(x for x in decisions if x["id"] == "Dd0-fP0AcsC")["classification"] == "recommended_post"


def test_self_thread_label_alone_does_not_establish_parent():
    f = observed()
    root = f["public_data"]["media"]
    child = root["text_post_app_info"]["self_thread"]["posts"]["edges"][0]["node"]
    for info in [
        {"is_reply": True, "reply_to_author": {"username": "reader"}},
        {"is_reply": False, "reply_to_author": {"username": "whoscall_tw"}},
        {},
    ]:
        child["text_post_app_info"] = info
        snapshot = public_snapshot(
            {"canonical": f["canonical"], "scripts": [json.dumps(f["public_data"])]},
            f["canonical"],
            f["canonical"],
            [],
            f["captured_at"],
        )
        node = next(p for p in snapshot["posts"] if p["id"] == child["code"])
        assert node["parent"] is None and node["order"] is None


def test_visible_body_fallback_does_not_strip_literal_symbols_or_mix_posts():
    url = "https://www.threads.com/@fixture_author/post/ROOT"

    def card(code, text):
        return (
            '<div><a href="'
            + url.replace("ROOT", code)
            + '"><time>now</time></a><div><span dir="auto">'
            + text
            + "</span></div></div>"
        )

    page = (
        '<meta charset="utf-8"><link rel="canonical" href="'
        + url
        + '">'
        + card("ROOT", "Literal ▓ text")
        + card("OTHER", "Recommended text")
    )

    async def transport(target):
        return html(page)

    result, trace = asyncio.run(
        execute(
            url,
            mode="none",
            fields=["post_text"],
            browser_factory=lambda run: Browser(run, transport=transport, dns=no_dns, settle_ms=100),
        )
    )
    assert result["data"]["post_text"]["value"] == "Literal ▓ text"
    assert result["data"]["post_text"]["status"] == "ok"


def test_explicit_json_parent_conflicting_with_anchored_self_thread_is_ambiguous():
    f = observed()
    child = f["public_data"]["media"]["text_post_app_info"]["self_thread"]["posts"]["edges"][0]["node"]
    child["text_post_app_info"]["reply_to_post"] = {"code": "DIFFERENT"}
    snapshot = public_snapshot(
        {"canonical": f["canonical"], "scripts": [json.dumps(f["public_data"])]},
        f["canonical"],
        f["canonical"],
        [],
        f["captured_at"],
    )
    node = next(p for p in snapshot["posts"] if p["id"] == child["code"])
    assert node["evidence_conflict"] and node["order"] is None
    assert build_data(snapshot, ["continuations"])[0]["continuations"]["value"] is None


@pytest.mark.parametrize(
    "wrapper",
    [
        '<span dir="auto" style="display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;overflow:hidden">Line one<br>Line two</span>',
        '<div style="height:10px;overflow:hidden"><span dir="auto">Line one<br>Line two</span></div>',
    ],
)
def test_visible_body_fallback_retains_partial_for_clipped_text(wrapper):
    url = "https://www.threads.com/@fixture_author/post/ROOT"
    page = (
        '<link rel="canonical" href="' + url + '"><div><a href="' + url + '"><time>now</time></a>' + wrapper + "</div>"
    )

    async def transport(target):
        return html(page)

    result, _ = asyncio.run(
        execute(
            url,
            mode="none",
            fields=["post_text"],
            browser_factory=lambda run: Browser(run, transport=transport, dns=no_dns, settle_ms=100),
        )
    )
    assert result["data"]["post_text"]["status"] == "partial"


def test_excluded_public_post_arrays_do_not_become_author_continuations():
    f = observed()
    root = f["public_data"]["media"]
    excluded = copy.deepcopy(root["text_post_app_info"]["self_thread"]["posts"]["edges"][0]["node"])
    excluded["code"] = "RECOMMENDED"
    excluded["is_caption_truncated"] = False
    excluded["text_post_app_info"]["reply_to_post"] = {"code": root["code"]}
    linked = copy.deepcopy(excluded)
    linked["code"] = "LINKED"
    f["public_data"]["relatedPosts"] = [{"thread_items": [{"post": excluded}]}]
    root["text_post_app_info"]["linked_inline_media"] = [linked]
    snapshot = public_snapshot(
        {"canonical": f["canonical"], "scripts": [json.dumps(f["public_data"])]},
        f["canonical"],
        f["canonical"],
        [],
        f["captured_at"],
    )
    data, decisions = build_data(snapshot, ["continuations"])
    assert [p["post_id"] for p in data["continuations"]["value"]] == ["Dd0_RHRH6h9"]
    assert all(p["recommended"] for p in snapshot["posts"] if p["id"] in {"RECOMMENDED", "LINKED"})
    assert all(p["classification"] == "recommended_post" for p in decisions if p["id"] in {"RECOMMENDED", "LINKED"})


@pytest.mark.parametrize("reverse", [False, True])
def test_self_thread_fragment_joins_only_exact_observed_post_identity(reverse):
    # Synthetic fragmented shape: a named record and a separate ID-only media fragment.
    # The old captured-record fixture combined these layers and could hide this gap.
    f = observed()
    root = copy.deepcopy(f["public_data"]["media"])
    thread = root["text_post_app_info"].pop("self_thread")
    root["text_post_app_info"].pop("direct_replies", None)
    root["text_post_app_info"].pop("linked_inline_media", None)
    fragment = {"id": root["id"], "text_post_app_info": {"self_thread": thread}}
    scripts = [json.dumps({"media": root}), json.dumps({"media": fragment})]
    if reverse:
        scripts.reverse()
    snapshot = public_snapshot(
        {"canonical": f["canonical"], "scripts": scripts}, f["canonical"], f["canonical"], [], f["captured_at"]
    )
    child = next(p for p in snapshot["posts"] if p["id"] == "Dd0_RHRH6h9")
    assert child["parent"] == root["code"] and child["order"] == 1
    assert child["self_thread_context"]["parent"] == root["code"]
    assert child["is_reply_observed"] is True and child["reply_to_author_observed"] == "whoscall_tw"
    assert ".media.text_post_app_info.self_thread.posts.edges[0].node" in child["source_path"]
    assert build_data(snapshot, ["continuations"])[0]["continuations"]["value"][0]["post_id"] == child["id"]


def test_unidentified_or_conflicting_fragment_is_not_attached_to_target():
    f = observed()
    root = copy.deepcopy(f["public_data"]["media"])
    thread = root["text_post_app_info"].pop("self_thread")
    root["text_post_app_info"].pop("direct_replies", None)
    root["text_post_app_info"].pop("linked_inline_media", None)
    other = copy.deepcopy(root)
    other.update(code="OTHER", id="OTHER-ID", pk="OTHER-PK")
    for refs in [
        {"id": "UNKNOWN"},
        {"id": root["id"], "pk": other["pk"]},
        {"id": root["id"], "code": "UNKNOWN"},
        {"id": root["id"], "pk": "UNKNOWN"},
        {"id": root["id"], "user": {"username": "other_author"}},
        {},
    ]:
        fragment = {**refs, "text_post_app_info": {"self_thread": thread}}
        snapshot = public_snapshot(
            {"canonical": f["canonical"], "scripts": [json.dumps([root, other]), json.dumps({"media": fragment})]},
            f["canonical"],
            f["canonical"],
            [],
            f["captured_at"],
        )
        child = next(p for p in snapshot["posts"] if p["id"] == "Dd0_RHRH6h9")
        assert child["parent"] is None and child["order"] is None and child["self_thread_context"] is None
