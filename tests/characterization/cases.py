"""The cases the golden fixtures are recorded from.

Each case names the app module it drives, the request the client makes, and the
reply the fake upstream gives. `harness.run_case` records what the proxy
forwarded and what it returned.

These pin behaviour as it *is*, not as it should be.
`anthropic/passthrough-forwards-unredacted` pins a known fail-open on purpose:
the catch-all forwards unhandled paths with the body untouched, so the secret
reaches the fake upstream in the clear and the fixture records it doing so.
That is the baseline, not a harness fault.

When it is fixed the fixture changes, and the diff is the point: a golden that
moves is a decision someone made, a golden that moves silently is a leak.

Anthropic, Grok and ChatGPT are covered here. The goldens were recorded from
a reference app tree given by `--app-path`, not from this package: a fixture
recorded from the refactored code proves only that it agrees with itself (Grok
from module `openai_main`, ChatGPT from module `chatgpt_main`). The ChatGPT
streaming case has no reference equivalent to record from, so
`chatgpt/codex-responses-streaming-split-placeholder` is new coverage for
logic that was reviewed but not characterized before. See
`tests/characterization/README.md` for the one gap this set does not close: no
live Codex CLI capture exists in this environment, so the exact
codex-turn-state/originator/chatgpt-account-id header names are taken from an
earlier recorded request rather than freshly verified —
low risk here specifically because ChatGPT forwards every header instead of
enumerating an allowlist (see `providers/chatgpt.py`), so an unverified header
name cannot cause one to be silently dropped, only the fixture to under-
represent what a real client sends.

The OpenAI (API-key) case has no fixtures yet: that provider is still out of
scope, deferred to whichever PR brings the account-plan/API-key split in.
"""
import json

from harness import PLACEHOLDER, SECRET

_ANTHROPIC_HEADERS = {
    "x-api-key": "sk-ant-test",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "prompt-caching-2024-07-31",
    "content-type": "application/json",
    "x-unrelated": "should-not-forward",
}

_OPENAI_HEADERS = {
    "authorization": "Bearer sk-test",
    "content-type": "application/json",
    "x-unrelated": "should-not-forward",
}

# What Grok Build actually sends, captured from the 1.0.13 binary: its own
# user-agent, `x-xai-token-auth`, and `x-grok-*` session identifiers. The
# provider forwards the first by name and the rest by prefix.
_GROK_HEADERS = {
    **_OPENAI_HEADERS,
    "user-agent": "grok-shell/1.0.13 (linux; x86_64)",
    "x-xai-token-auth": "xai-grok-cli",
    "x-grok-session-id": "abc-123",
}

# Taken from an earlier recorded request, not from a live capture — see the
# module docstring: no Codex CLI binary was available to re-verify.
# Unlike `_GROK_HEADERS`, `x-unrelated` here is NOT a "should not forward"
# marker: `chatgpt.py` forwards every header minus hop-by-hop, on purpose, so
# this one reaching upstream is the assertion, not a regression.
_CHATGPT_HEADERS = {
    "authorization": "Bearer sk-test",
    "content-type": "application/json",
    "x-unrelated": "should-not-forward",
}

