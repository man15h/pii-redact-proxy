"""Operational routes and request logging, in one copy for every provider.

These four routes plus the logging middleware are the same for every
provider, so there is one copy rather than one per provider. A per-provider
`/mappings` in particular is how a leak creeps in — see the docstring on
`mappings` below.

`passthrough` is not here: each provider blocks a different set of paths, so
it is provider knowledge, not shared plumbing.
"""
import time

from fastapi import Request

from .session import mapper


def register_ops(app, version: str, logger, get_http_client, build: str = "unknown"):
    """Attach the shared ops surface to a provider's FastAPI app.

    `version` is the provider's own behaviour string and `build` is which image
    is serving it; see `version_route` for why both are needed. `build` defaults
    rather than being required because it is diagnostics — a caller that cannot
    supply one still gets a working proxy.

    `get_http_client` is a callable rather than the client itself so the
    shutdown hook closes whatever the module holds at shutdown time, not
    whatever it held at import time — which is what lets a test swap the client
    after import.
    """

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        """Log every incoming request and the proxy's response status.

        Logs method, path, content-type, content-encoding, content-length, and
        the user-agent so we can see exactly what each client build sends.
        Skips /health to avoid log spam from probes.
        """
        path = request.url.path
        if path == "/health":
            return await call_next(request)

        start = time.monotonic()
        logger.info(
            "→ %s %s ct=%s ce=%s cl=%s ua=%s",
            request.method,
            path,
            request.headers.get("content-type", "-"),
            request.headers.get("content-encoding", "-"),
            request.headers.get("content-length", "-"),
            request.headers.get("user-agent", "-"),
        )
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("✗ %s %s — unhandled exception in handler", request.method, path)
            raise
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "← %s %s status=%s in %dms",
            request.method,
            path,
            response.status_code,
            elapsed_ms,
        )
        return response

    @app.on_event("shutdown")
    async def shutdown():
        await get_http_client().aclose()

    @app.get("/health")
    async def health():
        return {"status": "ok", "mappings_count": len(mapper.get_mappings_snapshot())}

    @app.get("/version")
    async def version_route():
        """Identify the running build so we can confirm a deploy actually landed.

        Two fields with two jobs. `version` is the provider's behaviour string,
        bumped by hand when behaviour changes, and the characterization
        fixtures pin it. `build` is which image is answering, stamped from the
        commit at build time.

        `version` alone cannot do this job. It only moves when behaviour does,
        so two builds that behave the same report the same string and the
        payload cannot say which code is serving. That is exactly the question
        a rollback asks.

        `build` is `unknown` on any image built without the stamp, and on a
        local run from a source tree. Unknown identity is a diagnostic answer,
        never a reason to refuse to start.
        """
        return {"version": version, "build": build}

    @app.get("/mappings")
    async def mappings():
        """Show current redaction placeholders (no originals).

        This endpoint is unauthenticated on the LAN and the proxy may be shared
        by several clients, so returning original values here would leak every
        accumulated secret to any LAN client. Placeholders + a
        preview of the original's first 4 chars are enough for debugging which
        mappings exist.

        Read this before adding a provider. A `/mappings` that returns the raw
        placeholder-to-original map hands every secret the proxy has
        accumulated to any caller. Every provider uses this implementation and
        no other; do not give one its own `/mappings`.
        """
        snapshot = mapper.get_mappings_snapshot()
        return {
            "count": len(snapshot),
            "placeholders": sorted(
                f"{v}  (orig: {k[:4]}…, len {len(k)})" for k, v in snapshot.items()
            ),
        }

    @app.post("/mappings/clear")
    async def clear_mappings():
        """Reset all redaction mappings."""
        mapper.clear()
        return {"status": "cleared"}
