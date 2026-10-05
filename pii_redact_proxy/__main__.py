"""Container entrypoint. `PROVIDER` picks the wire format, nothing else does.

Choosing the app by which module uvicorn is pointed at would make the compose
file encode the choice as a Python import path, and every service block would
differ in more than configuration.
"""
import logging
import os

import uvicorn

from . import build_app
from .providers import base


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    provider = os.getenv("PROVIDER")
    if not provider:
        raise SystemExit(
            f"PROVIDER is required; available: {', '.join(base.available())}"
        )
    uvicorn.run(
        build_app(provider),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8082")),
    )


if __name__ == "__main__":
    main()
