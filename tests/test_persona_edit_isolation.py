"""Exercise the HTTP persona editors with real per-user storage."""

import json

import pytest


TEMPLATE = """# Identity

- **Name**: Template
- **User Address**: Friend
- **Species**: Original character

# Personality

Warm and curious.

# Emotional Reactions

Keep emotional continuity.

# Relationship Stages

Build trust gradually.

# Agent Behavior

Listen before advising.

# Reference Images

- **Avatar**: original.png

# Prompt Hints

- archetype: friend
- interest: books

# Custom Section

Never discard this section.
"""


@pytest.fixture
def persona_app(tmp_path, monkeypatch):
    import app
    import auth
    import character
    import curator
    import model_library
    import storage

    data = tmp_path / "data"
    admin = data / "_admin"
    admin.mkdir(parents=True)
    users = {uid: {"token": f"test-token-{uid}", "status": "active",
                   "created_at": "2026-01-01T00:00:00"}
             for uid in ("u_alice", "u_bob", "u_fresh")}
    (admin / "users.json").write_text(json.dumps(users))
    for uid in users:
        (data / "users" / uid).mkdir(parents=True)
    template = tmp_path / "bundled-soul.md"
    template.write_text(TEMPLATE)
    monkeypatch.setattr(storage, "DATA_DIR", str(data))
    monkeypatch.setattr(auth, "_BASE_DATA_DIR", str(data))
    monkeypatch.setattr(auth, "_ADMIN_DIR", str(admin))
    monkeypatch.setattr(model_library, "_DEFAULT_SOUL_PATH", str(template))
    monkeypatch.setattr(character, "_SOUL_PATH", str(template))
    monkeypatch.setattr(character, "_configs", {})
    monkeypatch.setattr(app, "_is_client_mode", False)
    monkeypatch.setattr(app, "_client_mode_config", {})
    monkeypatch.setattr(curator, "start_loop_for_user", lambda *_: None)
    return app.app.test_client(), data, template


def headers(uid):
    return {"Authorization": f"Bearer test-token-{uid}"}


@pytest.mark.parametrize("endpoint,payload", [
    ("/api/character/fields", {"name": "Alice's companion"}),
    ("/api/settings/soul", {"content": TEMPLATE.replace("Template", "Alice's companion")}),
])
def test_default_persona_edits_are_private_even_after_cache_reload(persona_app, endpoint, payload):
    client, data, template = persona_app
    import character
    assert client.get("/api/character", headers=headers("u_bob")).json["name"] == "Template"
    response = client.post(endpoint, headers=headers("u_alice"), json=payload)
    assert response.status_code == 200
    assert client.get("/api/character", headers=headers("u_alice")).json["name"] == "Alice's companion"
    character._configs.clear()  # A restart must not hide shared-template writes.
    for uid in ("u_bob", "u_fresh"):
        assert client.get("/api/character", headers=headers(uid)).json["name"] == "Template"
    assert template.read_text() == TEMPLATE
    assert (data / "users/u_alice/personas/_default/soul.md").exists()
    assert not (data / "users/u_bob/personas/_default/soul.md").exists()


def test_partial_edit_preserves_hidden_sections_and_unknown_fields(persona_app):
    client, _, _ = persona_app
    h = headers("u_alice")
    response = client.post("/api/character/fields", headers=h, json={
        "name": "New name", "personality": "Curious.", "prompt_hints": {"interest": "music"},
    })
    assert response.status_code == 200
    saved = client.get("/api/settings/soul", headers=h).json["content"]
    for heading in ("Emotional Reactions", "Relationship Stages", "Agent Behavior", "Reference Images", "Custom Section"):
        original = TEMPLATE.split("# " + heading + "\n", 1)[1].split("\n# ", 1)[0]
        assert original in saved
    assert "**Species**: Original character" in saved
    assert "- archetype: friend" in saved
    assert "- interest: music" in saved
    assert "- interest: books" not in saved


def test_same_model_id_has_separate_personas_for_each_user(persona_app):
    client, data, template = persona_app
    for uid in ("u_alice", "u_bob"):
        (data / "users" / uid / "model_library.json").write_text(json.dumps({
            "active_model_id": "shared-id", "models": [{"id": "shared-id", "name": "Model"}],
        }))
    assert client.post("/api/character/fields", headers=headers("u_alice"), json={"name": "Alice"}).status_code == 200
    assert client.post("/api/character/fields", headers=headers("u_bob"), json={"name": "Bob"}).status_code == 200
    for uid, expected in (("u_alice", "Alice"), ("u_bob", "Bob")):
        assert client.get("/api/character", headers=headers(uid)).json["name"] == expected
        assert (data / "users" / uid / "personas/shared-id/soul.md").exists()
    assert template.read_text() == TEMPLATE


@pytest.mark.parametrize("payload", [{"name": ["bad"]}, {"name": "bad\n# Identity"},
                                     {"personality": None}, {"prompt_hints": []}])
def test_invalid_edit_leaves_persona_unchanged(persona_app, payload):
    client, _, template = persona_app
    response = client.post("/api/character/fields", headers=headers("u_alice"), json=payload)
    assert response.status_code == 400
    assert client.get("/api/settings/soul", headers=headers("u_alice")).json["content"] == TEMPLATE
    assert template.read_text() == TEMPLATE


