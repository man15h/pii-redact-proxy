# Characterization fixtures

Golden recordings of how the proxy behaves on the wire, taken from a
reference app tree **before** a refactor. Every later change has to reproduce
them.

## Why record before refactoring

The proxy carries PII correctness. A fixture recorded from refactored code
proves only that the refactor agrees with itself; a fixture recorded from the
code in production is the only thing that can say the refactor changed
nothing. So the recorder points at whichever app tree you give it, not
necessarily this repo — hence `--app-path`.

## What a fixture holds

A redacting proxy has two observable surfaces, and a fixture captures both:

| Key | Meaning |
|-----|---------|
| `request` | what the client sent the proxy |
| `upstream` | what the proxy forwarded — proves the secret was replaced going out |
| `response` | what the proxy returned — proves the placeholder was restored coming back |

Volatile response headers (`date`, `server`, `content-length`,
`transfer-encoding`) are dropped so goldens don't churn.

## Running it

Needs only fastapi + httpx. No Presidio, no spaCy — `redactor` and `session`
are stubbed with a one-line substitution, so what these fixtures characterize
is the *apps*: message traversal, the header allowlist, the catch-all, SSE
carry and tool-call buffering. The recognizers are a separate concern with
their own coverage.

The recorder drives both tree shapes from one fixture set: a flat layout keeps
the app modules side by side in a single directory, this repo puts them in a
package so they can share a core. `load_app` detects which it is given and
registers the `redactor` and `session` stubs under both module paths, so a
golden never has to be re-recorded just because a file moved.

### What these cannot see

Stubbing a module means its own import lines never execute. A flat
`from recognizers import ...` in `core/redactor.py` — correct in a directory of
sibling modules, unresolvable inside a package — passes every gate here,
because the file the fixtures load in its place has no such line. The
container would be the first thing to actually run it, and it would exit 1.

`tests/import_check.py` covers that: presidio is stubbed and nothing else, so
every module in the package is imported for real. The Dockerfile repeats the
check with the real dependencies, so an image that cannot start cannot be
pushed. Both run before these fixtures do — a byte-identical replay proves
nothing about code that will not import.

```sh
python3 -m venv /tmp/piichar && /tmp/piichar/bin/pip install fastapi httpx

# record (only when the baseline itself moves)
/tmp/piichar/bin/python characterize.py record \
    --app-path /path/to/app-tree

# verify any tree against the goldens
/tmp/piichar/bin/python characterize.py verify --app-path /path/to/some/app
/tmp/piichar/bin/python characterize.py verify --app-path ... --provider openai
```

`verify` exits non-zero on any difference and prints them one per line, keyed
by JSON path.

## The sentinel

The secret is `zz-test-host.invalid` and its placeholder is `<ZZTESTREF_1>`,
deliberately not a realistic-looking placeholder. Agents editing these files
run *behind* a live pii-proxy, so a literal matching a real mapping gets
de-redacted on the way in and re-redacted on the way out, and the fixture stops
saying what it appears to say. Keep both sentinels obviously synthetic.

## The one fixture that records a leak

`anthropic/passthrough-forwards-unredacted` records the secret arriving at the
upstream in the clear. That is the baseline, not a harness fault: the catch-all
forwards unhandled paths with the body untouched, by design. It is the "before"
for the work that closes it, and when that lands the fixture changes — which is
the point. A golden that moves is a decision someone made; a golden that moves
silently is a leak.

## The goldens that do not match the reference tree

`anthropic/ops-version` is edited by hand rather than recorded, and this tree
is *meant* to differ from the reference on it. `/version` has a second field,
`build`, naming the image that is answering:

```
.response.body.build: added ('unknown')
```

That is the whole expected difference. Verifying the reference tree therefore
reports `10 cases, 1 differing` and that one line — any other case, or any
other path inside this one, is a regression.

It is edited rather than re-recorded on purpose. Re-recording it from this
repo would make it a fixture recorded from the refactored code, which proves
only that the refactor agrees with itself, and the rule against that is the
reason these files exist. Editing it says the honest thing instead: this value
is intended, a person chose it, and the reference tree does not produce it.

Why the field exists: the version constant only moves when behaviour does, so
two builds report the same string and the payload cannot say which code is
serving — and "which code is answering on 8082" is the question a rollback
decision turns on.

