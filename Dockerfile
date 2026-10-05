# Pinned by digest as well as tag, for the reason the workflow pins buildkit:
# a tag is mutable, so the tag is documentation and the digest is what we
# actually build on. Bumping is a deliberate edit — re-resolve the tag and
# move both halves together. The digest is the multi-arch index, not the
# amd64 manifest, so it stays right if a second platform is ever added.
FROM python:3.12-slim@sha256:e5c9fa26ffb76e11e0f054f30dc2523a2f9693f0c36c0cf1e39b27e152d899fc

# curl for the healthcheck; the compilers are build-only and removed below.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gcc g++ python3-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && \
    python -m spacy download en_core_web_sm && \
    rm /tmp/requirements.txt

RUN apt-get purge -y gcc g++ python3-dev && apt-get autoremove -y

COPY pii_redact_proxy/ /app/pii_redact_proxy/

WORKDIR /app
EXPOSE 8082

# The commit this image was built from, stamped by CI and reported by /version
# beside the provider's behaviour version — which cannot answer "is this the
# new code" on its own, because it does not change on every commit.
#
# Defaulted rather than required: an image built without it answers `unknown`
# and still starts. Identity is diagnostics, and a proxy that refuses to serve
# because it cannot name itself is a worse outage than one that cannot.
#
# Above the smoke below, not at the end, so that check runs with the stamp in
# place. It costs no cache: the COPY above already invalidates everything here
# on every commit.
ARG BUILD_SHA=unknown
ENV BUILD_SHA=${BUILD_SHA}

# config.yml is NOT copied in. It lists the domains, hostnames, usernames and
# static mappings we redact, so it stays with the deployment and is mounted at
# /app/config.yml — the image is generic, the targets are not.

# Import the package for real, with the real dependencies, in the real layout.
# The container is the only place every import line executes: the fixtures stub
# redactor and session wholesale, so a flat `from recognizers import ...` that
# resolves only in a flat app directory would survive every other gate and
# crash-loop on first run. build_app() is one call short of what CMD does, so
# an image that cannot start cannot be pushed. Every provider is assembled, not
# just the first one that shipped: one image serves all of them by PROVIDER.
#
# config.yml is deliberately absent from this image, and load_config() runs at
# import, so the check supplies an empty one and leaves nothing behind — same
# layer, so it is not in the image either.
# It also reads /version back, which is the only check that the BUILD_SHA above
# actually reached the route: a misspelled build-arg leaves the wiring silent
# and `unknown` only shows up on a container someone has already deployed.
RUN cd /tmp && \
    printf 'domains: []\nhostnames: []\nusernames: []\npaths: []\nstatic_mappings: {}\n' > config.yml && \
    PYTHONPATH=/app python -c "\
import asyncio, os; \
from pii_redact_proxy import build_app; \
from pii_redact_proxy.providers import base; \
names = base.available(); \
assert names, 'no providers discovered'; \
version = lambda app: next(r for r in app.routes if getattr(r, 'path', None) == '/version').endpoint; \
bodies = {name: asyncio.run(version(build_app(name))()) for name in names}; \
print('/version ->', bodies); \
assert all(b['build'] == (os.getenv('BUILD_SHA') or 'unknown') for b in bodies.values()), bodies" && \
    rm config.yml

# PROVIDER selects the wire format. No default: an unset one should fail at
# start with the available names, not silently serve the wrong vendor.
ENV PROVIDER=""
CMD ["python", "-m", "pii_redact_proxy"]
