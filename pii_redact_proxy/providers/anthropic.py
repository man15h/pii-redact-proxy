"""Anthropic Messages API — the wire format Claude Code speaks.

The only provider deployed today: every agent points ANTHROPIC_BASE_URL at this
proxy, so nothing reaches Anthropic without passing through here.
"""

import json
import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..core.redactor import redact, de_redact
from ..core.session import mapper
from ..core.jsonwalk import redact_json_values as _redact_json_values
from .base import forward_headers
from .anthropic_streaming import proxy_stream

NAME = "anthropic"
# Bump on every behaviour-affecting deploy so /version proves what's running.
VERSION = "2026-08-21.2"
UPSTREAM_BASE_URL = "https://api.anthropic.com"

# Auth and API-version headers only. Anthropic needs no vendor prefix family,
# so that half of the contract is empty here rather than absent.
FORWARD_HEADERS = (
    "x-api-key",
    "authorization",
    "anthropic-version",
    "anthropic-beta",
    "content-type",
)
FORWARD_HEADER_PREFIXES = ()

logger = logging.getLogger("pii-proxy")

# Set by register(). Module-level rather than threaded through every helper:
# one process serves exactly one provider, which is what PROVIDER decides.
_client: httpx.AsyncClient = None


def register(app: FastAPI, http_client: httpx.AsyncClient) -> None:
    """Attach the Anthropic routes. Order matters — see passthrough."""
    global _client
    _client = http_client
    app.post("/v1/messages")(proxy_messages)
    app.post("/v1/messages/count_tokens")(proxy_count_tokens)
    # LAST: a catch-all shadows anything declared after it.
    app.api_route("/{path:path}",
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH"])(passthrough)


async def proxy_messages(request: Request):
    """Proxy Messages API: redact request, forward, de-redact response."""
    # Read and parse request body
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    is_streaming = payload.get("stream", False)
    mappings_before = len(mapper.get_mappings_snapshot())

    # Redact request — fail CLOSED on any redaction error. Forwarding an
    # unredacted request would defeat the entire purpose of this proxy, so a
    # 500 back to the client is the correct failure mode.
    try:
        # Snapshot original texts for debug logging
        if logger.isEnabledFor(logging.DEBUG):
            original_texts = _extract_texts(payload)

        # Redact message contents
        _redact_messages(payload)

        # Redact system prompt
        if "system" in payload:
            system = payload["system"]
            if isinstance(system, str):
                payload["system"] = redact(system)
            elif isinstance(system, list):
                payload["system"] = _redact_content_blocks(system)

        mappings_after = len(mapper.get_mappings_snapshot())
        new_redactions = mappings_after - mappings_before

        # Log redacted texts at DEBUG level
        if logger.isEnabledFor(logging.DEBUG):
            redacted_texts = _extract_texts(payload)
            for orig, redc in zip(original_texts, redacted_texts):
                if orig != redc:
                    logger.debug("ORIGINAL : %s", orig[:500])
                    logger.debug("REDACTED : %s", redc[:500])
    except Exception:
        logger.exception("Redaction failed — refusing to forward unredacted request")
        return JSONResponse(
            {"error": {"type": "pii_proxy_error", "message": "Redaction failed; request blocked to prevent PII leak."}},
            status_code=500,
        )

    # Forward auth and version headers from client
    forward = forward_headers(request, FORWARD_HEADERS, FORWARD_HEADER_PREFIXES)

    logger.info(
        "Forwarding request: model=%s stream=%s messages=%d new_redactions=%d total_mappings=%d",
        payload.get("model", "unknown"),
        is_streaming,
        len(payload.get("messages", [])),
        new_redactions,
        mappings_after,
    )

    if is_streaming:
        return await _handle_streaming(payload, forward)
    else:
        return await _handle_non_streaming(payload, forward)


async def proxy_count_tokens(request: Request):
    """Proxy token-counting API: redact request body, forward, return response as-is.

    The response is just `{"input_tokens": N}` — no PII to de-redact. We must
    redact the body so the count reflects what will actually be sent on the
    real /v1/messages call (redacted placeholders have different token counts
    than raw strings).
    """
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    try:
        _redact_messages(payload)
        if "system" in payload:
            system = payload["system"]
            if isinstance(system, str):
                payload["system"] = redact(system)
            elif isinstance(system, list):
                payload["system"] = _redact_content_blocks(system)
    except Exception:
        # Fail closed — forwarding an unredacted body to count_tokens would
        # leak the same PII we're trying to protect on /v1/messages.
        logger.exception("count_tokens redaction failed — refusing to forward unredacted")
        return JSONResponse(
            {"error": {"type": "pii_proxy_error", "message": "Redaction failed; request blocked to prevent PII leak."}},
            status_code=500,
        )

    forward = forward_headers(request, FORWARD_HEADERS, FORWARD_HEADER_PREFIXES)

    response = await _client.post(
        "/v1/messages/count_tokens",
        json=payload,
        headers=forward,
    )

    try:
        return JSONResponse(response.json(), status_code=response.status_code)
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": response.text}, status_code=response.status_code)


async def _handle_non_streaming(payload: dict, headers: dict) -> JSONResponse:
    """Forward non-streaming request and de-redact response."""
    response = await _client.post(
        "/v1/messages",
        json=payload,
        headers=headers,
    )

    if response.status_code != 200:
        try:
            error = response.json()
        except (json.JSONDecodeError, ValueError):
            error = {"error": response.text}
        return JSONResponse(error, status_code=response.status_code)

    result = response.json()

    # De-redact response content
    _de_redact_response(result)

    return JSONResponse(result, status_code=200)


async def _handle_streaming(payload: dict, headers: dict) -> StreamingResponse:
    """Forward streaming request and de-redact SSE events."""
    response = await _client.send(
        _client.build_request(
            "POST",
            "/v1/messages",
            json=payload,
            headers=headers,
        ),
        stream=True,
    )

    if response.status_code != 200:
        body = await response.aread()
        await response.aclose()
        try:
            error = json.loads(body)
        except json.JSONDecodeError:
            error = {"error": body.decode()}
        return JSONResponse(error, status_code=response.status_code)

    return StreamingResponse(
        proxy_stream(response),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "connection": "keep-alive",
        },
    )


