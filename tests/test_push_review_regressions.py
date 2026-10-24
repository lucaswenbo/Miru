"""Browser-account ownership and delayed notification regression tests."""

import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from test_persona_edit_isolation import persona_app, headers


def _subscribe(client, uid, endpoint, key="key", revision=None):
    payload = {"endpoint": endpoint, "keys": {"auth": key, "p256dh": key}}
    if revision is not None:
        payload["binding_version"] = revision
    return client.post("/api/push/subscribe", headers=headers(uid), json=payload)


def _binding(client, uid, endpoint):
    return client.get("/api/push/binding", headers=headers(uid), query_string={"endpoint": endpoint}).json["revision"]


def test_same_browser_rebind_revokes_old_user_and_rejects_late_registration(persona_app):
    import app, push_registry
    from flask import g
    client, data, _ = persona_app
    endpoint = "https://fcm.googleapis.com/fcm/send/same-browser"
    assert _subscribe(client, "u_alice", endpoint).status_code == 200
    old_revision = _binding(client, "u_alice", endpoint)
    assert _subscribe(client, "u_bob", endpoint, revision=old_revision).status_code == 200
    assert _subscribe(client, "u_alice", endpoint, revision=old_revision).status_code == 409
    assert _subscribe(client, "u_alice", endpoint).status_code == 409
    with app.app.app_context():
        g.user_id = "u_alice"
        g.user_data_dir = str(data / "users/u_alice")
        assert push_registry.subscriptions() == []
        # An old logout must not remove the new owner's binding.
        push_registry.unregister(endpoint)
        g.user_id = "u_bob"
        g.user_data_dir = str(data / "users/u_bob")
        assert [s["endpoint"] for s in push_registry.subscriptions()] == [endpoint]


def test_expired_old_keys_do_not_delete_refreshed_subscription(persona_app, monkeypatch):
    import app, core, pywebpush, push_registry
    from flask import g
    client, data, _ = persona_app
    endpoint = "https://fcm.googleapis.com/fcm/send/rotate"
    assert _subscribe(client, "u_alice", endpoint, "old").status_code == 200
    def expire_old_snapshot(**kwargs):
        assert kwargs["subscription_info"]["keys"]["auth"] == "old"
        assert _subscribe(client, "u_alice", endpoint, "new").status_code == 200
        raise pywebpush.WebPushException("expired", response=SimpleNamespace(status_code=410))
    monkeypatch.setattr(pywebpush, "webpush", expire_old_snapshot)
    monkeypatch.setattr(core.server_config, "get", lambda: {
        "vapid_private_key": "test-private", "vapid_public_key": "test-public"})
    with app.app.app_context():
        g.user_id = "u_alice"
        g.user_data_dir = str(data / "users/u_alice")
        core.send_push_message("test", "private message")
        remaining = push_registry.subscriptions()
        assert len(remaining) == 1
        assert remaining[0]["keys"]["auth"] == "new"


def test_windows_lock_keeps_retrying_and_propagates_non_contention_errors(tmp_path, monkeypatch):
    import file_store
    attempts = []
    sleeps = []
    def lock(fd, mode, length):
        attempts.append(mode)
        if len(attempts) <= 15:
            raise OSError(errno.EACCES, "busy")
    api = SimpleNamespace(LK_NBLCK=123, locking=lock)
    monkeypatch.setattr(file_store.time, "sleep", lambda delay: sleeps.append(delay))
    with open(tmp_path / "lock", "w+b") as handle:
        file_store._acquire_windows_lock(handle, api)
        assert attempts == [123] * 16
        assert len(sleeps) == 15
        api.locking = lambda *_: (_ for _ in ()).throw(OSError(errno.EBADF, "bad descriptor"))
        with pytest.raises(OSError, match="bad descriptor"):
            file_store._acquire_windows_lock(handle, api)


