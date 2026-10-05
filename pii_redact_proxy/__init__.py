"""A redacting reverse proxy, one core and one provider per wire format."""
import logging
import os

import httpx
from fastapi import FastAPI

from .core.ops import register_ops
from .providers import base

logger = logging.getLogger("pii-proxy")


def build_app(provider_name: str) -> FastAPI:
    """Assemble the app for one provider.

    Separate from `__main__` on purpose: this returns a FastAPI instance
    without binding a port, which is what lets the characterization fixtures
    drive the real assembly rather than a test-only imitation of it.

    The upstream base URL comes from the provider, but `UPSTREAM_BASE_URL` in
    the environment overrides it. That override is why this is testable at all
    — with a hardcoded upstream, a test could only reach the app by
    monkeypatching an import.
    """
    provider = base.load(provider_name)
    upstream = os.getenv("UPSTREAM_BASE_URL") or provider.UPSTREAM_BASE_URL
    # Stamped into the image by CI from the commit it built. Absent means a
    # local run or an image built before the stamp existed — both answer
    # `unknown` rather than failing, because build identity is diagnostics.
    build = os.getenv("BUILD_SHA") or "unknown"

    app = FastAPI(title=f"PII Redaction Proxy ({provider.NAME})")
    http_client = httpx.AsyncClient(
        base_url=upstream,
        timeout=httpx.Timeout(connect=10, read=300, write=30, pool=10),
    )

    # Ops first, provider second: the provider's catch-all would shadow
    # /health and friends if it were registered before them.
    register_ops(app, provider.VERSION, logger, lambda: http_client, build=build)
    provider.register(app, http_client)

    logger.info("provider=%s version=%s build=%s upstream=%s",
                provider.NAME, provider.VERSION, build, upstream)
    return app
