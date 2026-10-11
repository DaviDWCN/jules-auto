"""General utilities module."""

def sanitize_string(text: str) -> str:
    """Strip leading and trailing whitespace."""
    return text.strip() if text else ""

# TODO: implement safe_json_loads with fallback default value
