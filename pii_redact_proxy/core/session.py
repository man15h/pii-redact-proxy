"""Per-session entity mapping store for consistent redaction/de-redaction."""

import threading


class EntityMapper:
    """Maps original PII values to numbered placeholders and back.

    Thread-safe. Uses a global mapping, which assumes one client session
    at a time.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # entity_type -> {original_value: placeholder}
        self._forward: dict[str, dict[str, str]] = {}
        # placeholder -> original_value
        self._reverse: dict[str, str] = {}
        # entity_type -> next counter
        self._counters: dict[str, int] = {}

    def get_placeholder(self, entity_type: str, original: str) -> str:
        """Get or create a placeholder for the given original value."""
        with self._lock:
            type_map = self._forward.setdefault(entity_type, {})
            if original in type_map:
                return type_map[original]
            counter = self._counters.get(entity_type, 0) + 1
            self._counters[entity_type] = counter
            placeholder = f"<{entity_type}_{counter}>"
            type_map[original] = placeholder
            self._reverse[placeholder] = original
            return placeholder

    def de_redact(self, text: str) -> str:
        """Replace all placeholders in text with original values."""
        with self._lock:
            for placeholder, original in self._reverse.items():
                text = text.replace(placeholder, original)
        return text

    def clear(self):
        """Reset all mappings."""
        with self._lock:
            self._forward.clear()
            self._reverse.clear()
            self._counters.clear()

    def get_mappings_snapshot(self) -> dict[str, str]:
        """Return a copy of forward mappings for logging/debugging."""
        with self._lock:
            result = {}
            for type_map in self._forward.values():
                result.update(type_map)
            return result


# Global mapper instance
mapper = EntityMapper()
