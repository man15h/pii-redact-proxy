"""SSE stream handler — buffer, de-redact, and forward Anthropic streaming responses."""

import json
import logging
import re
from collections.abc import AsyncIterator

import httpx

from ..core.redactor import de_redact

logger = logging.getLogger(__name__)

# Matches a trailing incomplete placeholder like "<INTERNAL_HOST" or "<CUSTOM_"
_PARTIAL_PLACEHOLDER = re.compile(r"<[A-Z][A-Z0-9_]*$")


def _format_event(event_type: str, data: dict) -> bytes:
    """Serialise a complete SSE event block: event header + single data line + blank."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n".encode()


def _format_raw(event_type: str | None, raw_data: str) -> bytes:
    """Forward an event whose data we couldn't parse as JSON, unmodified."""
    if event_type:
        return f"event: {event_type}\ndata: {raw_data}\n\n".encode()
    return f"data: {raw_data}\n\n".encode()


async def proxy_stream(response: httpx.Response) -> AsyncIterator[bytes]:
    """Process Anthropic SSE stream, de-redacting text and thinking deltas.

    Reads upstream SSE one *event block at a time* (event header + data line +
    blank separator), then re-emits each block as a complete, well-formed SSE
    event. This guarantees we never put two `data:` lines under a single
    `event:` header — the SSE spec joins multiple `data:` lines into one
    message body, which would corrupt downstream JSON parsing (the symptom we
    saw on Claude Code 2.1.110: `JSON Parse error: Unexpected EOF`).

    Text / thinking deltas may split placeholders across chunks (e.g.
    "<INTERNAL_" then "HOSTNAME_1>"). We hold back any trailing partial
    placeholder until the next chunk completes it, then de-redact the joined
    string. If a delta is fully held back, we suppress the entire event (not
    just the data line) so the client never sees a stray header.

    Tool-use `input_json_delta` chunks are buffered and emitted as a single
    de-redacted `content_block_delta` event at `content_block_stop`, with its
    own event header so the client can dispatch it correctly.

    `signature_delta` events (for extended thinking) are passed through
    unmodified — the signature is cryptographically bound to the upstream
    thinking and must not be altered.
    """
    # Buffer for accumulating tool_use input JSON
    tool_input_buffers: dict[int, str] = {}
    # Track which content block indices are tool_use
    tool_use_indices: set[int] = set()
    # Carry-over buffer for text deltas with trailing partial placeholders
    text_carry: dict[int, str] = {}
    # Carry-over buffer for thinking deltas (same split-placeholder problem)
    thinking_carry: dict[int, str] = {}

    try:
        async for chunk in _process_stream(
            response,
            tool_input_buffers,
            tool_use_indices,
            text_carry,
            thinking_carry,
        ):
            yield chunk
    except Exception as exc:
        # Any exception inside the generator silently closes the SSE stream,
        # which the client sees as "JSON Parse error: Unexpected EOF" (or, on
        # Claude Code 2.1.105+, triggers the non-streaming fallback retry that
        # often fails the same way). Emit a structured SSE error event so the
        # client gets a clean, recognisable failure instead.
        logger.exception("proxy_stream failed mid-stream: %s", exc)
        try:
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
    tool_input_buffers: dict[int, str],
    tool_use_indices: set[int],
    text_carry: dict[int, str],
    thinking_carry: dict[int, str],
) -> AsyncIterator[bytes]:
    """Read upstream SSE one event block at a time and dispatch to _emit_event."""
    event_header: str | None = None
    data_payload: dict | None = None
    raw_payload: str | None = None

    async for line in response.aiter_lines():
        if not line:
            # Blank line terminates the current event — emit it.
            async for chunk in _emit_event(
                event_header,
                data_payload,
                raw_payload,
                tool_input_buffers,
                tool_use_indices,
                text_carry,
                thinking_carry,
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
                # OpenAI-style sentinel; Anthropic doesn't emit it but be defensive.
                raw_payload = "[DONE]"
            else:
                try:
                    data_payload = json.loads(payload_str)
                except json.JSONDecodeError:
                    # Forward unparseable data unmodified rather than dropping it.
                    raw_payload = payload_str
        # Silently ignore other SSE field types (id:, retry:, comments).

    # Flush any final event that wasn't terminated by a blank line.
    if event_header is not None or data_payload is not None or raw_payload is not None:
        async for chunk in _emit_event(
            event_header,
            data_payload,
            raw_payload,
            tool_input_buffers,
            tool_use_indices,
            text_carry,
            thinking_carry,
        ):
            yield chunk


async def _emit_event(
    event_header: str | None,
    data_payload: dict | None,
    raw_payload: str | None,
    tool_input_buffers: dict[int, str],
    tool_use_indices: set[int],
    text_carry: dict[int, str],
    thinking_carry: dict[int, str],
) -> AsyncIterator[bytes]:
    """Emit zero-or-more complete SSE event blocks for one upstream event."""
    # Couldn't parse data — pass through unmodified to avoid dropping anything.
    if raw_payload is not None:
        yield _format_raw(event_header, raw_payload)
        return

    if data_payload is None:
        # Header with no data, or a completely empty event — drop it.
        return

    event_type = event_header or data_payload.get("type", "message")

    if event_type == "content_block_delta":
        delta = data_payload.get("delta", {})
        delta_type = delta.get("type", "")
        index = data_payload.get("index", 0)

        if delta_type == "text_delta":
            text = delta.get("text", "")
            if index in text_carry:
                text = text_carry.pop(index) + text
            match = _PARTIAL_PLACEHOLDER.search(text)
            if match:
                text_carry[index] = text[match.start():]
                text = text[:match.start()]
            if text:
                delta["text"] = de_redact(text)
                data_payload["delta"] = delta
                yield _format_event(event_type, data_payload)
            # else: held back entirely — suppress the whole event.

        elif delta_type == "input_json_delta":
            # Buffer; emit one consolidated event at content_block_stop.
            partial = delta.get("partial_json", "")
            tool_input_buffers.setdefault(index, "")
            tool_input_buffers[index] += partial
            tool_use_indices.add(index)

        elif delta_type == "thinking_delta":
            thinking = delta.get("thinking", "")
            if index in thinking_carry:
                thinking = thinking_carry.pop(index) + thinking
            match = _PARTIAL_PLACEHOLDER.search(thinking)
            if match:
                thinking_carry[index] = thinking[match.start():]
                thinking = thinking[:match.start()]
            if thinking:
                delta["thinking"] = de_redact(thinking)
                data_payload["delta"] = delta
                yield _format_event(event_type, data_payload)

        elif delta_type == "signature_delta":
            # Signature is cryptographic — pass through verbatim.
            yield _format_event(event_type, data_payload)

        else:
            yield _format_event(event_type, data_payload)

    elif event_type == "content_block_start":
        content_block = data_payload.get("content_block", {})
        block_type = content_block.get("type")
        if block_type == "text":
            content_block["text"] = de_redact(content_block.get("text", ""))
            data_payload["content_block"] = content_block
        elif block_type == "thinking":
            if "thinking" in content_block:
                content_block["thinking"] = de_redact(content_block.get("thinking", ""))
            data_payload["content_block"] = content_block
        yield _format_event(event_type, data_payload)

    elif event_type == "content_block_stop":
        index = data_payload.get("index", 0)

        # Flush any held-back text — emit as its own complete delta event.
        if index in text_carry:
            remainder = de_redact(text_carry.pop(index))
            if remainder:
                yield _format_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": remainder},
                })

        # Flush any held-back thinking — same treatment.
        if index in thinking_carry:
            remainder = de_redact(thinking_carry.pop(index))
            if remainder:
                yield _format_event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "thinking_delta", "thinking": remainder},
                })

        # Flush buffered tool_use input as one consolidated delta event.
        if index in tool_use_indices and index in tool_input_buffers:
            raw_json = tool_input_buffers.pop(index)
            de_redacted = de_redact(raw_json)
            yield _format_event("content_block_delta", {
                "type": "content_block_delta",
                "index": index,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": de_redacted,
                },
            })
            tool_use_indices.discard(index)

        # Finally, emit the stop event itself.
        yield _format_event(event_type, data_payload)

    else:
        yield _format_event(event_type, data_payload)
