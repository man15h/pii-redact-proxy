"""OpenAI Codex CLI in ChatGPT-OAuth mode — the wire format Codex speaks on the account plan.

Same Responses API wire format the deferred `openai` (API-key) provider will
use, but:
  - Upstream is `chatgpt.com` (the ChatGPT backend) instead of `api.openai.com`.
  - The codex endpoint lives at `/backend-api/codex/responses`, not `/v1/responses`.
  - Auth is OAuth `Authorization: Bearer <JWT>` plus codex-specific metadata
    headers (`originator`, `chatgpt-account-id`, `x-codex-turn-state`,
    `x-client-request-id`, `x-openai-internal-codex-residency`, ...). The
    chatgpt backend is opinionated about these and they cannot be enumerated
    the way xAI's `x-grok-*`/`x-xai-*` families can, so the explicit handler
    forwards every header minus hop-by-hop instead of an allowlist — the one
    place this provider departs from `forward_headers`'s allowlist contract.
    That is a wire-format necessity, not new scope.

The redaction and de-redaction logic below runs on this repo's core
(`redact`/`de_redact`/`mapper`), and `/mappings` is the hardened core
implementation — see `core/ops.py` for why no provider brings its own.

The catch-all here refuses every non-GET path without an explicit handler,
following the Grok precedent (`grok.py`'s passthrough docstring) rather than
the narrower rule of blocking POST only under `/backend-api/codex/*`. That
narrower rule would leave PUT/PATCH under any other path forwarding a body
verbatim. A body is the direction PII travels; an unrecognised path
is a reason not to forward one, not a reason to allow every verb but one.
GET is still passed through unmodified (Codex validates auth and fetches
plugin lists on a few GET-only paths that carry no PII either direction).
"""

import json
import logging

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..core.redactor import redact, de_redact
from ..core.session import mapper
from ..core.jsonwalk import redact_json_values
from .responses_streaming import proxy_responses_stream

NAME = "chatgpt"
# Bump on every behaviour-affecting deploy so /version proves what's running.
VERSION = "2026-05-06-chatgpt.1"
UPSTREAM_BASE_URL = "https://chatgpt.com"

# Unused by this provider's explicit handler — see the module docstring for
# why chatgpt forwards every header instead of an allowlist. Declared empty
# rather than omitted so this module still satisfies the Provider protocol.
FORWARD_HEADERS = ()
FORWARD_HEADER_PREFIXES = ()

# Hop-by-hop headers we MUST drop before forwarding. httpx rebuilds these.
_HOP_BY_HOP = {"host", "content-length", "connection", "transfer-encoding"}

logger = logging.getLogger("pii-proxy")

# Set by register(). Module-level rather than threaded through every helper:
# one process serves exactly one provider, which is what PROVIDER decides.
_client: httpx.AsyncClient = None


def register(app: FastAPI, http_client: httpx.AsyncClient) -> None:
    """Attach the chatgpt routes. Order matters — see passthrough."""
    global _client
    _client = http_client
    app.post("/backend-api/codex/responses")(proxy_codex_responses)
    # LAST: a catch-all shadows anything declared after it.
    app.api_route("/{path:path}",
                  methods=["GET", "POST", "PUT", "DELETE", "PATCH"])(passthrough)


async def proxy_codex_responses(request: Request):
    """Codex CLI ChatGPT-OAuth path. Same body shape as the Responses API."""
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

    forward = _build_forward_headers(request)

    logger.info(
        "Forwarding chatgpt-codex request: model=%s stream=%s input=%s new_redactions=%d total_mappings=%d",
        payload.get("model", "unknown"),
        is_streaming,
        _input_summary(payload.get("input")),
        new_redactions,
        mappings_after,
    )

    if is_streaming:
        return await _handle_streaming(payload, forward)
    return await _handle_non_streaming(payload, forward)


async def _handle_non_streaming(payload: dict, headers: dict) -> JSONResponse:
    response = await _client.post(
        "/backend-api/codex/responses",
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
            "/backend-api/codex/responses",
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
        proxy_responses_stream(response),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "connection": "keep-alive",
        },
    )


def _build_forward_headers(request: Request) -> dict:
    """Forward all incoming headers except hop-by-hop.

    See the module docstring: the chatgpt backend uses several codex-specific
    headers that can't be dropped just because we haven't enumerated them, so
    this handler can't use `providers.base.forward_headers`'s allowlist.
    """
    forward = {}
    for key, value in request.headers.items():
        if key.lower() in _HOP_BY_HOP:
            continue
        forward[key] = value
    return forward


def _redact_request(payload: dict) -> None:
    """Redact PII in `input`, `instructions`, and tool parameter schemas in-place."""
    if "input" in payload:
        payload["input"] = _redact_input(payload["input"])

    if "instructions" in payload:
        instructions = payload["instructions"]
        if isinstance(instructions, str):
            payload["instructions"] = redact(instructions)
        elif isinstance(instructions, list):
            # Some clients send an array of instruction objects.
            payload["instructions"] = redact_json_values(instructions)

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


