// VERSION is checked by the browser — any change triggers SW update
const APP_VERSION = '__BUILD_VERSION__';
const CACHE_NAME = 'contextlife-' + APP_VERSION;
const PUSH_BINDING_CACHE = 'miru-push-binding-v1';
const PUSH_BINDING_URL = '/__miru_push_user__';
let pushStateWrites = Promise.resolve();
let pushUserEpoch = 0;

async function readPushUserFromCache() {
  const cache = await caches.open(PUSH_BINDING_CACHE);
  const response = await cache.match(PUSH_BINDING_URL);
  return response ? (await response.json()).user_id : '';
}

async function readPushUser() {
  await pushStateWrites;
  return readPushUserFromCache();
}

function queuePushState(task) {
  const result = pushStateWrites.catch(() => {}).then(task);
  pushStateWrites = result;
  return result;
}

function writePushUser(userId) {
  // Invalidate in-flight push handlers immediately, before cache I/O yields.
  pushUserEpoch++;
  return queuePushState(async () => {
    const cache = await caches.open(PUSH_BINDING_CACHE);
    await cache.put(PUSH_BINDING_URL, new Response(JSON.stringify({ user_id: userId })));
    const notifications = await self.registration.getNotifications();
    notifications.forEach(n => {
      if (!userId || n.data?.user_id !== userId) n.close();
    });
  });
}

self.addEventListener('message', event => {
  if (event.data?.type !== 'PUSH_USER') return;
  if (!event.source || new URL(event.source.url).origin !== self.location.origin) return;
  const userId = typeof event.data.user_id === 'string' ? event.data.user_id : '';
  event.waitUntil(writePushUser(userId).then(() => {
    if (event.ports?.[0]) event.ports[0].postMessage({ ok: true });
  }));
});
const STATIC_ASSETS = [
  '/assets/character-avatar.png',
  '/icon-192.png',
  '/icon-512.png',
];

// Install: pre-cache static assets only (NOT the HTML page)
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

// Activate: clean old caches + notify clients only on real update
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      const oldKeys = keys.filter((k) => k !== CACHE_NAME && k !== PUSH_BINDING_CACHE);
      return Promise.all(oldKeys.map((k) => caches.delete(k))).then(() => {
        // Only notify clients if old caches were replaced (= real version update)
        if (oldKeys.length > 0) {
          return self.clients.matchAll({ type: 'window' }).then((clients) => {
            clients.forEach((client) => {
              client.postMessage({ type: 'NEW_VERSION', version: APP_VERSION });
            });
          });
        }
      });
    })
  );
  self.clients.claim();
});

// Fetch strategy:
//   - API / POST: passthrough (no cache)
//   - Navigation (HTML): network-first, fallback to cache (always fresh when online)
//   - Static assets: cache-first, background refresh (fast loads)
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // API calls and POST requests: always network
  if (url.pathname.startsWith('/api/') || event.request.method !== 'GET') {
    return;
  }

  // Navigation requests (HTML pages): network-first
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).then((response) => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => caches.match(event.request))
    );
    return;
  }

  // Static assets: cache-first, background refresh
  event.respondWith(
    caches.match(event.request).then((cached) => {
      const fetchPromise = fetch(event.request).then((response) => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => cached);

      return cached || fetchPromise;
    })
  );
});

// Push notification received
self.addEventListener('push', (event) => {
  let data = { title: 'Miru 提醒', body: '你有一条新提醒', url: '/' };
  try {
    data = event.data.json();
  } catch (e) {
    data.body = event.data ? event.data.text() : data.body;
  }

  const bindingEpoch = pushUserEpoch;
  event.waitUntil(queuePushState(async () => {
    // A queued push can arrive after logout or a different account logs in.
    // Fail closed for old/unbound payloads, including legacy subscriptions.
    if (!data.user_id || bindingEpoch !== pushUserEpoch
        || data.user_id !== await readPushUserFromCache()) return;
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    if (bindingEpoch !== pushUserEpoch || data.user_id !== await readPushUserFromCache()) return;
    if (bindingEpoch !== pushUserEpoch) return;
    if (clients.some(client => client.visibilityState === 'visible'
        && new URL(client.url).pathname === '/app')) return;
    await self.registration.showNotification(data.title || 'Miru 提醒', {
      body: data.body || '',
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      data: { url: data.url || '/app', user_id: data.user_id },
      vibrate: [200, 100, 200],
      tag: data.tag || 'miru-reminder',
      renotify: true,
    });
    // If logout raced the browser's own asynchronous display, close the
    // late notification too, rather than relying on the earlier cleanup.
    if (bindingEpoch !== pushUserEpoch) {
      const notifications = await self.registration.getNotifications();
      notifications.forEach(n => {
        if (n.data?.user_id === data.user_id) n.close();
      });
    }
  }));
});

// Notification click: open/focus the app
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = event.notification.data?.url || '/';

  event.waitUntil(
    readPushUser().then(userId => {
      if (!userId || event.notification.data?.user_id !== userId) return null;
      return self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    }).then((clients) => {
      if (!clients) return;
      for (const client of clients) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          return client.focus();
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
