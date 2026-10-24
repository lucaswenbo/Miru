"""One browser endpoint belongs to one user, with versioned subscription keys."""

import hashlib
import json
import os
import uuid

from flask import g

import auth
import storage
from file_store import atomic_write_text, file_transaction


def _path():
    return os.path.join(auth._BASE_DATA_DIR, "_admin", "push_bindings.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _key(subscription):
    return hashlib.sha256(subscription["endpoint"].encode()).hexdigest()


def _version(subscription):
    return hashlib.sha256(json.dumps(subscription, sort_keys=True).encode()).hexdigest()


def _user_id():
    uid = getattr(g, "user_id", None)
    if not uid or uid == "_admin":
        raise ValueError("user context required for browser push")
    storage.get_data_dir()  # Reject stale/deleted managed user contexts.
    return uid


class BindingConflict(ValueError):
    pass


def revision(endpoint):
    _user_id()
    with file_transaction(_path()):
        return _load().get(_key({"endpoint": endpoint}), {}).get("revision")


def register(subscription, *, expected_revision=None):
    uid = _user_id()
    path = storage.push_subscriptions_path()
    with file_transaction(_path()), storage.json_transaction(path):
        bindings = _load()
        previous = bindings.get(_key(subscription))
        if previous and (previous["user_id"] != uid or expected_revision is not None):
            if expected_revision != previous.get("revision"):
                raise BindingConflict("push binding changed; retry with current revision")
        bindings[_key(subscription)] = {"user_id": uid, "version": _version(subscription),
                                        "revision": uuid.uuid4().hex}
        # Rebinding first revokes the old user's ability to send. A failure
        # writing the new user's file therefore fails closed.
        atomic_write_text(_path(), json.dumps(bindings))
        current = storage.read_json(path) or []
        current = [s for s in current if s.get("endpoint") != subscription["endpoint"]]
        storage.write_json(path, current + [subscription])


def subscriptions():
    uid = _user_id()
    with file_transaction(_path()):
        bindings = _load()
        current = storage.read_json(storage.push_subscriptions_path()) or []
        return [s for s in current if isinstance(s, dict) and s.get("endpoint")
                and bindings.get(_key(s), {}).get("user_id") == uid
                and bindings.get(_key(s), {}).get("version") == _version(s)]


def unregister(endpoint, *, snapshot=None):
    """Remove only this user's binding and, for expiry, the exact sent version."""
    uid = _user_id()
    path = storage.push_subscriptions_path()
    with file_transaction(_path()), storage.json_transaction(path):
        bindings = _load()
        key = _key({"endpoint": endpoint})
        binding = bindings.get(key)
        if binding and binding.get("user_id") == uid:
            if snapshot is None or binding.get("version") == _version(snapshot):
                bindings.pop(key)
                atomic_write_text(_path(), json.dumps(bindings))
        current = storage.read_json(path) or []
        retained = [s for s in current if not (s.get("endpoint") == endpoint
                    and (snapshot is None or s == snapshot))]
        if len(retained) != len(current):
            storage.write_json(path, retained)
        return len(current) - len(retained)
