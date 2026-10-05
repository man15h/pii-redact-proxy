"""xAI Chat Completions — the wire format Grok Build speaks.

Grok Build posts to `/v1/chat/completions`. That was verified against the
1.0.13 binary with `base_url` pointed at a capture listener, not read from the
docs: it sends its own user-agent, `x-xai-token-auth`, a family of `x-grok-*`
session headers, and a body of system prompt plus session context.

The format is OpenAI's, the upstream is api.x.ai. This is the Chat
Completions surface only; the Responses API belongs to the `openai` provider
and is not here.

The catch-all refuses everything but GET on paths it has no handler for. A
body is the direction PII travels, so an unrecognised path is a reason not to
forward one. Anthropic's provider forwards unhandled paths and blocks one
prefix; the two disagree on purpose, and `providers/base.py` says why that
stays per-provider.
"""

import json
import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..core.redactor import redact, de_redact
from ..core.session import mapper
from ..core.jsonwalk import redact_json_values
from .base import forward_headers
from .chat_completions_streaming import proxy_chat_stream

NAME = "grok"
# Bump on every behaviour-affecting deploy so /version proves what's running.
VERSION = "2026-08-31.1"
UPSTREAM_BASE_URL = "https://api.x.ai"

# `user-agent` is in here deliberately: providers gate coding-agent endpoints
# on it, and dropping it would leave upstream seeing httpx rather than the
# client. The catch-all already forwards everything on GET, so an explicit
# handler that stripped it would be the *less* faithful path.
FORWARD_HEADERS = (
    "authorization",
    "content-type",
    "user-agent",
)
# Grok Build sends `x-xai-token-auth` and a family of `x-grok-*` session
# identifiers that cannot be enumerated; they are part of how it identifies
# itself upstream, so they go by prefix.
FORWARD_HEADER_PREFIXES = ("x-xai-", "x-grok-")

logger = logging.getLogger("pii-proxy")

# Set by register(). Module-level rather than threaded through every helper:
# one process serves exactly one provider, which is what PROVIDER decides.
_client: httpx.AsyncClient = None


