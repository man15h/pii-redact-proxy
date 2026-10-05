"""Presidio analyzer + anonymizer wrapper with session-aware entity mapping."""

import hashlib
import logging
import os
from collections import OrderedDict

from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

# Relative, not flat. A flat `from recognizers import ...` resolves only when
# the app is a directory of sibling modules on sys.path. Inside a package it
# resolves to nothing, and the container exits 1 before uvicorn binds.
from .recognizers import load_config, build_recognizers
from .session import mapper

logger = logging.getLogger(__name__)

# Entity types we care about (custom + selective built-in)
CUSTOM_ENTITIES = [
    "PRIVATE_IP",
    "SSH_URL",
    "CUSTOM_DOMAIN",
    "INTERNAL_HOSTNAME",
    "CUSTOM_USERNAME",
    "SENSITIVE_PATH",
    "API_TOKEN",
]

# Load config and build recognizers once at import time
_config = load_config()
_recognizers = build_recognizers(_config)
_static_mappings = _config.get("static_mappings", {})

# Merge env-based token redactions (injected via the environment)
_TOKEN_ENV_PREFIX = "PII_REDACT_"
for _key, _value in os.environ.items():
    if _key.startswith(_TOKEN_ENV_PREFIX) and _value:
        _label = _key[len(_TOKEN_ENV_PREFIX):]  # e.g., "GITHUB_TOKEN"
        _static_mappings[_value] = f"<{_label}>"

# Sort static mappings by length (longest first) to avoid partial replacements
_static_sorted = sorted(_static_mappings.items(), key=lambda x: len(x[0]), reverse=True)

# Build global reverse map for de-redaction (replacement → original)
# Sort by length (longest first) to avoid partial replacements
_static_reverse = sorted(
    [(v, k) for k, v in _static_mappings.items()],
    key=lambda x: len(x[0]), reverse=True,
)

# Initialize Presidio engines — use small spaCy model (all our recognizers are regex-based)
_nlp_provider = NlpEngineProvider(nlp_configuration={
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
})
_analyzer = AnalyzerEngine(nlp_engine=_nlp_provider.create_engine())
for rec in _recognizers:
    _analyzer.registry.add_recognizer(rec)

_anonymizer = AnonymizerEngine()

# LRU cache for redaction results (bounded to prevent memory bloat)
_CACHE_MAX = 2048
_redact_cache: OrderedDict[str, str] = OrderedDict()


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _apply_static_mappings(text: str) -> tuple[str, dict[str, str]]:
    """Apply case-insensitive static string replacements. Returns modified text and reverse map."""
    reverse = {}
    text_lower = text.lower()
    for original, replacement in _static_sorted:
        orig_lower = original.lower()
        idx = text_lower.find(orig_lower)
        while idx != -1:
            # Replace the matched span (preserving surrounding text)
            reverse[replacement] = text[idx:idx + len(original)]
            text = text[:idx] + replacement + text[idx + len(original):]
            text_lower = text.lower()
            logger.info("Static redacted: %s -> %s", original[:8] + "...", replacement)
            idx = text_lower.find(orig_lower, idx + len(replacement))

    return text, reverse


def _undo_static_mappings(text: str, reverse: dict[str, str]) -> str:
    """Undo static replacements."""
    for replacement, original in reverse.items():
        text = text.replace(replacement, original)
    return text


def redact(text: str) -> str:
    """Redact PII from text destined for an upstream provider.

    Uses a hash-based cache to skip re-analysis of identical text blocks.
    """
    if not text:
        return text

    key = _cache_key(text)
    if key in _redact_cache:
        _redact_cache.move_to_end(key)
        return _redact_cache[key]

    result = _redact_uncached(text)

    # Store in bounded cache
    _redact_cache[key] = result
    if len(_redact_cache) > _CACHE_MAX:
        _redact_cache.popitem(last=False)

    return result


def _redact_uncached(text: str) -> str:
    """Run full redaction pipeline."""
    # Step 1: Static mappings
    text, _ = _apply_static_mappings(text)

    # Step 2: Presidio analysis (only our custom entities)
    results = _analyzer.analyze(
        text=text,
        entities=CUSTOM_ENTITIES,
        language="en",
    )

    if not results:
        return text

    # Step 3: Sort by start position (descending) to replace from end
    results.sort(key=lambda r: r.start, reverse=True)

    # Deduplicate overlapping spans — keep highest score
    filtered = []
    for result in results:
        overlaps = False
        for existing in filtered:
            if result.start < existing.end and result.end > existing.start:
                overlaps = True
                break
        if not overlaps:
            filtered.append(result)

    # Replace each detected entity with a session-consistent placeholder
    for result in filtered:
        original = text[result.start:result.end]
        placeholder = mapper.get_placeholder(result.entity_type, original)
        text = text[:result.start] + placeholder + text[result.end:]
        # Truncate the original — full values in container logs would leak
        # secrets to anything that can read logs (Dozzle, docker logs).
        logger.info("Redacted %s: %s... -> %s", result.entity_type, original[:8], placeholder)

    return text


def de_redact(text: str) -> str:
    """Replace placeholders in API response text with original values."""
    if not text:
        return text
    # Reverse Presidio entity placeholders
    text = mapper.de_redact(text)
    # Reverse static mappings
    for replacement, original in _static_reverse:
        if replacement in text:
            text = text.replace(replacement, original)
    return text
