"""SSE stream handler for the OpenAI Responses API wire format — buffer, de-redact, forward.

Named for the wire format rather than the provider, same reasoning as
`chat_completions_streaming`: `chatgpt` is the first provider to stream this
shape, and the deferred `openai` (API-key) provider will use the same codec
for its Responses API surface when it lands.

Mirror of `anthropic_streaming`: the structural concerns (one upstream event
becomes one well-formed `event:`+`data:`+blank block, trailing partial
placeholders are held back until the next event completes them, an exception
mid-stream emits a structured error rather than closing the socket) are
identical; only the event taxonomy and data shapes change.
"""

import json
import logging
import re
from collections.abc import AsyncIterator

import httpx

from ..core.redactor import de_redact

logger = logging.getLogger(__name__)

# Trailing incomplete placeholder like "<INTERNAL_HOST" or "<CUSTOM_" — same
# regex as the Anthropic and Chat Completions codecs; placeholder format is
# provider-agnostic.
_PARTIAL_PLACEHOLDER = re.compile(r"<[A-Z][A-Z0-9_]*$")


def _format_event(event_type: str, data: dict) -> bytes:
    """Serialise one complete SSE event block."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()


def _format_raw(event_type: str | None, raw_data: str) -> bytes:
    """Forward an event whose data we couldn't parse as JSON, unmodified."""
    if event_type:
        return f"event: {event_type}\ndata: {raw_data}\n\n".encode()
    return f"data: {raw_data}\n\n".encode()


async def proxy_responses_stream(response: httpx.Response) -> AsyncIterator[bytes]:
    """Process a Responses API SSE stream, de-redacting text and arguments.

    - `response.output_text.delta` / `.done` — de-redact text, with split-
      placeholder carry per (item_id, content_index).
    - `response.reasoning_summary_text.delta` / `.done` — same, keyed by
      (item_id, summary_index).
    - `response.refusal.delta` / `.done` — same, keyed by (item_id, content_index).
    - `response.function_call_arguments.delta` — buffered per item_id, NOT
      emitted; on `.done` we emit one consolidated event with de-redacted
      `arguments`.
    - `response.output_item.added/.done`, `response.content_part.added/.done`,
      `response.completed` — walked defensively to catch any text/arguments
      fields the upstream included as a final-state snapshot.
    - Everything else is passed through as a complete event block.
    """
    # Carry buffers for split placeholders across deltas.
    text_carry: dict[tuple[str, int], str] = {}
    reasoning_carry: dict[tuple[str, int], str] = {}
    refusal_carry: dict[tuple[str, int], str] = {}
    # Buffer for function call arguments (per item_id); we suppress per-delta
    # events and emit one consolidated `.done` instead.
    function_args_buffers: dict[str, str] = {}

    try:
        async for chunk in _process_stream(
            response,
            text_carry,
            reasoning_carry,
            refusal_carry,
            function_args_buffers,
        ):
            yield chunk
    except Exception as exc:
        logger.exception("proxy_responses_stream failed mid-stream: %s", exc)
        try:
            # OpenAI's structured error event shape — flat `error` object on a
            # `response.failed` envelope is the closest analogue. The CLI
            # tolerates an unknown `event: error` block too, but the named
            # event is more recognisable.
            yield _format_event("error", {
                "type": "error",
                "error": {
                    "type": "pii_proxy_error",
                    "message": f"PII proxy failed during stream: {type(exc).__name__}: {exc}",
                },
            })
        except Exception:
            logger.exception("Failed to emit SSE error event")
    finally:
        try:
            await response.aclose()
        except Exception:
            pass


