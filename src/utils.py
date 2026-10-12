"""General utilities module."""
import json
from typing import Any

def sanitize_string(text: str) -> str:
    """Strip leading and trailing whitespace."""
    return text.strip() if text else ""

def safe_json_loads(data: str, default: Any = None) -> Any:
    """Safely parse a JSON string, falling back to a default value on error."""
    try:
        return json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return default
