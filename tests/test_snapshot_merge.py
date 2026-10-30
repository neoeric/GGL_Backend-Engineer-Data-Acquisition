"""Real expand_thread regressions using local raw observations, no platform requests."""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from threads_agent.browser import Browser
from threads_agent.core import Run
from threads_agent.extraction import build_data, merge_public_snapshots, public_snapshot

URL = "https://www.threads.com/@fixture_author/post/ROOT"
RAW = Path(__file__).parent / "fixtures/raw-public-thread.json"


def raw():
    return json.loads(RAW.read_text(encoding="utf-8"))["public_data"]


def snapshot(data, time="2026-10-29T03:00:00Z", url=URL):
    return public_snapshot({"canonical": url, "scripts": [json.dumps(data)]}, url, url, [], time)


async def actual_expand(initial, later, canonical=URL):
    r = Run()
    r.source = URL
    b = Browser(r)
    b.snapshot = copy.deepcopy(initial)
    b.guard = SimpleNamespace(chain=[])

    class Page:
        url = URL

        async def evaluate(self, script, arg=None):
            return [] if "const selector=" in script else {"canonical": canonical, "scripts": [json.dumps(later)]}

        def locator(self, selector):
            return self

        async def evaluate_all(self, script):
            return False

    b.page = Page()

    async def observe():
        pass

    b.observe = observe

    async def no_expansion():
        return {"rounds": 0, "clicks": 0, "scrolls": 0, "stopped": "test_stub"}

    b._expand_visible_thread = no_expansion
    output = await b.expand_thread()  # actual production method, not FixtureBrowser
    return r, output


def root_only():
    return {"thread_items": [x for x in raw()["thread_items"] if x["post"]["code"] == "ROOT"]}


def test_full_to_root_only_keeps_confirmed_continuations_and_times():
    initial = snapshot(raw())
    r, merged = asyncio.run(actual_expand(initial, root_only()))
    data, _ = build_data(merged, ["continuations"])
    assert data["continuations"]["status"] == "ok"
    assert [p["post_id"] for p in data["continuations"]["value"]] == ["A1", "A2"]
    assert [p["order"] for p in data["continuations"]["value"]] == [1, 2]
    for post in merged["posts"]:
        assert post["first_observed_at"] == initial["captured_at"]
        assert post["observations"][0]["captured_at"] == initial["captured_at"]
    assert merged["boundary_evidence"] is None
    assert initial["posts"] == snapshot(raw())["posts"]  # inputs were not mutated
    assert r.events[-1]["type"] == "evidence_merge"
    assert r.events[-1]["output"]["decision"] == "merged"


@pytest.mark.parametrize("change", ["text", "parent"])
def test_conflicts_retain_both_observations_and_mark_descendants_ambiguous(change):
    later = raw()
    a1 = next(x["post"] for x in later["thread_items"] if x["post"]["code"] == "A1")
    if change == "text":
        a1["caption"]["text"] = "Different later text"
    else:
        a1["text_post_app_info"]["reply_to_post"]["code"] = "R1"
    initial = snapshot(raw())
    r, merged = asyncio.run(actual_expand(initial, later))
    a1 = next(x for x in merged["posts"] if x["id"] == "A1")
    assert a1["text"] == "第一段續文\n順序來自父子鏈" and a1["parent"] == "ROOT"
    assert a1["evidence_conflict"] and len(a1["observations"]) == 2
    assert a1["observations"][0]["captured_at"] == initial["captured_at"]
    data, decisions = build_data(merged, ["continuations"])
    assert data["continuations"]["status"] == "unconfirmed" and data["continuations"]["value"] is None
    assert {x["id"]: x["classification"] for x in decisions}["A2"] == "ambiguous_evidence_conflict"
    event = next(x for x in r.events if x["type"] == "evidence_merge")
    assert event["output"]["conflicts"][0]["post_id"] == "A1"
    assert change in event["output"]["conflicts"][0]["fields"]
    assert any(i["reason_code"] == "source_evidence_conflict" for i in r.issues)


def test_different_target_is_rejected_without_mixing_posts():
    initial = snapshot(raw())
    other = copy.deepcopy(root_only())
    other["thread_items"][0]["post"]["code"] = "OTHER"
    r, merged = asyncio.run(actual_expand(initial, other, "https://www.threads.com/@fixture_author/post/OTHER"))
    assert merged == initial
    assert "OTHER" not in [p["id"] for p in merged["posts"]]
    event = next(x for x in r.events if x["type"] == "evidence_merge")
    assert event["output"]["decision"] == "rejected_target"
    assert any(i["reason_code"] == "evidence_target_mismatch" for i in r.issues)


def test_matching_target_can_add_new_chain_and_recompute_order():
    initial = snapshot(root_only())
    merged, report = merge_public_snapshots(initial, snapshot(raw(), "2026-10-29T03:01:00Z"))
    data, _ = build_data(merged, ["continuations"])
    assert [p["post_id"] for p in data["continuations"]["value"]] == ["A1", "A2"]
    assert [p["order"] for p in data["continuations"]["value"]] == [1, 2]
    assert set(report["added_post_ids"]) == {"A1", "A2", "R1", "AR", "ARS"}


def test_root_text_conflict_is_partial_and_keeps_original_in_result():
    initial = snapshot(raw())
    later = root_only()
    later["thread_items"][0]["post"]["caption"]["text"] = "Edited root"
    _, merged = asyncio.run(actual_expand(initial, later))
    data, _ = build_data(merged, ["post_text", "continuations"])
    assert data["post_text"]["status"] == "partial"
    assert data["post_text"]["reason_code"] == "source_evidence_conflict"
    assert data["post_text"]["value"] == "原文\n保留換行"
    assert data["continuations"]["value"] is None
