"""Same-user contention must preserve every successful chat append."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def chat_store(tmp_path, monkeypatch):
    import storage
    monkeypatch.setattr(storage, "get_data_dir", lambda: str(tmp_path))
    return storage


def test_concurrent_first_chat_appends_keep_all_messages(chat_store):
    def append(i):
        chat_store.append_chat_message({"id": str(i), "role": "user", "time": "2026-01-01 12:00:00"})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(128)))
    messages = chat_store.get_chat_history(limit=1000)
    assert len(messages) == 128
    assert {m["id"] for m in messages} == {str(i) for i in range(128)}


def test_archiving_cannot_overwrite_concurrent_appends(chat_store):
    def action(i):
        if i % 3 == 0:
            chat_store.archive_old_chat_messages(keep_recent=10)
        chat_store.append_chat_message({"id": str(i), "time": "2026-01-01 12:00:00"})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(action, range(96)))
    messages = chat_store.get_chat_history(limit=1000) + chat_store.get_archived_chat_history("2026-01")
    assert len(messages) == 96
    assert {m["id"] for m in messages} == {str(i) for i in range(96)}


def test_separate_processes_share_chat_transaction_lock(chat_store, tmp_path):
    script = """import storage,sys
storage.get_data_dir = lambda: sys.argv[1]
for i in range(24):
    storage.append_chat_message({'id': sys.argv[2] + '-' + str(i)})
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(i)],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for i in range(4)]
    try:
        for process in processes:
            _, error = process.communicate(timeout=20)
            assert process.returncode == 0, error.decode()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    messages = chat_store.get_chat_history(limit=1000)
    assert len(messages) == 96
    assert len({m["id"] for m in messages}) == 96


def test_failed_write_preserves_existing_json_and_cleans_temp_file(chat_store, tmp_path):
    path = tmp_path / "document.json"
    chat_store.write_json(path, [{"id": "existing"}])
    with pytest.raises(TypeError):
        chat_store.write_json(path, {"unserializable": object()})
    assert json.loads(path.read_text()) == [{"id": "existing"}]
    assert not list(tmp_path.glob("*.tmp"))
