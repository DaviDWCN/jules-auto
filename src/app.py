"""Sample API service implementation."""

def get_health():
    return {"status": "ok", "service": "your-api"}

def get_version():
    return {"version": "0.1.0"}
