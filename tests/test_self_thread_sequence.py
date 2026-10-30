"""Later self_thread edges: retained as sequence-inferred continuations, never silently dropped."""
import asyncio
import copy
import json

import pytest

from threads_agent.agent import execute
from threads_agent.browser import Browser
from threads_agent.extraction import build_data, public_snapshot
from test_browser import html, no_dns

AUTHOR = "fixture_author"
URL = f"https://www.threads.com/@{AUTHOR}/post/ROOT"


def node(code, text, reply_to=AUTHOR, is_reply=True, explicit_parent=None, truncated=False):
    info = {"is_reply": is_reply, "reply_to_author": {"username": reply_to}, "self_thread_count": 0}
    if explicit_parent:
        info["reply_to_post"] = {"code": explicit_parent}
    return {"code": code, "id": f"{code}-ID", "pk": f"{code}-PK", "user": {"username": AUTHOR, "full_name": "Fixture"},
            "caption": {"text": text}, "is_caption_truncated": truncated, "text_post_app_info": info}


def media(edges):
    root = node("ROOT", "Main post", is_reply=False)
    root["text_post_app_info"] = {"is_reply": False, "self_thread_count": len(edges),
                                  "self_thread": {"posts": {"edges": [{"node": n} for n in edges]}}}
    return {"media": root}


def snapshot(public_data, scripts=None):
    return public_snapshot({"canonical": URL, "scripts": scripts or [json.dumps(public_data)]}, URL, URL, [], "fixture-time")


def post(snap, code):
    return next(p for p in snap["posts"] if p["id"] == code)


def test_three_edge_self_thread_is_chained_and_partial():
    snap = snapshot(media([node("N1", "one"), node("N2", "two"), node("N3", "three")]))
    data, decisions = build_data(snap, ["continuations"])
    cont = data["continuations"]
    assert cont["status"] == "partial" and cont["reason_code"] == "thread_incomplete"
    assert [x["post_id"] for x in cont["value"]] == ["N1", "N2", "N3"]
    assert [x["order"] for x in cont["value"]] == [1, 2, 3]
    assert [x["parent_post_id"] for x in cont["value"]] == ["ROOT", "N1", "N2"]
    assert [x["relationship_inferred"] for x in cont["value"]] == [None, "self_thread_sequence", "self_thread_sequence"]
    assert cont["value"][2]["order_evidence"]["post_ids"] == ["ROOT", "N1", "N2", "N3"]
    assert "self_thread.posts.edges[2].node" in cont["value"][2]["order_evidence"]["relationship_source"]
    assert {d["id"]: d["classification"] for d in decisions} == {"N1": "continuation", "N2": "continuation", "N3": "continuation"}


def test_first_edge_alone_can_still_be_complete():
    snap = snapshot(media([node("N1", "one")]))
    cont = build_data(snap, ["continuations"])[0]["continuations"]
    assert cont["status"] == "ok" and [x["post_id"] for x in cont["value"]] == ["N1"]
    assert cont["value"][0]["relationship_inferred"] is None


def test_explicit_matching_parent_on_later_edge_is_not_inferred():
    snap = snapshot(media([node("N1", "one"), node("N2", "two", explicit_parent="N1")]))
    cont = build_data(snap, ["continuations"])[0]["continuations"]
    assert cont["status"] == "ok"
    assert [x["relationship_inferred"] for x in cont["value"]] == [None, None]
    assert post(snap, "N2")["parent"] == "N1"


def test_later_edge_replying_to_reader_breaks_the_sequence():
    snap = snapshot(media([node("N1", "one"), node("N2", "two", reply_to="reader"), node("N3", "three")]))
    cont = build_data(snap, ["continuations"])[0]["continuations"]
    assert [x["post_id"] for x in cont["value"]] == ["N1"]
    assert post(snap, "N2")["parent"] is None
    # N3's inferred parent is N2, which is unattached, so N3 cannot reach the root either.
    assert post(snap, "N3")["parent"] == "N2"
    assert cont["status"] == "partial"


def test_explicit_conflicting_parent_on_later_edge_stays_ambiguous():
    snap = snapshot(media([node("N1", "one"), node("N2", "two", explicit_parent="DIFFERENT"), node("N3", "three")]))
    assert post(snap, "N2")["evidence_conflict"] and post(snap, "N2")["order"] is None
    data, decisions = build_data(snap, ["continuations"])
    assert [x["post_id"] for x in data["continuations"]["value"]] == ["N1"]
    classes = {d["id"]: d["classification"] for d in decisions}
    assert classes["N2"] == "ambiguous_evidence_conflict" and classes["N3"] == "ambiguous_evidence_conflict"


def test_edge_without_code_stops_chaining_without_guessing():
    broken = node("N2", "two")
    del broken["code"]
    snap = snapshot(media([node("N1", "one"), broken, node("N3", "three")]))
    assert [p["id"] for p in snap["posts"] if p["id"] in {"N1", "N3"} and p.get("parent")] == ["N1"]
    assert post(snap, "N3")["parent"] is None


@pytest.mark.parametrize("reverse", [False, True])
def test_fragmented_scripts_chain_all_edges(reverse):
    full = media([node("N1", "one"), node("N2", "two")])
    root = copy.deepcopy(full["media"])
    thread = root["text_post_app_info"].pop("self_thread")
    fragment = {"id": root["id"], "text_post_app_info": {"self_thread": thread}}
    scripts = [json.dumps({"media": root}), json.dumps({"media": fragment})]
    if reverse:
        scripts.reverse()
    snap = snapshot(None, scripts)
    cont = build_data(snap, ["continuations"])[0]["continuations"]
    assert [x["post_id"] for x in cont["value"]] == ["N1", "N2"]
    assert cont["value"][1]["relationship_inferred"] == "self_thread_sequence"


def test_agent_result_keeps_inferred_sequence_partial():
    public = media([node("N1", "one"), node("N2", "two"), node("N3", "three")])

    async def transport(url):
        assert url == URL
        return html('<link rel="canonical" href="' + URL + '"><script type="application/json">'
                    + json.dumps(public) + '</script><body>fixture</body>')

    result, trace = asyncio.run(execute(URL, mode="none", fields=["post_text", "continuations"],
                                        browser_factory=lambda run: Browser(run, transport=transport, dns=no_dns, settle_ms=100)))
    assert result["status"] == "partial"
    assert result["data"]["post_text"]["status"] == "ok"
    assert [x["post_id"] for x in result["data"]["continuations"]["value"]] == ["N1", "N2", "N3"]
    merged = next(e for e in trace["events"] if e["type"] == "tool_call" and e["name"] == "expand_thread")["output"]
    assert next(p for p in merged["posts"] if p["id"] == "N3")["relationship_inferred"] == "self_thread_sequence"