The Grok set has two more, for the same reason. `grok/ops-version` carries the
same `build` edit. `grok/ops-mappings` is the leak being closed: the reference
module answers `/mappings` with the raw placeholder-to-original map, every
secret the proxy has accumulated, to any LAN caller; the core answers with
placeholders and a four-character preview. The golden holds the core's shape,
edited by hand, so a provider that brought its own leaky route would fail
here rather than ship it. `grok/unhandled-put-blocked` is the third: the
reference refuses POST alone and forwards a PUT body verbatim, so the golden
could not be recorded from it and is written by hand to the 501 the proxy
answers.
Verifying the Grok reference tree therefore reports exactly those three cases
differing — the two ops cases only inside `.response.body`, and the PUT case
recording the secret arriving upstream.

The ChatGPT set has the same three, plus one more of its own.
`chatgpt/ops-version` and `chatgpt/ops-mappings` carry the same two edits as
Grok's, for the same reasons — `version` is unchanged (`2026-05-06-chatgpt.1`,
the string the reference module reports) but `build` is new, and
`/mappings` moves from the raw leak to the core's hardened shape.
`chatgpt/unhandled-put-blocked` is the same PUT gap as Grok's.
`chatgpt/unhandled-post-blocked` is the one case that is *also* a POST but is
still hand-written rather than recorded: the reference module does refuse
some POSTs, but only under `/backend-api/codex/*` — a POST anywhere else
forwards its body verbatim. This package tightens that to match Grok's rule —
refuse every unhandled non-GET, not just POST under one prefix — so this
golden could not be recorded from the reference either.
`chatgpt/codex-responses`, `chatgpt/codex-responses-streaming-split-placeholder`
and `chatgpt/plugins-get-forwarded` are the three genuinely behavior-preserved
cases: recorded from the reference tree (module `chatgpt_main`, plus its
`openai_main` and `openai_streaming` siblings the module imports from) and
replayed unchanged through this package. The streaming case had no existing
fixture, so it is new coverage recorded fresh against the reference tree;
recording it needs the two sibling modules present alongside
`chatgpt_main.py`, same as running the app for real would.

One gap this set does not close: the exact codex-specific header names a live
Codex CLI sends (`originator`, `chatgpt-account-id`, `x-codex-turn-state`, and
others named in `providers/chatgpt.py`'s module docstring) were not
re-verified against the real binary — no Codex CLI was available. The header
set the fixtures use is taken from an earlier recorded request
(`authorization`, `content-type`, plus a marker header) rather than freshly
captured. This matters less
here than it would for an allowlist-based provider: `chatgpt.py` forwards every
header minus hop-by-hop instead of enumerating one (see the module docstring),
so an un-captured header name cannot be silently dropped — only the fixtures
under-represent what a real client actually sends. Whoever next has access to
the Codex CLI binary should run a capture and diff the real header set against
what's recorded here; if it drifts, re-record and say so in this file the way
the entries above do.

Verifying the ChatGPT reference tree (`--app-path` pointed at a flat tree
with `chatgpt_main.py`, `openai_main.py` and `openai_streaming.py` all
present) therefore reports exactly two cases differing — the two ops cases,
inside `.response.body` only — plus the two hand-written cases
(`unhandled-post-blocked`, `unhandled-put-blocked`) recording the secret
arriving upstream, the same shape as Grok's reference-tree diff.

## Scope

Anthropic, Grok and ChatGPT. The Grok goldens were recorded from a reviewed
reference tree (module `openai_main`) and this package is verified against
them. The ChatGPT goldens were recorded from a reference tree with
`chatgpt_main.py`, `openai_main.py` and `openai_streaming.py`, and this
package is verified against them the same way. The OpenAI (API-key) case has
no fixtures yet; it arrives with that half of the split. The recorder still
drives the flat layout, so nothing has to be reconstructed from memory.

```sh
# the Grok reference leg, by hand at review: expect the three hand-edited
# cases differing and nothing else
/tmp/piichar/bin/python characterize.py verify --provider grok \
    --app-path /path/to/grok-reference-tree

# the ChatGPT reference leg: expect the four hand-edited/tightened cases
# differing and nothing else (tree must include chatgpt_main.py,
# openai_main.py and openai_streaming.py — chatgpt_main imports the other two)
/tmp/piichar/bin/python characterize.py verify --provider chatgpt \
    --app-path /path/to/chatgpt-reference-tree
```

## Adding a case

Append to `cases.py`: the flat-layout app module to record from (`module` —
for Grok that is `openai_main`), the
client request, and the reply the fake upstream should give (a dict for JSON,
a list for SSE; list items may be `(event_name, payload)` pairs where the
stream carries `event:` headers, as Anthropic's does). Then
`record --case <name>`.