def register(app: FastAPI, http_client: httpx.AsyncClient) -> None:
    """Attach the Grok routes. Order matters — see passthrough."""
    global _client
    _client = http_client
    app.post("/v1/chat/completions")(proxy_chat_completions)
    app.get("/v1/models")(proxy_models)
    # LAST: a catch-all shadows anything declared after it.
    app.api_route("/{path:path}",
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH"])(passthrough)


async def proxy_chat_completions(request: Request):
    """Proxy Chat Completions: redact request, forward, de-redact response."""
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    is_streaming = payload.get("stream", False)
    mappings_before = len(mapper.get_mappings_snapshot())

    # Fail CLOSED on any redaction error — forwarding unredacted defeats the
    # whole point of this proxy, so a 500 to the client is the correct mode.
    try:
        if logger.isEnabledFor(logging.DEBUG):
            original_texts = _extract_texts(payload)

        _redact_request(payload)

        mappings_after = len(mapper.get_mappings_snapshot())
        new_redactions = mappings_after - mappings_before

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

    forward = forward_headers(request, FORWARD_HEADERS, FORWARD_HEADER_PREFIXES)

    messages = payload.get("messages")
    logger.info(
        "Forwarding chat request: model=%s stream=%s messages=%d new_redactions=%d total_mappings=%d",
        payload.get("model", "unknown"),
        is_streaming,
        len(messages) if isinstance(messages, list) else 0,
        new_redactions,
        mappings_after,
    )

    if is_streaming:
        return await _handle_streaming(payload, forward)
    return await _handle_non_streaming(payload, forward)


async def _handle_non_streaming(payload: dict, headers: dict) -> JSONResponse:
    response = await _client.post(
        "/v1/chat/completions",
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
    _de_redact_response(result)
    return JSONResponse(result, status_code=200)


async def _handle_streaming(payload: dict, headers: dict) -> StreamingResponse:
    response = await _client.send(
        _client.build_request(
            "POST",
            "/v1/chat/completions",
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
        proxy_chat_stream(response),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "connection": "keep-alive",
        },
    )


async def proxy_models(request: Request):
    """List models — no PII either direction.

    Explicit rather than left to the catch-all so it sends the allowlist, not
    everything the client had in hand.
    """
    forward = forward_headers(request, FORWARD_HEADERS, FORWARD_HEADER_PREFIXES)
    response = await _client.get("/v1/models", headers=forward)
    try:
        return JSONResponse(response.json(), status_code=response.status_code)
    except (json.JSONDecodeError, ValueError):
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers={"content-type": response.headers.get("content-type", "application/json")},
        )


def _redact_request(payload: dict) -> None:
    """Redact PII in `messages` and tool parameter schemas in-place."""
    messages = payload.get("messages")
    if isinstance(messages, list):
        payload["messages"] = [_redact_message(m) for m in messages]
    _redact_tool_schemas(payload)


def _redact_message(message):
    """Redact one Chat Completions message.

    Shapes: {"role": ..., "content": str | [{"type": "text", "text": ...}, ...]},
    assistant messages with `tool_calls[].function.arguments` (a JSON string),
    the legacy single `function_call`, and `reasoning_content` echoed back into
    a follow-up turn by reasoning models.
    """
    if not isinstance(message, dict):
        return redact_json_values(message)

    content = message.get("content")
    if isinstance(content, str):
        message["content"] = redact(content)
    elif isinstance(content, list):
        message["content"] = [_redact_content_part(part) for part in content]

    if isinstance(message.get("reasoning_content"), str):
        message["reasoning_content"] = redact(message["reasoning_content"])

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function")
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                fn["arguments"] = redact(fn["arguments"])

    function_call = message.get("function_call")
    if isinstance(function_call, dict) and isinstance(function_call.get("arguments"), str):
        function_call["arguments"] = redact(function_call["arguments"])

    return message


def _redact_content_part(part):
    if not isinstance(part, dict):
        return redact_json_values(part)
    if part.get("type") == "text" and isinstance(part.get("text"), str):
        part["text"] = redact(part["text"])
        return part
    # image_url / input_audio / anything newer — recurse so an embedded path or
    # internal URL still gets caught.
    return redact_json_values(part)


def _redact_tool_schemas(payload: dict) -> None:
    """Redact tool parameter schemas — a description can name a host."""
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            # Function-style tools: {"type": "function", "function": {"parameters": {...}}}
            fn = tool.get("function")
            if isinstance(fn, dict) and "parameters" in fn:
                fn["parameters"] = redact_json_values(fn["parameters"])
            # Newer flat shape: {"type": "function", "name": "...", "parameters": {...}}
            if "parameters" in tool and isinstance(tool["parameters"], (dict, list)):
                tool["parameters"] = redact_json_values(tool["parameters"])


def _de_redact_response(result: dict) -> None:
    """De-redact placeholders in a non-streaming Chat Completions result."""
    choices = result.get("choices")
    if not isinstance(choices, list):
        return
    for choice in choices:
        if isinstance(choice, dict):
            _de_redact_message(choice.get("message"))


def _de_redact_message(message) -> None:
    if not isinstance(message, dict):
        return
    for field in ("content", "reasoning_content", "refusal"):
        if isinstance(message.get(field), str):
            message[field] = de_redact(message[field])

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function")
            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                fn["arguments"] = de_redact(fn["arguments"])

    function_call = message.get("function_call")
    if isinstance(function_call, dict) and isinstance(function_call.get("arguments"), str):
        function_call["arguments"] = de_redact(function_call["arguments"])


def _extract_texts(payload: dict) -> list[str]:
    """Pull text strings out of an outgoing chat payload for DEBUG diff logging."""
    texts = []
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return texts
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        if isinstance(message.get("reasoning_content"), str):
            texts.append(message["reasoning_content"])
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                    texts.append(fn["arguments"])
    return texts


async def passthrough(path: str, request: Request):
    """Transparent passthrough for GET-shaped endpoints we don't redact.

    Everything except GET that reaches here is BLOCKED. A request body is the
    direction PII travels, so "we didn't recognise the path" is not a reason to
    forward one — it is the reason not to. Failing closed only under
    `/v1/responses/*` would let a client on a different surface (Grok Build
    posts to `/v1/chat/completions`) have its whole context forwarded verbatim
    while the log line read like an ordinary passthrough; closing it for POST
    alone would leave PUT and PATCH carrying a body through. The rule is about
    the body, not the verb.

    Adding a surface therefore means adding an explicit redacting handler
    above, not widening this list.
    """
    if request.method != "GET":
        logger.error("Blocked %s /%s — no redacting handler for that path", request.method, path)
        return JSONResponse(
            {"error": {
                "type": "pii_proxy_error",
                "message": (
                    f"{request.method} /{path} is not handled by the PII redaction proxy. "
                    f"Unrecognised {request.method} paths are blocked rather than forwarded unredacted."
                ),
            }},
            status_code=501,
        )

    # Named apart from the `forward_headers` helper on purpose: this is the
    # opposite policy. The explicit handler sends an allowlist; the catch-all
    # copies everything the client sent except hop-by-hop.
    passthrough_headers = {}
    for key, value in request.headers.items():
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
