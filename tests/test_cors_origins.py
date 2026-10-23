import pytest


@pytest.mark.parametrize("origin", [
    "http://localhost.audit.invalid", "http://127.0.0.1.evil.test",
    "http://192.168.example.test", "http://10.attacker.test", "http://172.1.2.3",
    "https://companion.example.evil.test", "https://companion.example:444",
    "https://tunnel.example.evil.test", "http://localhost@evil.test", "null",
    "http://localhost:bad", "http://localhost/path", "http://localhost#evil",
    "http://local\thost", "https://localhost?next=evil",
])
def test_hostname_prefixes_and_malformed_origins_are_not_trusted(monkeypatch, origin):
    import app
    monkeypatch.setenv("DOMAIN", "companion.example")
    monkeypatch.setenv("TUNNEL_URL", "https://tunnel.example")
    response = app.app.test_client().get("/api/health", headers={"Origin": origin})
    assert "Access-Control-Allow-Origin" not in response.headers


@pytest.mark.parametrize("origin", [
    "http://localhost:5001", "http://127.0.0.1:9000", "http://[::1]:5001",
    "https://localhost", "tauri://localhost", "capacitor://localhost",
    "http://192.168.1.2:5001", "http://10.1.2.3", "http://172.16.1.2",
    "https://companion.example", "https://companion.example:443", "https://tunnel.example",
])
def test_existing_native_lan_and_configured_origins_still_work(monkeypatch, origin):
    import app
    monkeypatch.setenv("DOMAIN", "companion.example")
    monkeypatch.setenv("TUNNEL_URL", "https://tunnel.example")
    response = app.app.test_client().options("/api/chat", headers={"Origin": origin})
    assert response.headers["Access-Control-Allow-Origin"] == origin
    assert "PUT" in response.headers["Access-Control-Allow-Methods"]
    assert "Origin" in response.vary
