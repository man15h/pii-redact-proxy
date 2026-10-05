"""Generic JSON traversal shared by every provider.

Only what is genuinely wire-format-agnostic lives here. `redact_json_values`
walks any JSON-like structure and redacts string *values*, leaving keys alone —
that is identical work whatever the provider calls its fields.

`_extract_texts` deliberately does NOT live here. More than one provider
defines one, but they walk different shapes (`messages[].content` versus
`input[]`/`instructions`), so they are provider knowledge wearing a shared
name. Each stays with its provider.
"""
from .redactor import redact


def redact_json_values(obj):
    """Recursively redact string values in a JSON-like structure (keys untouched)."""
    if isinstance(obj, str):
        return redact(obj)
    elif isinstance(obj, dict):
        return {k: redact_json_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [redact_json_values(item) for item in obj]
    return obj
