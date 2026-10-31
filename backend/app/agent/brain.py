"""Model provider abstraction.

Four back-ends behind one interface:

    anthropic | openai | ollama | offline

All HTTP is done with the standard library, deliberately.  A hackathon demo
should not be able to fail because a vendor SDK minor-version changed, and
`urllib` has no version to drift.

`offline` is a deterministic reasoner that drives the *same tools* through the
*same loop*.  It exists so that the demonstration still runs with no API key,
no network and no budget -- and so there is an honest rules-based baseline to
compare the model against (specification section 10).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..events import bus


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class BrainReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class BrainError(RuntimeError):
    pass


_RETRY_AFTER = re.compile(r"after\s+(\d+(?:\.\d+)?)\s*second", re.I)


def _retry_delay(attempt: int, detail: str, header: str | None) -> float:
    """How long to wait before retrying a throttled or transient request.

    Providers advertise this three ways: a Retry-After header, a hint in the
    error body ("try again after 1 seconds"), or nothing at all. A free tier
    capped at N requests per minute needs roughly 60/N seconds of spacing, so
    the floor matters more than the hint -- several providers under-report it.
    """
    hinted = 0.0
    if header:
        try:
            hinted = float(header)
        except ValueError:
            hinted = 0.0
    m = _RETRY_AFTER.search(detail or "")
    if m:
        hinted = max(hinted, float(m.group(1)))
    backoff = min(settings.llm_retry_base_s * (2 ** attempt), settings.llm_retry_max_s)
    return max(hinted, backoff)


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str],
               timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    last: BrainError | None = None

    for attempt in range(max(settings.llm_retries, 0) + 1):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:600]
            last = BrainError(f"HTTP {exc.code} from {url}: {detail}")
            # 429 = throttled, 5xx = transient. Anything else is our fault.
            if exc.code != 429 and exc.code < 500:
                raise last from exc
            if attempt >= settings.llm_retries:
                raise last from exc
            delay = _retry_delay(attempt, detail, exc.headers.get("Retry-After"))
            bus.emit("llm_throttled", {"status": exc.code, "waiting_s": round(delay, 1),
                                       "attempt": attempt + 1})
            time.sleep(delay)
        except urllib.error.URLError as exc:
            last = BrainError(f"cannot reach {url}: {exc.reason}")
            if attempt >= settings.llm_retries:
                raise last from exc
            time.sleep(_retry_delay(attempt, "", None))
        except TimeoutError as exc:
            last = BrainError(f"timed out calling {url}")
            if attempt >= settings.llm_retries:
                raise last from exc
            time.sleep(_retry_delay(attempt, "", None))

    raise last or BrainError("request failed")


class Brain:
    name = "base"
    is_model = True

    def converse(self, system: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]]) -> BrainReply:
        raise NotImplementedError


# ---------------------------------------------------------------------------
class AnthropicBrain(Brain):
    name = "anthropic"
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self) -> None:
        if not settings.anthropic_api_key:
            raise BrainError("ANTHROPIC_API_KEY is not set")
        self.model = settings.model_deep
        self.fallback = settings.model_fallback
        self._fell_back = False

    def converse(self, system, messages, tools) -> BrainReply:
        reply = self._once(self.model, system, messages, tools)
        # A refusal is a safety-classifier outcome, not a transport error, so
        # retrying the same model is pointless -- switch to the fallback.
        if reply.stop_reason == "refusal" and self.fallback \
                and self.fallback != self.model:
            bus.emit("model_refusal", {"model": self.model,
                                       "falling_back_to": self.fallback,
                                       "detail": reply.raw.get("stop_details")})
            if not self._fell_back:
                self._fell_back = True
            reply = self._once(self.fallback, system, messages, tools)
            if reply.stop_reason == "refusal":
                raise BrainError(
                    f"both {self.model} and {self.fallback} declined this request: "
                    f"{reply.raw.get('stop_details')}")
        return reply

    def _once(self, model, system, messages, tools) -> BrainReply:
        payload = {
            "model": model,
            "max_tokens": settings.max_tokens,
            "system": system,
            "messages": messages,
            "tools": [{"name": t["name"], "description": t["description"],
                       "input_schema": t["input_schema"]} for t in tools],
        }
        data = _post_json(self.URL, payload, {
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
        }, settings.llm_timeout_s)

        text, calls = "", []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(id=block["id"], name=block["name"],
                                      arguments=block.get("input", {}) or {}))
        u = data.get("usage", {}) or {}
        return BrainReply(
            text=text, tool_calls=calls,
            stop_reason=data.get("stop_reason", "end_turn"),
            usage={"input_tokens": u.get("input_tokens", 0),
                   "output_tokens": u.get("output_tokens", 0)},
            raw=data,
        )

    @staticmethod
    def assistant_turn(reply: BrainReply) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if reply.text:
            content.append({"type": "text", "text": reply.text})
        for c in reply.tool_calls:
            content.append({"type": "tool_use", "id": c.id, "name": c.name,
                            "input": c.arguments})
        return {"role": "assistant", "content": content or [{"type": "text", "text": "."}]}

    @staticmethod
    def tool_results_turn(results: list[tuple[str, Any]]) -> dict[str, Any]:
        return {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid,
             "content": json.dumps(out, default=str)[:20000]}
            for tid, out in results]}


# ---------------------------------------------------------------------------
class OpenAIBrain(Brain):
    """Any OpenAI-compatible chat-completions endpoint.

    Subclassed for Kimi/Moonshot below; the wire format is identical, only the
    base URL, key and model name differ.
    """
    name = "openai"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, label: str = "openai") -> None:
        self.api_key = api_key if api_key is not None else settings.openai_api_key
        if not self.api_key:
            raise BrainError(
                f"no API key configured for '{label}' -- set it in .env")
        self.base = (base_url or settings.openai_base_url).rstrip("/")
        self.URL = f"{self.base}/chat/completions"
        self.model = model or settings.openai_model
        self.name = label

    def converse(self, system, messages, tools) -> BrainReply:
        reply = self._once(self.model, system, messages, tools)
        # A refusal is a safety-classifier outcome, not a transport error, so
        # retrying the same model is pointless -- switch to the fallback.
        if reply.stop_reason == "refusal" and self.fallback \
                and self.fallback != self.model:
            bus.emit("model_refusal", {"model": self.model,
                                       "falling_back_to": self.fallback,
                                       "detail": reply.raw.get("stop_details")})
            if not self._fell_back:
                self._fell_back = True
            reply = self._once(self.fallback, system, messages, tools)
            if reply.stop_reason == "refusal":
                raise BrainError(
                    f"both {self.model} and {self.fallback} declined this request: "
                    f"{reply.raw.get('stop_details')}")
        return reply

    def _once(self, model, system, messages, tools) -> BrainReply:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system}] + messages,
            "tools": [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["input_schema"]}} for t in tools],
            "max_tokens": settings.max_tokens,
        }
        data = _post_json(self.URL, payload,
                          {"Authorization": f"Bearer {self.api_key}"},
                          settings.llm_timeout_s)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        calls = [ToolCall(id=tc.get("id", f"call_{i}"),
                          name=tc["function"]["name"],
                          arguments=json.loads(tc["function"].get("arguments") or "{}"))
                 for i, tc in enumerate(msg.get("tool_calls") or [])]
        u = data.get("usage", {}) or {}
        return BrainReply(
            text=msg.get("content") or "", tool_calls=calls,
            stop_reason=choice.get("finish_reason", "stop"),
            usage={"input_tokens": u.get("prompt_tokens", 0),
                   "output_tokens": u.get("completion_tokens", 0)},
            raw=data,
        )

    @staticmethod
    def assistant_turn(reply: BrainReply) -> dict[str, Any]:
        m: dict[str, Any] = {"role": "assistant", "content": reply.text or None}
        if reply.tool_calls:
            m["tool_calls"] = [{"id": c.id, "type": "function",
                                "function": {"name": c.name,
                                             "arguments": json.dumps(c.arguments)}}
                               for c in reply.tool_calls]
        return m

    @staticmethod
    def tool_results_turn(results: list[tuple[str, Any]]) -> list[dict[str, Any]]:
        return [{"role": "tool", "tool_call_id": tid,
                 "content": json.dumps(out, default=str)[:20000]} for tid, out in results]


class KimiBrain(OpenAIBrain):
    """Kimi (Moonshot AI). OpenAI-compatible, with native tool calling.

    Default endpoint is the international one (api.moonshot.ai). Accounts
    created on the mainland console use api.moonshot.cn -- set
    RESTORIO_KIMI_BASE_URL if a call returns 401 with a valid-looking key.
    """
    def __init__(self) -> None:
        super().__init__(api_key=settings.kimi_api_key,
                         base_url=settings.kimi_base_url,
                         model=settings.kimi_model, label="kimi")


# ---------------------------------------------------------------------------
class OllamaBrain(Brain):
    name = "ollama"

    def __init__(self) -> None:
        self.model = settings.ollama_model
        self.url = settings.ollama_host.rstrip("/") + "/api/chat"

    def converse(self, system, messages, tools) -> BrainReply:
        reply = self._once(self.model, system, messages, tools)
        # A refusal is a safety-classifier outcome, not a transport error, so
        # retrying the same model is pointless -- switch to the fallback.
        if reply.stop_reason == "refusal" and self.fallback \
                and self.fallback != self.model:
            bus.emit("model_refusal", {"model": self.model,
                                       "falling_back_to": self.fallback,
                                       "detail": reply.raw.get("stop_details")})
            if not self._fell_back:
                self._fell_back = True
            reply = self._once(self.fallback, system, messages, tools)
            if reply.stop_reason == "refusal":
                raise BrainError(
                    f"both {self.model} and {self.fallback} declined this request: "
                    f"{reply.raw.get('stop_details')}")
        return reply

    def _once(self, model, system, messages, tools) -> BrainReply:
        payload = {
            "model": model, "stream": False,
            "messages": [{"role": "system", "content": system}] + messages,
            "tools": [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["input_schema"]}} for t in tools],
        }
        data = _post_json(self.url, payload, {}, settings.llm_timeout_s)
        msg = data.get("message", {}) or {}
        calls = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append(ToolCall(id=f"call_{i}", name=fn.get("name", ""),
                                  arguments=args or {}))
        return BrainReply(text=msg.get("content") or "", tool_calls=calls,
                          stop_reason="stop", raw=data)

    @staticmethod
    def assistant_turn(reply: BrainReply) -> dict[str, Any]:
        return OpenAIBrain.assistant_turn(reply)

    @staticmethod
    def tool_results_turn(results: list[tuple[str, Any]]) -> list[dict[str, Any]]:
        return [{"role": "tool", "content": json.dumps(out, default=str)[:20000]}
                for _tid, out in results]


def make_brain(provider: str | None = None) -> Brain:
    p = provider or settings.resolved_provider()
    if p == "anthropic":
        return AnthropicBrain()
    if p == "openai":
        return OpenAIBrain()
    if p in ("kimi", "moonshot"):
        return KimiBrain()
    if p == "ollama":
        return OllamaBrain()
    if p == "sloppy":                      # evaluation control, see sloppy.py
        from .sloppy import SloppyBrain
        return SloppyBrain()
    from .offline import OfflineBrain
    return OfflineBrain()


def probe(provider: str | None = None) -> dict[str, Any]:
    """Cheap reachability check used by the UI's provider badge."""
    p = provider or settings.resolved_provider()
    started = time.time()
    try:
        brain = make_brain(p)
        if not brain.is_model:
            return {"provider": p, "ok": True, "detail": "deterministic reasoner",
                    "latency_ms": 0}
        reply = brain.converse("Reply with the single word: ready.",
                               [{"role": "user", "content": "ready?"}], [])
        return {"provider": p, "ok": True,
                "detail": (reply.text or "").strip()[:60] or "ok",
                "latency_ms": int((time.time() - started) * 1000)}
    except Exception as exc:                          # noqa: BLE001
        return {"provider": p, "ok": False, "detail": str(exc)[:240],
                "latency_ms": int((time.time() - started) * 1000)}
