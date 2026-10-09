from src.app import get_health, get_version

def test_health():
    res = get_health()
    assert res["status"] == "ok"
    assert res["service"] == "your-api"

def test_version():
    res = get_version()
    assert res["version"] == "0.1.0"
