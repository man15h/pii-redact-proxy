"""What a provider is, and what the core needs from one.

A provider answers exactly one question: what does this vendor's wire format
look like? Everything else — recognizers, the placeholder mapper, the ops
routes, the request log — belongs to core and is identical whoever we are
talking to.

The shape below is deliberately drawn from all three wire formats, not just
one of them, because an abstraction designed against a single case comes out
shaped like that case. Concretely, that is why:

* `FORWARD_HEADER_PREFIXES` exists at all. Anthropic needs only the exact list.
  xAI's client sends a family of `x-xai-*` and `x-grok-*` headers that cannot
  be enumerated, so prefix matching has to be in the contract from the start
  rather than bolted on when that provider lands.
* `register` takes the client rather than reading a module global. A
  hardcoded upstream makes an app untestable without monkeypatching an
  import.
* nothing here says how the catch-all should behave. Anthropic forwards
  unhandled paths and blocks one prefix; the OpenAI work refuses unhandled
  POSTs outright. That is a real disagreement about policy, so it stays inside
  each provider's `register` instead of being averaged into a flag neither
  wants.
"""
from importlib import import_module
from typing import Protocol, runtime_checkable

import httpx
from fastapi import FastAPI, Request


@runtime_checkable
class Provider(Protocol):
    NAME: str
    #: Bumped on every behaviour-affecting deploy: what this proxy does. Which
    #: image is doing it is the `build` field beside it, stamped by CI.
    VERSION: str
    UPSTREAM_BASE_URL: str
    #: Exact header names forwarded upstream. Everything else is dropped.
    FORWARD_HEADERS: tuple[str, ...]
    #: Header name prefixes forwarded upstream, for vendor families.
    FORWARD_HEADER_PREFIXES: tuple[str, ...]

    def register(self, app: FastAPI, http_client: httpx.AsyncClient) -> None:
        """Attach this provider's routes to the app."""


def forward_headers(request: Request, names: tuple[str, ...],
                    prefixes: tuple[str, ...] = ()) -> dict:
    """Build the upstream header set: an allowlist, never a copy of the client's.

    Explicit handlers send only what a provider names. That is the opposite of
    the catch-all, which forwards everything it did not strip — the difference
    is deliberate and is why redacting handlers are the safe path.
    """
    out = {}
    for name in names:
        value = request.headers.get(name)
        if value:
            out[name] = value
    if prefixes:
        for key, value in request.headers.items():
            if key.lower().startswith(prefixes) and value:
                out[key] = value
    return out


def load(name: str) -> Provider:
    """Import the provider module `PROVIDER` selected.

    Raises with the available names rather than a bare ImportError, because the
    likeliest cause is a typo in an environment variable on a host somebody is
    already debugging.
    """
    try:
        return import_module(f"{__package__}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name and not exc.name.startswith(f"{__package__}."):
            raise
        raise ValueError(
            f"unknown PROVIDER {name!r}; available: {', '.join(available())}"
        ) from exc


def available() -> list[str]:
    """Provider names, discovered by what a module declares rather than by
    what it is called.

    Filename convention would have been shorter and wrong: `anthropic_streaming`
    sits in this package and is a codec, not a provider. A module is a provider
    when it declares NAME, which is the same thing the Protocol above asks for.
    """
    import pkgutil

    pkg = import_module(__package__)
    names = []
    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name.startswith("_") or mod.name == "base":
            continue
        try:
            candidate = import_module(f"{__package__}.{mod.name}")
        except Exception:
            # Listing available providers must not fail because one of them
            # cannot import; that error belongs to whoever selects it.
            continue
        if getattr(candidate, "NAME", None):
            names.append(candidate.NAME)
    return sorted(names)