def test_attention_delivery_is_committed_before_push_and_recovers_after_append_crash(persona_app, monkeypatch):
    import app, core, prompt, storage
    from flask import g
    _, data, _ = persona_app
    calls = []
    def agent(*_, **__):
        calls.append("generated")
        return {"reply": "Only once", "tool_calls": []}
    monkeypatch.setattr(prompt, "call_proactive_agent", agent)
    monkeypatch.setattr(core, "_build_chat_context", lambda: "")
    monkeypatch.setattr(core, "_proactive_toolset", lambda *_: ({}, []))
    monkeypatch.setattr(core, "_attention_delivery_timing_suppression_reason", lambda: "")
    pushes = []
    def push(*_, **__):
        assert storage.load_attention_intent_queue()[0]["status"] == "delivered"
        pushes.append("sent")
    monkeypatch.setattr(core, "send_push_message", push)
    with app.app.app_context():
        g.user_id = "u_alice"
        g.user_data_dir = str(data / "users/u_alice")
        def seed(ident):
            storage.save_attention_intent_queue([{"id": ident, "status": "pending",
                "expires_at": "2099-01-01 00:00:00", "topic_key": "test"}])
        seed("normal")
        assert core.deliver_attention_intent_once("normal")["delivery_status"] == "delivered"
        assert pushes == ["sent"]
        assert core.deliver_attention_intent_once("normal") is None
        seed("crash")
        original = core._update_attention_intent
        def crash_on_mark(ident, patch):
            if patch.get("status") == "delivered":
                raise OSError("simulated crash after append")
            return original(ident, patch)
        with monkeypatch.context() as patcher:
            patcher.setattr(core, "_update_attention_intent", crash_on_mark)
            assert core.deliver_attention_intent_once("crash")["delivery_status"] == "failed"
        # Recovery must work even if normal maintenance archived the append.
        storage.append_chat_message({"id": "later", "role": "assistant", "text": "later",
                                     "time": "2026-01-01 00:00:00"})
        storage.archive_old_chat_messages(keep_recent=1)
        recovered = core.deliver_attention_intent_once("crash")
        assert recovered["recovered"] is True
        assert calls == ["generated", "generated"]
        assert len(storage.get_chat_history(limit=100)) == 1
        assert storage.load_attention_intent_queue()[0]["message_id"] == recovered["message_id"]


def test_attention_delivery_is_idempotent_across_processes(tmp_path, monkeypatch):
    import storage
    monkeypatch.setattr(storage, "get_data_dir", lambda: str(tmp_path))
    storage.save_attention_intent_queue([{"id": "same-intent", "status": "pending",
        "expires_at": "2099-01-01 00:00:00", "topic_key": "test"}])
    script = r"""
import core, prompt, time, sys
from pathlib import Path
root = Path(sys.argv[1])
core._build_chat_context = lambda: ''
core._proactive_toolset = lambda *_: ({}, [])
core._attention_delivery_timing_suppression_reason = lambda: ''
core._push_proactive_message = lambda *_: None
def agent(*_, **__):
    with (root/'generations').open('a') as handle: handle.write('one\n')
    time.sleep(.05)
    return {'reply':'Exactly one message', 'tool_calls':[]}
prompt.call_proactive_agent = agent
(root/('ready-' + sys.argv[2])).touch()
deadline = time.monotonic() + 10
while not (root/'go').exists():
    if time.monotonic() > deadline: raise RuntimeError('barrier timeout')
    time.sleep(.01)
core.deliver_attention_intent_once('same-intent')
"""
    env = dict(os.environ, DATA_DIR=str(tmp_path), MIRU_HEADLESS="1",
               PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), str(i)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(2)]
    try:
        deadline = time.monotonic() + 10
        while len(list(tmp_path.glob("ready-*"))) < 2:
            assert time.monotonic() < deadline
            time.sleep(.01)
        (tmp_path / "go").touch()
        for process in processes:
            _, error = process.communicate(timeout=15)
            assert process.returncode == 0, error.decode()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert (tmp_path / "generations").read_text().splitlines() == ["one"]
    assert len(storage.get_chat_history()) == 1


def test_worker_filters_accounts_foreground_and_persisted_old_notifications():
    worker = Path(__file__).resolve().parents[1] / "service-worker.js"
    harness = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const stores = new Map();