async def _process_stream(
    response: httpx.Response,
    text_carry: dict[tuple[str, int], str],
    reasoning_carry: dict[tuple[str, int], str],
    refusal_carry: dict[tuple[str, int], str],
    function_args_buffers: dict[str, str],
) -> AsyncIterator[bytes]:
    """Read upstream SSE one event block at a time and dispatch to _emit_event."""
    event_header: str | None = None
    data_payload: dict | None = None
    raw_payload: str | None = None

    async for line in response.aiter_lines():
        if not line:
            async for chunk in _emit_event(
                event_header,
                data_payload,
                raw_payload,
                text_carry,
                reasoning_carry,
                refusal_carry,
                function_args_buffers,
            ):
                yield chunk
            event_header = None
            data_payload = None
            raw_payload = None
            continue

        if line.startswith("event:"):
            event_header = line[len("event:"):].strip()
        elif line.startswith("data:"):
            payload_str = line[len("data:"):].strip()
            if payload_str == "[DONE]":
                raw_payload = "[DONE]"
            else:
                try:
                    data_payload = json.loads(payload_str)
                except json.JSONDecodeError:
                    raw_payload = payload_str

    # Flush any final event that wasn't terminated by a blank line.
    if event_header is not None or data_payload is not None or raw_payload is not None:
        async for chunk in _emit_event(
            event_header,
            data_payload,
            raw_payload,
            text_carry,
            reasoning_carry,
            refusal_carry,
            function_args_buffers,
        ):
            yield chunk