def _redact_input(value):
    """Redact the Responses API `input` field (string OR array of items)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_input_item(item) for item in value]
    return value


def _redact_input_item(item):
    """Redact a single item from an `input` array.

    Item shapes we care about:
      - {"type": "message", "content": str}
      - {"type": "message", "content": [{"type": "input_text", "text": "..."}, ...]}
      - {"type": "function_call", "arguments": "<json string>", ...}
      - {"type": "function_call_output", "output": "..."}
      - {"type": "input_image"|"input_file", ...}  (URLs/base64 — recurse defensively)
      - anything else — recurse defensively to catch strings nested anywhere
    """
    if not isinstance(item, dict):
        return redact_json_values(item)

    item_type = item.get("type", "")

    if item_type == "message":
        content = item.get("content")
        if isinstance(content, str):
            item["content"] = redact(content)
        elif isinstance(content, list):
            item["content"] = [_redact_content_part(part) for part in content]
        return item

    if item_type == "function_call":
        # `arguments` is a JSON string; redact it as a string (placeholders are
        # opaque tokens and survive JSON-string round-trips because the
        # placeholder format `<NAME_N>` doesn't contain JSON metacharacters).
        if isinstance(item.get("arguments"), str):
            item["arguments"] = redact(item["arguments"])
        return item

    if item_type == "function_call_output":
        if isinstance(item.get("output"), str):
            item["output"] = redact(item["output"])
        return item

    # Unknown item type — recurse so any embedded string PII still gets caught.
    return redact_json_values(item)


def _redact_content_part(part):
    """Redact a single content part within a message item."""
    if not isinstance(part, dict):
        return redact_json_values(part)
    part_type = part.get("type", "")
    if part_type in ("input_text", "output_text", "summary_text", "refusal"):
        # Field name varies: input_text/output_text/summary_text use `text`,
        # refusal uses `refusal`. Redact whichever is present.
        if isinstance(part.get("text"), str):
            part["text"] = redact(part["text"])
        if isinstance(part.get("refusal"), str):
            part["refusal"] = redact(part["refusal"])
        return part
    # Any other content part type — recurse defensively.
    return redact_json_values(part)


def _de_redact_response(result: dict) -> None:
    """De-redact placeholders in a non-streaming Responses API result."""
    output = result.get("output")
    if isinstance(output, list):
        for item in output:
            _de_redact_output_item(item)


def _de_redact_output_item(item) -> None:
    if not isinstance(item, dict):
        return
    item_type = item.get("type", "")

    if item_type == "message":
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                _de_redact_content_part(part)
        elif isinstance(content, str):
            item["content"] = de_redact(content)

    elif item_type == "function_call":
        if isinstance(item.get("arguments"), str):
            item["arguments"] = de_redact(item["arguments"])

    elif item_type == "reasoning":
        # `summary` is a list of summary_text parts.
        summary = item.get("summary")
        if isinstance(summary, list):
            for part in summary:
                _de_redact_content_part(part)

    elif item_type == "refusal":
        if isinstance(item.get("refusal"), str):
            item["refusal"] = de_redact(item["refusal"])


def _de_redact_content_part(part) -> None:
    if not isinstance(part, dict):
        return
    if isinstance(part.get("text"), str):
        part["text"] = de_redact(part["text"])
    if isinstance(part.get("refusal"), str):
        part["refusal"] = de_redact(part["refusal"])


def _extract_texts(payload: dict) -> list[str]:
    """Pull text strings out of an outgoing payload for DEBUG-level diff logging."""
    texts = []
    inp = payload.get("input")
    if isinstance(inp, str):
        texts.append(inp)
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str):
                texts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        texts.append(part["text"])
            if isinstance(item.get("output"), str):
                texts.append(item["output"])
            if isinstance(item.get("arguments"), str):
                texts.append(item["arguments"])
    instructions = payload.get("instructions")
    if isinstance(instructions, str):
        texts.append(instructions)
    return texts


def _input_summary(inp) -> str:
    if isinstance(inp, str):
        return f"str(len={len(inp)})"
    if isinstance(inp, list):
        return f"items={len(inp)}"
    return "none"


async def passthrough(path: str, request: Request):
    """Transparent passthrough for GET-shaped endpoints we don't redact.

    Everything except GET that reaches here is BLOCKED — see the module
    docstring for why.

    Codex pulls plugins from `/backend-api/plugins/featured`, validates auth
    via `/backend-api/codex/agent-identity`, etc. Those GETs carry no PII
    either direction, so they pass through with every header forwarded (same
    policy as the explicit handler — see the module docstring on why this
    provider doesn't use an allowlist).
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

    forward = _build_forward_headers(request)
    body = await request.body()

    try:
        response = await _client.request(
            request.method,
            f"/{path}",
            params=request.query_params,
            headers=forward,
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