def test_external_atomic_persona_update_invalidates_cached_character(persona_app):
    client, data, _ = persona_app
    from file_store import atomic_write_text
    h = headers("u_alice")
    assert client.post("/api/character/fields", headers=h, json={"name": "First"}).status_code == 200
    atomic_write_text(data / "users/u_alice/personas/_default/soul.md", TEMPLATE.replace("Template", "Second"))
    assert client.get("/api/character", headers=h).json["name"] == "Second"


def test_inactive_user_context_cannot_write_default_template(persona_app):
    import app, model_library
    from flask import g
    _, data, template = persona_app
    with app.app.app_context():
        g.user_id = "u_deleted"
        g.user_data_dir = str(data / "users/u_deleted")
        with pytest.raises(ValueError, match="context"):
            model_library.save_active_soul("overwrite")
    assert template.read_text() == TEMPLATE


def test_proactive_web_push_only_uses_current_users_subscriptions(persona_app, monkeypatch):
    from types import SimpleNamespace
    import app, attention_engine, core, persona_writer_state, pywebpush, sse, storage
    from flask import g

    client, data, _ = persona_app
    endpoints = {"u_alice": "https://fcm.googleapis.com/fcm/send/alice",
                 "u_bob": "https://web.push.apple.com/bob"}
    for uid, endpoint in endpoints.items():
        response = client.post("/api/push/subscribe", headers=headers(uid), json={
            "endpoint": endpoint, "keys": {"auth": "test-auth", "p256dh": "test-public"},
        })
        assert response.status_code == 200
    sent = []
    broadcasts = []
    monkeypatch.setattr(pywebpush, "webpush", lambda **kw: sent.append(kw))
    monkeypatch.setattr(core.server_config, "get", lambda: {
        "vapid_private_key": "test-private", "vapid_public_key": "test-public",
    })
    monkeypatch.setattr(sse, "broadcast", lambda event, msg, **kw: broadcasts.append(kw["user_id"]))
    monkeypatch.setattr(attention_engine, "get_attention_engine", lambda: SimpleNamespace(record_signal=lambda *_: None))
    monkeypatch.setattr(persona_writer_state, "record_proactive_outcome", lambda *_: None)
    with app.app.app_context():
        g.user_id = "u_alice"
        g.user_data_dir = str(data / "users/u_alice")
        message = core._append_attention_proactive_message("Hello Alice", {"id": "intent"}, {})
        assert storage.get_chat_history()[0]["id"] == message["id"]
    assert broadcasts == ["u_alice"]
    assert len(sent) == 1
    assert sent[0]["subscription_info"]["endpoint"] == endpoints["u_alice"]
    assert json.loads(sent[0]["data"])["tag"] == message["id"]
    assert sent[0]["timeout"] == 5
    assert client.get("/api/chat/history", headers=headers("u_bob")).json == []


def test_push_outage_does_not_lose_or_duplicate_the_chat_message(persona_app, monkeypatch):
    from types import SimpleNamespace
    import app, attention_engine, core, persona_writer_state, storage
    from flask import g
    _, data, _ = persona_app
    def unavailable(*_, **__):
        raise RuntimeError("fake push outage")
    monkeypatch.setattr(core, "send_push_message", unavailable)
    monkeypatch.setattr(attention_engine, "get_attention_engine", lambda: SimpleNamespace(record_signal=lambda *_: None))
    monkeypatch.setattr(persona_writer_state, "record_proactive_outcome", lambda *_: None)
    with app.app.app_context():
        g.user_id = "u_alice"
        g.user_data_dir = str(data / "users/u_alice")
        msg = core._append_attention_proactive_message("Still here", {"id": "intent"}, {})
        assert [m["id"] for m in storage.get_chat_history()] == [msg["id"]]
        assert storage.get_pending_notifications("test-device")[0]["id"] == msg["id"]


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1/internal", "https://127.0.0.1/",
    "https://fcm.googleapis.com.evil.test/", "https://fcm.googleapis.com:8443/",
    "https://fcm.googleapis.com@evil.test/", "https://user@fcm.googleapis.com/",
    "file:///etc/passwd", "https://fcm.google\tapis.com/", "https://web.push.apple.com/#bad"])
def test_push_subscription_rejects_arbitrary_destinations(persona_app, endpoint):
    client, _, _ = persona_app
    response = client.post("/api/push/subscribe", headers=headers("u_alice"), json={
        "endpoint": endpoint, "keys": {"auth": "a", "p256dh": "p"},
    })
    assert response.status_code == 400


def test_resubscribe_refreshes_keys_without_duplicate_endpoint(persona_app):
    client, data, _ = persona_app
    endpoint = "https://updates.push.services.mozilla.com/wpush/v2/test"
    for key in ("old", "new"):
        assert client.post("/api/push/subscribe", headers=headers("u_alice"), json={
            "endpoint": endpoint, "keys": {"auth": key, "p256dh": key},
        }).status_code == 200
    saved = json.loads((data / "users/u_alice/push_subscriptions.json").read_text())
    assert len(saved) == 1
    assert saved[0]["keys"]["auth"] == "new"