async def _emit_event(
    event_header: str | None,
    data_payload: dict | None,
    raw_payload: str | None,
    text_carry: dict[tuple[str, int], str],
    reasoning_carry: dict[tuple[str, int], str],
    refusal_carry: dict[tuple[str, int], str],
    function_args_buffers: dict[str, str],
) -> AsyncIterator[bytes]:
    if raw_payload is not None:
        yield _format_raw(event_header, raw_payload)
        return

    if data_payload is None:
        return

    event_type = event_header or data_payload.get("type", "")

    # ---------- text deltas (carry partial placeholders) ----------
    if event_type == "response.output_text.delta":
        key = (data_payload.get("item_id", ""), data_payload.get("content_index", 0))
        text = data_payload.get("delta", "")
        if key in text_carry:
            text = text_carry.pop(key) + text
        match = _PARTIAL_PLACEHOLDER.search(text)
        if match:
            text_carry[key] = text[match.start():]
            text = text[:match.start()]
        if text:
            data_payload["delta"] = de_redact(text)
            yield _format_event(event_type, data_payload)
        # else: fully held back — suppress the entire event.
        return

    if event_type == "response.output_text.done":
        key = (data_payload.get("item_id", ""), data_payload.get("content_index", 0))
        # Drop any leftover carry — the `.done` event carries the full text
        # authoritatively, so the carry fragment is redundant (and would
        # otherwise leak as a stray delta after `.done`).
        text_carry.pop(key, None)
        if isinstance(data_payload.get("text"), str):
            data_payload["text"] = de_redact(data_payload["text"])
        yield _format_event(event_type, data_payload)
        return

    # ---------- reasoning summary deltas ----------
    if event_type == "response.reasoning_summary_text.delta":
        key = (data_payload.get("item_id", ""), data_payload.get("summary_index", 0))
        text = data_payload.get("delta", "")
        if key in reasoning_carry:
            text = reasoning_carry.pop(key) + text
        match = _PARTIAL_PLACEHOLDER.search(text)
        if match:
            reasoning_carry[key] = text[match.start():]
            text = text[:match.start()]
        if text:
            data_payload["delta"] = de_redact(text)
            yield _format_event(event_type, data_payload)
        return

    if event_type == "response.reasoning_summary_text.done":
        key = (data_payload.get("item_id", ""), data_payload.get("summary_index", 0))
        reasoning_carry.pop(key, None)
        if isinstance(data_payload.get("text"), str):
            data_payload["text"] = de_redact(data_payload["text"])
        yield _format_event(event_type, data_payload)
        return

    # ---------- refusal deltas ----------
    if event_type == "response.refusal.delta":
        key = (data_payload.get("item_id", ""), data_payload.get("content_index", 0))
        text = data_payload.get("delta", "")
        if key in refusal_carry:
            text = refusal_carry.pop(key) + text
        match = _PARTIAL_PLACEHOLDER.search(text)
        if match:
            refusal_carry[key] = text[match.start():]
            text = text[:match.start()]
        if text:
            data_payload["delta"] = de_redact(text)
            yield _format_event(event_type, data_payload)
        return

    if event_type == "response.refusal.done":
        key = (data_payload.get("item_id", ""), data_payload.get("content_index", 0))
        refusal_carry.pop(key, None)
        if isinstance(data_payload.get("refusal"), str):
            data_payload["refusal"] = de_redact(data_payload["refusal"])
        yield _format_event(event_type, data_payload)
        return

    # ---------- function call arguments (buffer per item, emit only at .done) ----------
    if event_type == "response.function_call_arguments.delta":
        item_id = data_payload.get("item_id", "")
        function_args_buffers[item_id] = function_args_buffers.get(item_id, "") + data_payload.get("delta", "")
        # Suppress per-delta events — split placeholders inside JSON would
        # corrupt the de-redaction. We emit one consolidated event at .done.
        return

    if event_type == "response.function_call_arguments.done":
        item_id = data_payload.get("item_id", "")
        # Prefer upstream's authoritative `arguments` field; the buffer is just
        # to ensure we don't emit fragmented events. They should match, but if
        # they ever diverge, upstream is the source of truth.
        arguments = data_payload.get("arguments")
        if not isinstance(arguments, str):
            arguments = function_args_buffers.get(item_id, "")
        function_args_buffers.pop(item_id, None)
        data_payload["arguments"] = de_redact(arguments)
        yield _format_event(event_type, data_payload)
        return

    # ---------- envelope events with embedded snapshots (defense in depth) ----------
    if event_type in ("response.output_item.added", "response.output_item.done"):
        item = data_payload.get("item")
        if isinstance(item, dict):
            _de_redact_output_item_inplace(item)
        yield _format_event(event_type, data_payload)
        return

    if event_type in ("response.content_part.added", "response.content_part.done"):
        part = data_payload.get("part")
        if isinstance(part, dict):
            _de_redact_content_part_inplace(part)
        yield _format_event(event_type, data_payload)
        return

    if event_type == "response.completed":
        # Final-state snapshot. Codex CLI accumulates deltas on its own, but
        # any client that reads response.completed.response.output[] directly
        # (e.g. tools that skip the streaming UI) needs de-redacted values.
        outer = data_payload.get("response")
        if isinstance(outer, dict):
            output = outer.get("output")
            if isinstance(output, list):
                for item in output:
                    _de_redact_output_item_inplace(item)
        yield _format_event(event_type, data_payload)
        return

    # Plain envelopes — no PII inside.
    if event_type in (
        "response.created",
        "response.in_progress",
        "response.queued",
        "response.failed",
        "response.incomplete",
    ):
        yield _format_event(event_type, data_payload)
        return

    # Anything else — passthrough. Mirrors anthropic_streaming.py's final case.
    yield _format_event(event_type, data_payload)


def _de_redact_output_item_inplace(item: dict) -> None:
    """De-redact text/arguments inside one item from response.output[]."""
    item_type = item.get("type", "")
    if item_type == "message":
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    _de_redact_content_part_inplace(part)
        elif isinstance(content, str):
            item["content"] = de_redact(content)
    elif item_type == "function_call":
        if isinstance(item.get("arguments"), str):
            item["arguments"] = de_redact(item["arguments"])
    elif item_type == "reasoning":
        summary = item.get("summary")
        if isinstance(summary, list):
            for part in summary:
                if isinstance(part, dict):
                    _de_redact_content_part_inplace(part)
    elif item_type == "refusal":
        if isinstance(item.get("refusal"), str):
            item["refusal"] = de_redact(item["refusal"])


def _de_redact_content_part_inplace(part: dict) -> None:
    if isinstance(part.get("text"), str):
        part["text"] = de_redact(part["text"])
    if isinstance(part.get("refusal"), str):
        part["refusal"] = de_redact(part["refusal"])
