"""Custom Presidio recognizers loaded from config.yml."""

import re

import yaml
from presidio_analyzer import PatternRecognizer, Pattern


def load_config(config_path: str = "config.yml") -> dict:
    """Load the redaction targets.

    Deliberately not shipped in the image. This file lists the domains,
    hostnames, usernames and static mappings we redact — i.e. the very things
    worth protecting — so it stays with the deployment and is mounted in, and
    this repo never holds a copy. Missing it is fatal by design: a proxy that
    started with an empty target list would redact nothing and say nothing.
    """
    try:
        with open(config_path) as f:
            return yaml.safe_load(f)
    except FileNotFoundError as exc:
        raise SystemExit(
            f"FATAL: {config_path} not found. It is not baked into the image — "
            "mount it (compose: ./config.yml:/app/config.yml:ro)."
        ) from exc


def build_recognizers(config: dict) -> list[PatternRecognizer]:
    """Build Presidio PatternRecognizers from config."""
    recognizers = []

    # Private IP recognizer (built-in, not from config)
    recognizers.append(PatternRecognizer(
        supported_entity="PRIVATE_IP",
        patterns=[
            Pattern("rfc1918_10", r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", 0.9),
            Pattern("rfc1918_172", r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", 0.9),
            Pattern("rfc1918_192", r"\b192\.168\.\d{1,3}\.\d{1,3}\b", 0.9),
            Pattern("cgnat", r"\b100\.(6[4-9]|[7-9]\d|1[0-2]\d)\.\d{1,3}\.\d{1,3}\b", 0.9),
        ],
    ))

    # SSH URL recognizer
    recognizers.append(PatternRecognizer(
        supported_entity="SSH_URL",
        patterns=[
            Pattern("ssh_url", r"ssh://[\w.\-]+@[\w.\-:]+(?:/\S*)?", 0.95),
        ],
    ))

    # Domain recognizers from config
    for i, domain_conf in enumerate(config.get("domains", [])):
        pattern_str = domain_conf["pattern"]
        prefix = domain_conf.get("replacement_prefix", "DOMAIN")
        recognizers.append(PatternRecognizer(
            supported_entity="CUSTOM_DOMAIN",
            patterns=[
                Pattern(f"domain_{i}", rf"\b{pattern_str}\b", 0.85),
            ],
        ))

    # Hostname recognizers from config
    hostnames = config.get("hostnames", [])
    if hostnames:
        # Build alternation pattern with word boundaries
        names = "|".join(re.escape(h) for h in hostnames)
        recognizers.append(PatternRecognizer(
            supported_entity="INTERNAL_HOSTNAME",
            patterns=[
                Pattern("hostnames", rf"\b({names})\b", 0.7),
            ],
        ))

    # Username recognizers from config
    usernames = config.get("usernames", [])
    if usernames:
        names = "|".join(re.escape(u) for u in usernames)
        recognizers.append(PatternRecognizer(
            supported_entity="CUSTOM_USERNAME",
            patterns=[
                Pattern("usernames", rf"\b({names})\b", 0.6),
            ],
        ))

    # API key / token recognizer (catches common secret patterns in tool results)
    recognizers.append(PatternRecognizer(
        supported_entity="API_TOKEN",
        patterns=[
            # Anthropic API keys
            Pattern("anthropic_key", r"\bsk-ant-api\w{2}-[\w\-]{20,}\b", 0.95),
            # OpenAI-style keys
            Pattern("openai_key", r"\bsk-[A-Za-z0-9]{20,}\b", 0.85),
            # GitHub tokens (classic & fine-grained)
            Pattern("github_token", r"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}\b", 0.95),
            # GitLab tokens
            Pattern("gitlab_token", r"\bglpat-[A-Za-z0-9\-_]{20,}\b", 0.95),
            # AWS access keys
            Pattern("aws_key", r"\bAKIA[A-Z0-9]{16}\b", 0.95),
            # Generic bearer/auth tokens (long base64-ish strings after common prefixes).
            # [ \t] only — \s matched newlines, fusing a trailing "token" word with an
            # unrelated string on the next line and storing the over-matched span in the
            # mapper (seen live: "token\n<40-hex>" mappings that replayed swallowed
            # adjacent text into later de-redactions).
            Pattern("bearer_token", r"(?i)(?:bearer|token|authorization)[: \t]+[A-Za-z0-9\-_\.]{30,}", 0.8),
        ],
    ))

    # Path recognizers from config
    for i, path_conf in enumerate(config.get("paths", [])):
        pattern_str = path_conf["pattern"]
        recognizers.append(PatternRecognizer(
            supported_entity="SENSITIVE_PATH",
            patterns=[
                Pattern(f"path_{i}", pattern_str, 0.8),
            ],
        ))

    return recognizers
