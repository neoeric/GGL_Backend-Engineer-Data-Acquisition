"""Small bounded agent loop. Text and authority always come from verified tools."""

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path

from .browser import Browser
from .core import FIELDS, Failure, Run, field, utc
from .extraction import build_data, public_snapshot
from .model import Model, extract_urls
from .security import check_url, post_identity


class FixtureBrowser:
    def __init__(self, run: Run, path: str | Path) -> None:
        self.run = run
        self.snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
        if self.snapshot.get("fixture_kind") == "synthetic_public_serialization_not_live_capture":
            raw = self.snapshot
            self.snapshot = public_snapshot(
                {"canonical": raw["canonical"], "scripts": [json.dumps(raw["public_data"])]},
                run.source,
                raw["canonical"],
                [{"url": run.source, "kind": "synthetic_fixture"}],
                utc(),
            )
        self.read = False

    async def read_post(self) -> dict:
        # Fixture URL must match request; fixture is explicitly marked in result and trace.
        if post_identity(self.run.source) != post_identity(self.snapshot.get("canonical_url")):
            raise Failure("fixture_target_mismatch", technical=True)
        self.read = True
        self.run.captured_at = self.snapshot.get("captured_at")
        self.run.snapshot.update(self.snapshot)
        return self.snapshot

    async def expand_thread(self) -> dict:
        if not self.read:
            raise Failure("tool_precondition", technical=True)
        return self.snapshot

    async def read_profile(self) -> dict:
        if not self.read:
            raise Failure("tool_precondition", technical=True)
        return self.snapshot

    async def close(self) -> None:
        pass


def validate_parse(parsed: object, message: str) -> tuple[str, list[str]]:
    if not isinstance(parsed, dict) or set(parsed) != {
        "url",
        "requested_fields",
        "unsupported_fields",
        "invalid_reason",
    }:
        raise Failure("model_schema_error", technical=True)
    if (
        parsed["url"] is not None
        and not isinstance(parsed["url"], str)
        or parsed["invalid_reason"] is not None
        and not isinstance(parsed["invalid_reason"], str)
        or not isinstance(parsed["unsupported_fields"], list)
        or any(not isinstance(x, str) for x in parsed["unsupported_fields"])
        or not isinstance(parsed["requested_fields"], list)
        or any(not isinstance(x, str) for x in parsed["requested_fields"])
    ):
        raise Failure("model_schema_error", technical=True)
    urls = extract_urls(message)
    # URL count and scope are checked independently of model output.
    if len(urls) != 1:
        raise Failure("invalid_url_count", invalid=True)
    url = check_url(urls[0])
    if parsed.get("invalid_reason") or parsed.get("unsupported_fields"):
        raise Failure("unsupported_or_missing_fields", invalid=True)
    fields = parsed.get("requested_fields")
    if len(fields) != len(set(fields)) or any(x not in FIELDS for x in fields):
        raise Failure("model_schema_error", technical=True)
    if not fields:
        raise Failure("unsupported_or_missing_fields", invalid=True)
    # Compare the model's URL through the same extractor, so trailing prose or punctuation
    # the model kept or dropped is not mistaken for a different target.
    model_urls = extract_urls(parsed["url"]) if isinstance(parsed.get("url"), str) else []
    if model_urls != [urls[0]]:
        raise Failure("model_url_mismatch", technical=True)
    return url, [f for f in FIELDS if f in fields]


async def execute(
    message: str,
    mode: str = "openai-compatible",
    fixture: str | Path | None = None,
    fields: list[str] | None = None,
    seconds: float = 120,
    max_tools: int = 3,
    max_llm: int = 1,
    trace_file: str | None = None,
    model_factory: Callable[..., Model] = Model,
    browser_factory: Callable[..., Browser] = Browser,
) -> tuple[dict, dict]:
    run = Run(seconds, max_tools, max_llm)
    browser = None
    data = {}
    run.event(
        "run_start",
        input={"message": message},
        mode=mode,
        evidence_mode="synthetic_fixture" if fixture else "live_anonymous",
        limits={"seconds": round(run.deadline - time.monotonic(), 3), "tools": run.max_tools, "llm": run.max_llm},
    )
    model = model_factory(run, mode)
    try:
        if run.invalid:
            raise Failure("invalid_limits", invalid=True)
        urls = extract_urls(message)
        if len(urls) != 1:
            raise Failure("invalid_url_count", invalid=True)
        # Only a supported URL is a source_url; reject third-party shorteners before navigation/model.
        run.source = check_url(urls[0])
        if mode == "none":
            if not fields or any(x not in FIELDS for x in fields) or len(fields) != len(set(fields)):
                raise Failure("explicit_fields_required", invalid=True)
            run.requested = [f for f in FIELDS if f in fields]
        else:
            parsed = await asyncio.wait_for(model.call("parse", {"message": message}), run.remaining())
            run.source, run.requested = validate_parse(parsed, message)
        browser = FixtureBrowser(run, fixture) if fixture else browser_factory(run)
        actions = ["read_post"]
        if "continuations" in run.requested:
            actions.append("expand_thread")
        if "follower_count" in run.requested or "display_name" in run.requested:
            actions.append("read_profile")
        for action in actions:
            if (
                action == "read_profile"
                and ("follower_count" not in run.requested or run.snapshot.get("follower_count") is not None)
                and ("display_name" not in run.requested or run.snapshot.get("display_name"))
            ):
                continue
            if action != "read_post" and not run.snapshot.get("canonical_url"):
                break
            try:
                snapshot = await run.tool(action, getattr(browser, action))
                run.snapshot.update(snapshot)
                data, decisions = build_data(run.snapshot, run.requested)
                run.event(
                    "validation",
                    output={"classifications": decisions, "field_statuses": {f: v["status"] for f, v in data.items()}},
                )
            except Failure as e:
                run.fail(e)
                if not run.snapshot.get("canonical_url") or e.code in {
                    "access_denied",
                    "rate_limited",
                    "login_or_challenge",
                    "login_required",
                }:
                    break
        if run.snapshot.get("canonical_url"):
            data, _ = build_data(run.snapshot, run.requested)
        elif run.requested:
            run.issue("unresolved_target")
    except Failure as e:
        run.fail(e)
    except TimeoutError:
        run.fail(Failure("time_budget_exceeded", technical=True))
    except Exception as e:
        # Do not log arbitrary exception strings, response bodies or headers.
        run.fail(Failure("unexpected_error", technical=True))
        run.event("exception", output={"type": type(e).__name__}, status="error")
    finally:
        if browser:
            try:
                await asyncio.wait_for(browser.close(), max(0.01, min(5, run.deadline - time.monotonic())))
                run.event("browser_closed", status="ok")
            except Exception:
                run.fail(Failure("browser_close_error", technical=True))
        if not data:
            code = run.issues[0]["reason_code"] if run.issues else "not_observed"
            data = {f: field("error" if run.technical else "unavailable", code=code) for f in run.requested}
    result, trace = run.finish(data, trace_file)
    result["evidence_mode"] = "synthetic_fixture" if fixture else "live_anonymous"
    result["model_mode"] = mode
    # A mock has zero real model calls. Keep simulated counts explicitly separate.
    if mode == "mock":
        simulated = result["usage"]["llm_calls"]
        for usage in (result["usage"], trace["usage"], trace["events"][-1]["usage"]):
            usage["mock_calls"] = simulated
            usage["llm_calls"] = 0
            usage["calls_with_usage"] = 0
    return result, trace