def _redact_messages(payload: dict):
    """Redact PII in messages[].content recursively."""
    for message in payload.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = redact(content)
        elif isinstance(content, list):
            message["content"] = _redact_content_blocks(content)


def _redact_content_blocks(blocks: list) -> list:
    """Redact PII in a list of content blocks."""
    for block in blocks:
        block_type = block.get("type", "")

        if block_type == "text":
            block["text"] = redact(block.get("text", ""))

        elif block_type == "tool_result":
            # tool_result can have string or list content
            result_content = block.get("content")
            if isinstance(result_content, str):
                block["content"] = redact(result_content)
            elif isinstance(result_content, list):
                _redact_content_blocks(result_content)

        elif block_type == "tool_use":
            # Redact values in tool_use input (but not keys)
            if "input" in block:
                block["input"] = _redact_json_values(block["input"])

        elif block_type == "thinking":
            # Redact the thinking prose but preserve `signature` byte-for-byte —
            # Anthropic validates the signature against the original thinking text,
            # BUT when replaying history the signature is bound to what was produced
            # upstream (post-de-redaction from us). We redact here because the model
            # originally saw placeholders; the signature travels unchanged.
            if "thinking" in block:
                block["thinking"] = redact(block.get("thinking", ""))

        elif block_type == "redacted_thinking":
            # Opaque encrypted payload from the API — never touch `data`.
            pass

    return blocks


def _de_redact_response(result: dict):
    """De-redact placeholders in response content."""
    for block in result.get("content", []):
        block_type = block.get("type", "")

        if block_type == "text":
            block["text"] = de_redact(block.get("text", ""))

        elif block_type == "tool_use":
            if "input" in block:
                block["input"] = _de_redact_json_values(block["input"])

        elif block_type == "thinking":
            # De-redact the thinking prose; leave `signature` untouched.
            if "thinking" in block:
                block["thinking"] = de_redact(block.get("thinking", ""))


def _de_redact_json_values(obj):
    """Recursively de-redact string values in a JSON-like structure."""
    if isinstance(obj, str):
        return de_redact(obj)
    elif isinstance(obj, dict):
        return {k: _de_redact_json_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_de_redact_json_values(item) for item in obj]
    return obj


def _extract_texts(payload: dict) -> list[str]:
    """Extract all text strings from messages for logging comparison."""
    texts = []
    for message in payload.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
    system = payload.get("system")
    if isinstance(system, str):
        texts.append(system)
    return texts


async def passthrough(path: str, request: Request):
    """Transparent pass-through for any Anthropic API endpoint we don't redact.

    Claude Code routes ALL traffic through us (ANTHROPIC_BASE_URL), so any
    unrecognised path (e.g. /v1/models, future telemetry endpoints) must be
    forwarded unmodified rather than 404'd.

    We intentionally do NOT redact bodies here — these endpoints don't carry
    user prose, and tampering risks breaking them. If a new endpoint does
    start carrying PII, give it an explicit handler above.
    """
    # /v1/messages/batches carries full message payloads with user prose —
    # the same PII surface as /v1/messages, but batched. Passing it through
    # the catch-all would silently leak unredacted content. Refuse loudly
    # until we grow an explicit redacting handler for the batch format.
    normalized = path.lstrip("/")
    if normalized == "v1/messages/batches" or normalized.startswith("v1/messages/batches/"):
        logger.error("Blocked /v1/messages/batches* — batch API not supported by redactor yet")
        return JSONResponse(
            {"error": {
                "type": "pii_proxy_error",
                "message": "Message Batches API is not yet supported by the PII redaction proxy. Requests to /v1/messages/batches are blocked to prevent unredacted PII leaks.",
            }},
            status_code=501,
        )

    # Named apart from the `forward_headers` helper on purpose: this is the
    # opposite policy. The explicit handlers send an allowlist; the catch-all
    # copies everything the client sent except hop-by-hop. That asymmetry is
    # the reason an unhandled path is the risky one.
    passthrough_headers = {}
    for key, value in request.headers.items():
        # Skip hop-by-hop and host headers; httpx rebuilds them.
        if key.lower() in ("host", "content-length", "connection", "transfer-encoding"):
            continue
        passthrough_headers[key] = value

    body = await request.body()

    try:
        response = await _client.request(
            request.method,
            f"/{path}",
            params=request.query_params,
            headers=passthrough_headers,
            content=body,
        )
    except httpx.HTTPError as e:
        logger.warning("Passthrough to /%s failed: %s", path, e)
        return JSONResponse({"error": str(e)}, status_code=502)

    # Strip hop-by-hop response headers before passing back.
    response_headers = {
        k: v for k, v in response.headers.items()
        if k.lower() not in ("content-length", "connection", "transfer-encoding", "content-encoding")
    }

    logger.info(
        "Passthrough: %s /%s -> %d (resp ct=%s ce=%s len=%d)",
        request.method,
        path,
        response.status_code,
        response.headers.get("content-type", "-"),
        response.headers.get("content-encoding", "-"),
        len(response.content),
    )
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=response_headers,
    )
