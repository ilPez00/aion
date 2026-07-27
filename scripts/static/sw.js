/* AION service worker — offline app shell.
 *
 * Cache-first for the static shell (so the HUD opens with no network), but
 * NEVER touch /api/ or the SSE stream — those must always hit the live daemon.
 */
const CACHE = 'aion-v3';
const SHELL = ['/', '/index.html', '/manifest.webmanifest', '/icon.svg',
               '/static/hud.css', '/static/organic.js', '/static/hud.js'];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // live data must never be served from cache
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;
  e.respondWith(
    caches.match(e.request).then((hit) =>
      hit ||
      fetch(e.request)
        .then((r) => {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
          return r;
        })
        .catch(() => caches.match('/index.html'))
    )
  );
});
