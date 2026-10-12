import pytest
from src.utils import sanitize_string

def test_sanitize_string_normal():
    assert sanitize_string("  hello world  ") == "hello world"
    assert sanitize_string("hello") == "hello"

def test_sanitize_string_empty():
    assert sanitize_string("") == ""

def test_sanitize_string_none():
    assert sanitize_string(None) == ""

def test_sanitize_string_only_whitespace():
    assert sanitize_string("   \t\n") == ""
