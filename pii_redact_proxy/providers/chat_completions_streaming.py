"""SSE stream handler for OpenAI Chat Completions — buffer, de-redact, forward.

Named for the wire format rather than the provider, because it is not Grok's:
`grok` is the first provider to stream this shape, and the `openai` provider
will use the same codec for its chat surface when it lands. (Its Responses API
stream is a different taxonomy and gets its own codec then.)

Same three rules as `anthropic_streaming`: one upstream chunk becomes one
well-formed block, trailing partial placeholders are held back until the next
chunk completes them, and an exception mid-stream emits a structured error
rather than closing the socket. Mapped onto a taxonomy that has no event names
and no `.done` events: every chunk is `data: {...}` and the stream ends with a
literal `data: [DONE]`.

The hard part is tool calls. Anthropic gives us `content_block_stop` and the
Responses API a `.done` event carrying the whole `arguments` string; Chat
Completions only fragments them across `delta.tool_calls[].function.arguments`,
with the end implied by `finish_reason`. De-redacting a fragment is unsafe — a
placeholder split across two chunks would survive as literal `opposites.solar`
text — so we buffer the fragments, suppress them, and emit one consolidated
`tool_calls` on the chunk that carries `finish_reason`.
"""

import json
import logging
import re
from collections.abc import AsyncIterator

import httpx

from ..core.redactor import de_redact

logger = logging.getLogger(__name__)

# Trailing incomplete placeholder like "<INTERNAL_HOST" or "<CUSTOM_" — same
# regex as the Anthropic side; placeholder format is provider-agnostic.
_PARTIAL_PLACEHOLDER = re.compile(r"<[A-Z][A-Z0-9_]*$")

# Delta fields that carry free prose and therefore need placeholder-split carry.
_TEXT_FIELDS = ("content", "reasoning_content", "refusal")


def _format_data(data: dict) -> bytes:
    """Serialise one chunk — no `event:` line in this taxonomy."""
    return f"data: {json.dumps(data)}\n\n".encode()


def _format_raw(raw_data: str) -> bytes:
    """Forward a data line we couldn't parse as JSON, unmodified."""
    return f"data: {raw_data}\n\n".encode()


async def proxy_chat_stream(response: httpx.Response) -> AsyncIterator[bytes]:
    """Process a Chat Completions SSE stream, de-redacting text and tool arguments."""
    # Split-placeholder carry, keyed by (choice_index, field).
    carry: dict[tuple[int, str], str] = {}
    # Tool call argument fragments and their metadata, keyed by
    # (choice_index, tool_call_index).
    tool_args: dict[tuple[int, int], str] = {}
    tool_meta: dict[tuple[int, int], dict] = {}

    try:
        async for chunk in _process_stream(response, carry, tool_args, tool_meta):
            yield chunk
    except Exception as exc:
        logger.exception("proxy_chat_stream failed mid-stream: %s", exc)
        try:
            # Chat Completions has no error event type; an error object on a
            # chunk-shaped envelope is what OpenAI-protocol clients parse.
            yield _format_data({
                "error": {
                    "type": "pii_proxy_error",
                    "message": f"PII proxy failed during stream: {type(exc).__name__}: {exc}",
                },
            })
        except Exception:
            logger.exception("Failed to emit SSE error chunk")
    finally:
        try:
            await response.aclose()
        except Exception:
            pass


async def _process_stream(
    response: httpx.Response,
    carry: dict[tuple[int, str], str],
    tool_args: dict[tuple[int, int], str],
    tool_meta: dict[tuple[int, int], dict],
) -> AsyncIterator[bytes]:
    async for line in response.aiter_lines():
        if not line or not line.startswith("data:"):
            continue

        payload_str = line[len("data:"):].strip()
        if payload_str == "[DONE]":
            yield b"data: [DONE]\n\n"
            continue

        try:
            data = json.loads(payload_str)
        except json.JSONDecodeError:
            # Unparseable data line — forward untouched rather than drop it.
            yield _format_raw(payload_str)
            continue

        async for chunk in _emit_chunk(data, carry, tool_args, tool_meta):
            yield chunk


async def _emit_chunk(
    data: dict,
    carry: dict[tuple[int, str], str],
    tool_args: dict[tuple[int, int], str],
    tool_meta: dict[tuple[int, int], dict],
) -> AsyncIterator[bytes]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        # No choices at all is the usage-only trailer (or an envelope we don't
        # model) — nothing to de-redact, and suppressing it would lose usage.
        yield _format_data(data)
        return

    emit = False
    for choice in choices:
        if not isinstance(choice, dict):
            emit = True
            continue

        index = choice.get("index", 0)
        delta = choice.get("delta")
        finish_reason = choice.get("finish_reason")

        if isinstance(delta, dict):
            for field in _TEXT_FIELDS:
                text = delta.get(field)
                if not isinstance(text, str):
                    continue
                key = (index, field)
                if key in carry:
                    text = carry.pop(key) + text
                match = _PARTIAL_PLACEHOLDER.search(text)
                if match:
                    carry[key] = text[match.start():]
                    text = text[:match.start()]
                if text:
                    delta[field] = de_redact(text)
                else:
                    # Wholly held back — drop the field so we don't emit an
                    # empty string the client would append as real output.
                    delta.pop(field, None)

            if isinstance(delta.get("tool_calls"), list):
                _buffer_tool_calls(index, delta["tool_calls"], tool_args, tool_meta)
                delta.pop("tool_calls", None)

            if delta:
                emit = True

        if finish_reason is not None:
            flushed = _flush_tool_calls(index, tool_args, tool_meta)
            if flushed:
                if not isinstance(delta, dict):
                    delta = {}
                    choice["delta"] = delta
                delta["tool_calls"] = flushed
            # A carry fragment surviving past finish_reason is a truncated
            # placeholder; dropping it is right — emitting it would append
            # a stray `<CUSTOM_` to the client's accumulated text.
            for field in _TEXT_FIELDS:
                carry.pop((index, field), None)
            emit = True

    if emit:
        yield _format_data(data)


def _buffer_tool_calls(
    index: int,
    tool_calls: list,
    tool_args: dict[tuple[int, int], str],
    tool_meta: dict[tuple[int, int], dict],
) -> None:
    """Accumulate one chunk's tool-call fragments; emit nothing."""
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        key = (index, call.get("index", 0))
        meta = tool_meta.setdefault(key, {"index": call.get("index", 0)})
        for field in ("id", "type"):
            if call.get(field):
                meta[field] = call[field]
        function = call.get("function")
        if isinstance(function, dict):
            if function.get("name"):
                meta.setdefault("function", {})["name"] = function["name"]
            if isinstance(function.get("arguments"), str):
                tool_args[key] = tool_args.get(key, "") + function["arguments"]


def _flush_tool_calls(
    index: int,
    tool_args: dict[tuple[int, int], str],
    tool_meta: dict[tuple[int, int], dict],
) -> list:
    """Consolidate this choice's buffered tool calls into one de-redacted list."""
    keys = sorted(k for k in tool_meta if k[0] == index)
    flushed = []
    for key in keys:
        meta = tool_meta.pop(key)
        arguments = tool_args.pop(key, "")
        function = meta.setdefault("function", {})
        function["arguments"] = de_redact(arguments)
        flushed.append(meta)
    return flushed