let shown = [], clients = [], closed = 0, opened = 0;
function worker() {
  const events = {};
  const caches = {
    async open(name) {
      if (!stores.has(name)) stores.set(name, new Map());
      const data = stores.get(name);
      return { async match(key) { return data.get(key)?.clone(); },
        async put(key, value) { data.set(key, value.clone()); }, async addAll() {} };
    },
    async keys() { return [...stores.keys()]; },
    async delete(name) { return stores.delete(name); }
  };
  const self = {
    location: {origin: 'https://miru.test'},
    addEventListener(type, fn) { events[type] = fn; }, skipWaiting() {},
    registration: { async showNotification(title, options) { shown.push(options); },
      async getNotifications() { return shown.map(n => ({data:n.data, close() { closed++; }})); } },
    clients: { async matchAll() { return clients; }, claim() {}, async openWindow() { opened++; } }
  };
  const context = vm.createContext({self, caches, URL, Response, console});
  vm.runInContext(fs.readFileSync(WORKER, 'utf8'), context);
  return async function fire(type, data) {
    let pending;
    events[type]({...data, waitUntil(value) { pending = value; }});
    await pending;
  };
}
async function bind(fire, uid) {
  let ack = false;
  await fire('message', {source:{url:'https://miru.test/app'}, data:{type:'PUSH_USER', user_id:uid},
    ports:[{postMessage() { ack = true; }}]});
  assert.equal(ack, true);
}
async function main() {
  let fire = worker();
  const push = uid => fire('push', {data:{json() { return {user_id:uid, body:'private', url:'/app'}; }}});
  await bind(fire, 'alice');
  clients = [{url:'https://miru.test/app', visibilityState:'visible'}];
  await push('alice'); assert.equal(shown.length, 0);
  clients = [{url:'https://miru.test/app', visibilityState:'hidden'}];
  await push('alice'); assert.equal(shown.length, 1);
  await bind(fire, 'bob'); assert.equal(closed, 1);
  await push('alice'); assert.equal(shown.length, 1);
  await push('bob'); assert.equal(shown.length, 2);
  // Restart the worker: account filtering survives without an open page.
  fire = worker(); clients = [];
  await push('alice'); assert.equal(shown.length, 2);
  await push('bob'); assert.equal(shown.length, 3);
  await fire('notificationclick', {notification:{data:{user_id:'alice', url:'/app'}, close() {}}});
  assert.equal(opened, 0);
  await bind(fire, '');
  await push('bob'); assert.equal(shown.length, 3);
  await push(undefined); assert.equal(shown.length, 3);
}
main().catch(e => {console.error(e); process.exitCode=1;});
""".replace("WORKER", json.dumps(str(worker)))
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_browser_rebinds_an_existing_subscription_and_clears_before_logout():
    html = (Path(__file__).resolve().parents[1] / "templates/index.html").read_text()
    start = html.index("function _setBrowserPushUser(")
    end = html.index("// ===== Emotion Ambiance", start)
    javascript = html[start:end]
    harness = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
let token = 'alice', messages = [], subscriptions = [], trace = [];
class MessageChannel {
  constructor() { this.port1 = {close() {}}; this.port2 = {peer:this.port1}; }
}
const active = {postMessage(data, ports) {
  messages.push(data.user_id); trace.push('worker:' + data.user_id);
  if (ports) ports[0].peer.onmessage({data:{ok:true}});
}};
const sub = {endpoint:'https://fcm.googleapis.com/fcm/send/browser',
  toJSON() { return {endpoint:this.endpoint, keys:{auth:'a', p256dh:'p'}}; },
  async unsubscribe() { trace.push('unsubscribe'); }};
const reg = {active, addEventListener() {}, pushManager:{async getSubscription() { return sub; }}};
const navigator = {serviceWorker:{controller:active, ready:Promise.resolve(reg),
  async register() { return reg; }, async getRegistration() { return reg; }, addEventListener() {}}};
const context = vm.createContext({window:{PushManager:function(){}}, navigator, MessageChannel,
  Notification:{permission:'granted'}, console, setTimeout, clearTimeout,
  _getAuthToken:() => token, _isClientMode:() => false,
  startVersionPolling() {}, startModelPolling() {},
  async fetch(url, options) {
    if (url === '/api/auth/me') return {ok:true, async json() { return {user_id:token}; }};
    if (url.startsWith('/api/push/binding?')) return {ok:true, async json() { return {revision:'version'}; }};
    if (url === '/api/push/subscribe') { subscriptions.push(JSON.parse(options.body)); return {ok:true,status:200}; }
    if (url === '/api/push/unsubscribe') { trace.push('server-unbind'); return {ok:true}; }
    throw new Error('unexpected fetch ' + url);
  }
});
vm.runInContext(SCRIPT, context);
(async () => {
  await context.initPWA();
  token = 'bob';
  await context.initPWA();
  assert.equal(subscriptions.length, 2);
  assert.equal(subscriptions[1].binding_version, 'version');
  assert.deepEqual(messages, ['', 'alice', '', 'bob']);
  await context._disconnectBrowserPush();
  assert.deepEqual(trace.slice(-3), ['worker:', 'server-unbind', 'unsubscribe']);
})().catch(e => {console.error(e); process.exitCode=1;});
""".replace("SCRIPT", json.dumps(javascript))
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
