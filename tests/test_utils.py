from src.utils import sanitize_string, safe_json_loads

def test_sanitize_string():
    assert sanitize_string("  hello  ") == "hello"
    assert sanitize_string("world") == "world"
    assert sanitize_string("") == ""
    assert sanitize_string(None) == ""

def test_safe_json_loads_valid():
    assert safe_json_loads('{"a": 1}') == {"a": 1}
    assert safe_json_loads('[1, 2, 3]') == [1, 2, 3]
    assert safe_json_loads('"hello"') == "hello"

def test_safe_json_loads_invalid_json():
    # default fallback
    assert safe_json_loads('{"a": 1') is None
    # custom fallback
    assert safe_json_loads('{"a": 1', default={}) == {}
    assert safe_json_loads('invalid', default="fallback") == "fallback"

def test_safe_json_loads_invalid_type():
    # default fallback
    assert safe_json_loads(None) is None
    assert safe_json_loads(123) is None
    # custom fallback
    assert safe_json_loads(None, default={}) == {}