CASES = [
    {
        "name": "anthropic/messages-non-streaming",
        "provider": "anthropic",
        "module": "main",
        "request": {
            "method": "POST",
            "path": "/v1/messages",
            "headers": _ANTHROPIC_HEADERS,
            "body": {
                "model": "claude-opus-4",
                "system": f"You deploy to {SECRET}.",
                "messages": [
                    {"role": "user", "content": f"is {SECRET} up?"},
                    {"role": "assistant", "content": [
                        {"type": "text", "text": f"checking {SECRET}"},
                        {"type": "tool_use", "id": "t1", "name": "ping",
                         "input": {"host": SECRET}},
                    ]},
                    {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "t1",
                         "content": f"pong from {SECRET}"},
                    ]},
                ],
            },
        },
        "upstream_response": {
            "id": "msg_1", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": f"{PLACEHOLDER} is up"}],
            "stop_reason": "end_turn",
        },
    },
    {
        "name": "anthropic/messages-system-as-blocks",
        "provider": "anthropic",
        "module": "main",
        "request": {
            "method": "POST",
            "path": "/v1/messages",
            "headers": _ANTHROPIC_HEADERS,
            "body": {
                "model": "claude-opus-4",
                "system": [{"type": "text", "text": f"host: {SECRET}",
                            "cache_control": {"type": "ephemeral"}}],
                "messages": [{"role": "user", "content": "hi"}],
            },
        },
        "upstream_response": {
            "id": "msg_2", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
        },
    },
    {
        # The placeholder is split across two text deltas and a tool_use input
        # arrives in three JSON fragments — the two things the carry buffer and
        # the tool-input buffer exist for.
        "name": "anthropic/messages-streaming-split-placeholder",
        "provider": "anthropic",
        "module": "main",
        "stream": True,
        "request": {
            "method": "POST",
            "path": "/v1/messages",
            "headers": _ANTHROPIC_HEADERS,
            "body": {"model": "claude-opus-4", "stream": True,
                     "messages": [{"role": "user", "content": f"where is {SECRET}"}]},
        },
        "upstream_response": [
            ("message_start", {"type": "message_start",
                               "message": {"id": "msg_3", "content": []}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta",
                                               "text": "host is <ZZTESTREF_"}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "1> ok"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("content_block_start", {"type": "content_block_start", "index": 1,
                                     "content_block": {"type": "tool_use", "id": "t9",
                                                       "name": "ping", "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                     "delta": {"type": "input_json_delta",
                                               "partial_json": '{"host":"<ZZT'}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                     "delta": {"type": "input_json_delta",
                                               "partial_json": 'ESTREF_1'}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                     "delta": {"type": "input_json_delta",
                                               "partial_json": '>"}'}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "tool_use"},
                               "usage": {"output_tokens": 7}}),
            ("message_stop", {"type": "message_stop"}),
        ],
    },
    {
        # The response is a bare token count, so nothing is de-redacted coming
        # back; the whole point is that the body forwarded is the redacted one,
        # because placeholders and raw strings tokenize differently.
        "name": "anthropic/count-tokens",
        "provider": "anthropic",
        "module": "main",
        "request": {
            "method": "POST",
            "path": "/v1/messages/count_tokens",
            "headers": _ANTHROPIC_HEADERS,
            "body": {"model": "claude-opus-4", "system": f"about {SECRET}",
                     "messages": [{"role": "user", "content": f"and {SECRET}"}]},
        },
        "upstream_response": {"input_tokens": 42},
    },
    {
        "name": "anthropic/batches-blocked",
        "provider": "anthropic",
        "module": "main",
        "request": {
            "method": "POST",
            "path": "/v1/messages/batches",
            "headers": _ANTHROPIC_HEADERS,
            "body": {"requests": [{"params": {"messages": [
                {"role": "user", "content": f"batch {SECRET}"}]}}]},
        },
        # Never reached: the recorded `upstream` block must stay empty.
        "upstream_response": {"should": "not be reached"},
    },
    {
        # FAIL-OPEN, pinned deliberately. The catch-all forwards any unhandled
        # path with its body untouched, so a secret in an unrecognised endpoint
        # reaches upstream raw. The fixture records the secret arriving at the
        # upstream — the leak, in evidence.
        "name": "anthropic/passthrough-forwards-unredacted",
        "provider": "anthropic",
        "module": "main",
        "request": {
            "method": "POST",
            "path": "/v1/some-future-endpoint",
            "headers": _ANTHROPIC_HEADERS,
            "body": {"note": f"secret is {SECRET}"},
        },
        "upstream_response": {"ok": True},
    },
    {
        # Every messages[] shape Grok Build emits: a string system prompt,
        # content-part text, an assistant tool_call whose arguments are a JSON
        # string, a tool result, and a tool schema whose description names the
        # host. The response carries the placeholder in prose and in arguments.
        "name": "grok/chat-completions-non-streaming",
        "provider": "grok",
        "module": "openai_main",
        "request": {
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": _GROK_HEADERS,
            "body": {
                "model": "grok-4",
                "messages": [
                    {"role": "system", "content": f"deploy to {SECRET}"},
                    {"role": "user", "content": [
                        {"type": "text", "text": f"check {SECRET}"}]},
                    {"role": "assistant", "tool_calls": [
                        {"id": "c0", "type": "function",
                         "function": {"name": "ping",
                                      "arguments": json.dumps({"h": SECRET})}}]},
                    {"role": "tool", "tool_call_id": "c0",
                     "content": f"pong from {SECRET}"},
                ],
                "tools": [{"type": "function", "function": {
                    "name": "ping",
                    "parameters": {"description": f"pings {SECRET}"}}}],
            },
        },
        "upstream_response": {
            "id": "chatcmpl_1", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant",
                "content": f"reached {PLACEHOLDER}",
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "ping",
                                             "arguments": json.dumps({"h": PLACEHOLDER})}}],
            }}],
        },
    },
    {
        # The placeholder is split across two content deltas and a tool call's
        # arguments arrive in three fragments. Chat Completions has no `.done`
        # event, so the fragments must be buffered and emitted once, on the
        # chunk carrying finish_reason; the usage-only trailer has no choices
        # and must survive.
        "name": "grok/chat-completions-streaming-split-placeholder",
        "provider": "grok",
        "module": "openai_main",
        "stream": True,
        "request": {
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": _GROK_HEADERS,
            "body": {"model": "grok-4", "stream": True,
                     "messages": [{"role": "user", "content": f"where is {SECRET}"}]},
        },
        "upstream_response": [
            {"choices": [{"index": 0, "delta": {"role": "assistant"},
                          "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "host is <ZZTESTREF_"},
                          "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "1> ok"},
                          "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "c1", "type": "function",
                 "function": {"name": "ping", "arguments": '{"h":"<ZZT'}}]},
                "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "ESTREF_1"}}]},
                "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '>"}'}}]},
                "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"total_tokens": 7}},
        ],
    },
    {
        # FAIL-CLOSED, the opposite of the Anthropic catch-all. A POST with no
        # redacting handler is answered 501 and the recorded `upstream` block
        # stays empty: the secret never left.
        "name": "grok/unhandled-post-blocked",
        "provider": "grok",
        "module": "openai_main",
        "request": {
            "method": "POST",
            "path": "/v1/embeddings",
            "headers": _GROK_HEADERS,
            "body": {"input": SECRET},
        },
        "upstream_response": {"should": "not be reached"},
    },
    {
        # Same rule, other verb. The reference tree blocks POST alone and
        # forwards a PUT body verbatim, so this golden could not be recorded
        # from it: it is hand-written to the 501 the proxy answers, and is the
        # third case the reference leg is meant to fail.
        "name": "grok/unhandled-put-blocked",
        "provider": "grok",
        "module": "openai_main",
        "request": {
            "method": "PUT",
            "path": "/v1/some-future-endpoint",
            "headers": _GROK_HEADERS,
            "body": {"note": f"secret is {SECRET}"},
        },
        "upstream_response": {"should": "not be reached"},
    },
    {
        # The one GET with an explicit handler. No body either direction, but
        # it sends the allowlist rather than everything the client had in
        # hand — the unrelated header must not arrive upstream.
        "name": "grok/models-get-allowlisted",
        "provider": "grok",
        "module": "openai_main",
        "request": {
            "method": "GET",
            "path": "/v1/models",
            "headers": _GROK_HEADERS,
        },
        "upstream_response": {"data": [{"id": "grok-4"}]},
    },
    {
        # An input_text part and a string `instructions` field, both carrying
        # the secret; the response echoes it back inside an output_text part.
        "name": "chatgpt/codex-responses",
        "provider": "chatgpt",
        "module": "chatgpt_main",
        "request": {
            "method": "POST",
            "path": "/backend-api/codex/responses",
            "headers": _CHATGPT_HEADERS,
            "body": {
                "model": "codex",
                "instructions": f"host {SECRET}",
                "input": [
                    {"role": "user", "content": [
                        {"type": "input_text", "text": f"ping {SECRET}"}]},
                ],
            },
        },
        "upstream_response": {
            "id": "resp_2",
            "output": [
                {"type": "message", "content": [
                    {"type": "output_text", "text": f"reached {PLACEHOLDER}"}]},
            ],
        },
    },
    {
        # New coverage: no streaming fixture existed for this provider.
        # Mirrors the Grok/Anthropic streaming cases' two hard parts —
        # a placeholder split across two output_text deltas, and a function
        # call's arguments arriving in three fragments — reshaped to the
        # Responses API's event-typed SSE (bare `data:` lines carrying `type`,
        # no `event:` header, same as the real API and unlike Anthropic).
        "name": "chatgpt/codex-responses-streaming-split-placeholder",
        "provider": "chatgpt",
        "module": "chatgpt_main",
        "stream": True,
        "request": {
            "method": "POST",
            "path": "/backend-api/codex/responses",
            "headers": _CHATGPT_HEADERS,
            "body": {"model": "codex", "stream": True,
                     "input": [{"role": "user", "content": [
                         {"type": "input_text", "text": f"where is {SECRET}"}]}]},
        },
        "upstream_response": [
            {"type": "response.created", "response": {"id": "resp_3"}},
            {"type": "response.output_text.delta", "item_id": "item_1",
             "content_index": 0, "delta": "host is <ZZTESTREF_"},
            {"type": "response.output_text.delta", "item_id": "item_1",
             "content_index": 0, "delta": "1> ok"},
            {"type": "response.output_text.done", "item_id": "item_1",
             "content_index": 0, "text": "host is <ZZTESTREF_1> ok"},
            {"type": "response.function_call_arguments.delta",
             "item_id": "fc_1", "delta": '{"host":"<ZZT'},
            {"type": "response.function_call_arguments.delta",
             "item_id": "fc_1", "delta": "ESTREF_1"},
            {"type": "response.function_call_arguments.delta",
             "item_id": "fc_1", "delta": '>"}'},
            {"type": "response.function_call_arguments.done",
             "item_id": "fc_1", "arguments": '{"host":"<ZZTESTREF_1>"}'},
            {"type": "response.completed", "response": {"id": "resp_3", "output": []}},
        ],
    },
    {
        # FAIL-CLOSED, same rule and rationale as Grok's pair: a POST with no
        # redacting handler is refused rather than forwarded. This is also
        # where chatgpt's catch-all diverges from the reference tree — see
        # `providers/chatgpt.py`'s module docstring. The reference blocks POST
        # only under `/backend-api/codex/*` and forwards this path's body
        # verbatim; this golden pins the tightened rule instead.
        "name": "chatgpt/unhandled-post-blocked",
        "provider": "chatgpt",
        "module": "chatgpt_main",
        "request": {
            "method": "POST",
            "path": "/backend-api/something-else",
            "headers": _CHATGPT_HEADERS,
            "body": {"note": f"secret is {SECRET}"},
        },
        "upstream_response": {"should": "not be reached"},
    },
    {
        # Same rule, other verb — the reference tree has no rule for PUT at
        # all, so like `grok/unhandled-put-blocked` this is hand-written to
        # the 501 the proxy answers, not recorded.
        "name": "chatgpt/unhandled-put-blocked",
        "provider": "chatgpt",
        "module": "chatgpt_main",
        "request": {
            "method": "PUT",
            "path": "/backend-api/some-future-endpoint",
            "headers": _CHATGPT_HEADERS,
            "body": {"note": f"secret is {SECRET}"},
        },
        "upstream_response": {"should": "not be reached"},
    },
    {
        # The one case unchanged from the fail-open catch-all: GET has
        # no PII-bearing body either direction, so it still passes through —
        # and still with every header, not an allowlist (contrast
        # `grok/models-get-allowlisted`, which is why `x-unrelated` reaches
        # upstream here but not there).
        "name": "chatgpt/plugins-get-forwarded",
        "provider": "chatgpt",
        "module": "chatgpt_main",
        "request": {
            "method": "GET",
            "path": "/backend-api/plugins/featured",
            "headers": _CHATGPT_HEADERS,
        },
        "upstream_response": {"plugins": []},
    },
]


# ---------------------------------------------------------------- ops routes
# The four routes served by the shared core. For Anthropic these pin that the
# routes answer exactly as the reference app does. For Grok and ChatGPT some of
# them are *meant* to differ from the reference module, and were edited by hand
# because the shared core answers differently; the fixtures README names them,
# with the PUT case above.
_OPS_ROUTES = [
    ("health", "GET", "/health"),
    ("version", "GET", "/version"),
    ("mappings", "GET", "/mappings"),
    ("mappings-clear", "POST", "/mappings/clear"),
]

for _provider, _module in (("anthropic", "main"), ("grok", "openai_main"),
                           ("chatgpt", "chatgpt_main")):
    for _label, _method, _path in _OPS_ROUTES:
        CASES.append({
            "name": f"{_provider}/ops-{_label}",
            "provider": _provider,
            "module": _module,
            "request": {"method": _method, "path": _path},
            # Ops routes are served locally and never reach upstream; the
            # recorded `upstream` block staying empty is part of the assertion.
            "upstream_response": {"should": "not be reached"},
        })


def by_provider(provider):
    return [c for c in CASES if provider in (None, c["provider"])]
