# User isolation and private-instance settings

The bundled `soul.md` is a read-only template. A user without an active model
gets an editable persona at `data/users/<uid>/personas/_default/soul.md`.
Model-specific personas stay at `data/users/<uid>/personas/<model_id>/soul.md`.
Both the raw soul editor and the field editor write to these scoped files;
partial field edits preserve unexposed and custom Markdown sections. Existing
model-specific personas are retained. Historical edits to a shared template
cannot be attributed to a user automatically; review that template when
upgrading an installation which has already experienced the isolation bug.

On a shared server, the first user to open model settings **does not** become
the instance owner. Administrators continue to use `/api/admin/*` and the
existing administrator model configuration endpoints. To delegate instance
model settings to a specific active user, set:

```text
MIRU_INSTANCE_OWNER_USER_ID=u_your_existing_user_id
```

Restart the backend after changing this environment setting. The selected
user can configure the instance-wide vision/chat/memory providers; other
users cannot. An operator-selected ID takes precedence over a saved owner.
Existing saved owner assignments remain valid when no explicit ID is set;
audit `_admin/owner.json` when converting a private instance to shared use.
Deleting or suspending an owner does not let a different shared-server user
claim the instance.

Local single-device accounts retain access to their own model settings.
For a **private single-owner server**, an operator may explicitly opt into
the historical first-user claim/reclaim behavior:

```text
MIRU_ALLOW_OWNER_CLAIM=1
```

Do not enable this option on a shared service. The private-server installer
explicitly sets this option for its single-owner instance, including instances
created by the existing first-run wizard. Operators can override it with
`MIRU_ALLOW_OWNER_CLAIM=0` and/or select an explicit owner. Generic source and
Docker starts remain administrator-controlled by default; a previously
unassigned shared server no longer grants ownership from a GET request.

Chat appends and archiving now hold a per-file transaction across the entire
read/modify/write. Cooperative local processes use sidecar `.lock` files,
and replacements use unique temporary files. Do not remove live lock files.
These locks do not provide distributed storage, database transactions across
multiple files, or recovery for in-memory background queues. The current
single-backend lifecycle still applies; this is not multi-worker deployment
support.

Attention and morning proactive messages now invoke the existing Web Push
sender after writing chat history and pending notifications. Sending is
best effort, with a five-second timeout per subscription; a push outage does
not erase or regenerate the chat message. It does not add a durable push
retry queue or APNs/FCM native integration. Android's native SSE notification
channel remains unchanged. Test foreground/background behavior on real
devices before release.

User-supplied Web Push endpoints must use HTTPS on a supported browser push
service: Google FCM, Mozilla Web Push, Apple Web Push, or Windows notification
services. Unknown/custom providers require deliberate implementation and
validation, rather than allowing arbitrary server-side URLs. Existing
subscriptions to unrecognized destinations are skipped during sending.

Browser subscriptions are now registered on every account bootstrap. A
server-side, versioned endpoint binding allows only the current owner to send;
a stale registration cannot reclaim an endpoint after an account switch.
Legacy subscriptions remain silent until the updated app registers them.
The service worker separately persists a token-free user ID, clears it on
logout, closes old-account notifications, and drops queued pushes for another
user even when no page is open. It suppresses system notifications when the
chat page is visible. Expiry cleanup compares the sent subscription snapshot,
so an old-key failure cannot remove newly refreshed keys.

Attention delivery marks the intent delivered before external push calls and
serializes the generation with a local process lock. If a process stops after
the chat append but before that status write, the next attempt recovers the
existing message from current or archived history instead of generating it
again. This does not add durable retries for the external push itself.
