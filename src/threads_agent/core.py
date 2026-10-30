"""Result contract, bounded accounting and allowlisted trace construction."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

FIELDS = ("display_name", "follower_count", "post_text", "continuations")
FIELD_STATUS = {"ok", "partial", "unconfirmed", "unavailable", "error"}


def utc() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Failure(Exception):
    def __init__(self, code: str, reason: str | None = None, technical: bool = False, invalid: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason or code.replace("_", " ")
        self.technical = technical
        self.invalid = invalid


def field(status: str, value: object = None, code: str | None = None, reason: str | None = None) -> dict:
    result = {"status": status, "value": value}
    if status != "ok":
        result.update(reason_code=code or "not_observed", reason=reason or "Source evidence is insufficient.")
    return result


def scrub(value: object, secrets: tuple[str, ...] = ()) -> object:
    """Extra defense after event fields have been explicitly selected by callers."""
    if isinstance(value, dict):
        return {
            k: scrub(v, secrets)
            for k, v in value.items()
            if not re.search(r"authorization|cookie|api.?key|password|reasoning|chain.of.thought", k, re.I)
        }
    if isinstance(value, list):
        return [scrub(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)Bearer\s+[\w.\-]+", "Bearer [REDACTED]", value)
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
        value = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[REDACTED]@", value)
        value = re.sub(r"(?i)([?&](?:access_token|token|api_key|key|auth|password)=)[^&#\s]+", r"\1[REDACTED]", value)
    return value


class Run:
    def __init__(self, seconds: float = 120, max_tools: int = 6, max_llm: int = 3) -> None:
        invalid_limits = not valid_limits(seconds, max_tools, max_llm)
        if invalid_limits:
            seconds, max_tools, max_llm = 120, 6, 3
        self.id = str(uuid.uuid4())
        self.started_at = utc()
        self.deadline = time.monotonic() + min(seconds, 120)
        self.work_deadline = self.deadline - min(5, max(0, seconds * 0.05))
        self.max_tools, self.max_llm = min(max_tools, 6), min(max_llm, 3)
        self.tools = 0
        self.calls = []
        self.events = []
        self.issues = []
        self.invalid = False
        self.technical = False
        self.source = None
        self.requested = []
        self.captured_at = None
        self.snapshot = {}
        self.secrets = tuple(filter(None, [os.getenv("LLM_API_KEY")]))
        if invalid_limits:
            self.fail(
                Failure(
                    "invalid_limits",
                    "Budgets must be finite positive numbers; call counts must be integers.",
                    invalid=True,
                )
            )

    def remaining(self) -> float:
        remaining = self.work_deadline - time.monotonic()
        if remaining <= 0:
            raise Failure("time_budget_exceeded", technical=True)
        return remaining

    def event(self, kind: str, **payload: object) -> None:
        original = {"type": kind, "at": utc(), **payload}
        cleaned = scrub(original, self.secrets)
        if cleaned != original:
            cleaned.update(sanitized=True, sanitization_reason="trace_credential_or_private_field_policy")
        self.events.append(cleaned)

    def issue(self, code: str, reason: str | None = None) -> None:
        issue = {"reason_code": code, "reason": reason or code.replace("_", " ")}
        if issue not in self.issues:
            self.issues.append(issue)

    def fail(self, error: Failure) -> None:
        self.issue(error.code, error.reason)
        self.invalid |= error.invalid
        self.technical |= error.technical

    def usage(self) -> dict:
        known = [x for x in self.calls if x is not None]
        i, o = sum(x[0] for x in known), sum(x[1] for x in known)
        complete = len(known) == len(self.calls)
        return {
            "llm_calls": len(self.calls),
            "calls_with_usage": len(known),
            "known_input_tokens": i,
            "known_output_tokens": o,
            "input_tokens": i if complete else None,
            "output_tokens": o if complete else None,
            "total_tokens": i + o if complete else None,
            "usage_complete": complete,
            "reason": None if complete else "At least one attempted model call has no verified usage.",
        }

    async def tool(self, name: str, fn: Callable[[], Awaitable[dict]]) -> dict:
        if self.tools >= self.max_tools:
            raise Failure("tool_budget_exceeded", technical=True)
        self.remaining()
        self.tools += 1
        call_id = f"tool-{self.tools}"
        start = time.monotonic()
        try:
            output = await asyncio.wait_for(fn(), self.remaining())
            self.event(
                "tool_call",
                call_id=call_id,
                name=name,
                input={"source_url": self.source},
                output=output,
                status="ok",
                elapsed_ms=round((time.monotonic() - start) * 1000),
            )
            return output
        except BaseException as e:
            code = e.code if isinstance(e, Failure) else "tool_timeout" if isinstance(e, TimeoutError) else "tool_error"
            self.event(
                "tool_call",
                call_id=call_id,
                name=name,
                input={"source_url": self.source},
                output=None,
                status="error",
                reason_code=code,
                elapsed_ms=round((time.monotonic() - start) * 1000),
            )
            if isinstance(e, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            raise e if isinstance(e, Failure) else Failure(code, technical=True) from e

    def finish(self, data: dict, trace_file: str | None = None) -> tuple[dict, dict]:
        try:
            return self._finish(data, trace_file)
        except Exception:
            # Stable final error path; preserve prior events and any individually valid values.
            self.fail(Failure("result_schema_error", technical=True))
            recovered = {}
            for name in self.requested:
                try:
                    validate_field(name, data[name])
                    recovered[name] = data[name]
                except Exception:
                    recovered[name] = field("error", code="result_schema_error")
            self.requested = [x for x in self.requested if x in FIELDS]
            recovered = {k: v for k, v in recovered.items() if k in self.requested}
            self.snapshot = {
                k: v for k, v in self.snapshot.items() if k in {"canonical_url", "final_url", "redirect_chain"}
            }
            return self._finish(recovered, trace_file, validate=False)

    def _finish(self, data: dict, trace_file: str | None = None, validate: bool = True) -> tuple[dict, dict]:
        data = sanitize_source_data(data, self.secrets)
        if any(v.get("sanitized") for v in data.values()):
            self.issue(
                "source_text_sanitized",
                "Sensitive source strings were masked; affected values are partial, not verbatim.",
            )
        usage = self.usage()
        if not usage["usage_complete"]:
            self.issue("usage_incomplete")
        confirmed = any(v["status"] in ("ok", "partial") and v["value"] is not None for v in data.values())
        all_ok = bool(data) and all(v["status"] == "ok" for v in data.values())
        identity = bool(self.snapshot.get("canonical_url"))
        if self.invalid:
            status = "invalid_request"
        elif confirmed:
            status = (
                "complete"
                if all_ok and identity and usage["usage_complete"] and not self.issues and not self.technical
                else "partial"
            )
        else:
            status = "error" if self.technical else "unavailable"
        result = {
            "schema_version": "1.0",
            "run_id": self.id,
            "status": status,
            "started_at": self.started_at,
            "finished_at": utc(),
            "requested_fields": self.requested,
            "source_url": self.source,
            "canonical_url": self.snapshot.get("canonical_url"),
            "redirect_chain": self.snapshot.get("redirect_chain", []),
            "final_url": self.snapshot.get("final_url"),
            "captured_at": self.captured_at,
            "data": data,
            "usage": usage,
            "issues": self.issues,
            "trace_file": trace_file,
        }
        if validate:
            validate_result(result)
        # Generic trace regexes must never silently rewrite public source text.
        source_data = result.pop("data")
        result = scrub(result, self.secrets)
        result["data"] = source_data
        self.event("run_end", status=status, usage=usage, tool_calls=self.tools, issues=self.issues)
        return result, {
            "schema_version": "1.0",
            "run_id": self.id,
            "events": self.events,
            "status": status,
            "usage": usage,
        }


def valid_limits(seconds: object, tools: object, llm: object) -> bool:
    return (
        isinstance(seconds, (int, float))
        and not isinstance(seconds, bool)
        and math.isfinite(seconds)
        and seconds > 0
        and all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in (tools, llm))
    )


def require(condition: object) -> None:
    if not condition:
        raise Failure("result_schema_error", technical=True)


def validate_field(key: str, v: object) -> None:
    require(key in FIELDS and isinstance(v, dict) and v.get("status") in FIELD_STATUS and "value" in v)
    status, value = v["status"], v["value"]
    if status != "ok":
        require(
            isinstance(v.get("reason_code"), str)
            and bool(v["reason_code"])
            and isinstance(v.get("reason"), str)
            and bool(v["reason"])
        )
    if status in {"unconfirmed", "unavailable", "error"}:
        require(value is None)
        return
    require(value is not None)
    if key in {"display_name", "post_text"}:
        require(isinstance(value, str))
    elif key == "follower_count":
        require(isinstance(value, dict) and set(value) == {"display", "exact", "approximate"})
        require(isinstance(value["display"], str))
        for k in ["exact", "approximate"]:
            require(value[k] is None or isinstance(value[k], int) and not isinstance(value[k], bool) and value[k] >= 0)
        require(value["exact"] is not None or value["approximate"] is not None)
    else:
        require(isinstance(value, list) and (status != "partial" or bool(value)))
        prior = 0
        for item in value:
            require(
                isinstance(item, dict)
                and all(isinstance(item.get(k), str) for k in ["post_id", "url", "text", "parent_post_id"])
            )
            require(
                isinstance(item.get("order"), int) and not isinstance(item["order"], bool) and item["order"] > prior
            )
            prior = item["order"]


def validate_result(r: object) -> None:
    require(isinstance(r, dict) and isinstance(r.get("requested_fields"), list) and isinstance(r.get("data"), dict))
    require(all(isinstance(x, str) and x in FIELDS for x in r["requested_fields"]))
    require(
        len(set(r["requested_fields"])) == len(r["requested_fields"]) and set(r["data"]) == set(r["requested_fields"])
    )
    require(r.get("status") in {"invalid_request", "complete", "partial", "error", "unavailable"})
    for key, v in r["data"].items():
        validate_field(key, v)


def sanitize_source_data(data: dict, secrets: tuple[str, ...]) -> dict:
    def clean(x):
        if isinstance(x, dict):
            return {k: clean(v) for k, v in x.items()}
        if isinstance(x, list):
            return [clean(v) for v in x]
        if isinstance(x, str):
            for secret in secrets:
                if secret:
                    x = x.replace(secret, "[REDACTED]")
            return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED_CREDENTIAL_LIKE_STRING]", x)
        return x

    result = {}
    for name, value in data.items():
        safe = clean(value)
        if safe != value and isinstance(safe, dict) and safe.get("value") is not None:
            safe.update(
                status="partial",
                sanitized=True,
                reason_code="source_text_sanitized",
                reason="Credential or credential-like source strings were masked; this value is not verbatim.",
            )
        result[name] = safe
    return result


def atomic_json(path: str | Path, value: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
