/* AION service worker — offline app shell.
 *
 * NETWORK-FIRST for the shell, with the cache as the offline fallback. It was
 * cache-first, which is the usual PWA advice and was wrong here in a way that
 * hid every frontend change ever shipped: `caches.match()` returned a hit, so
 * an installed HUD served hud.js/organic.js/hud.css from the version it first
 * saw, forever. The only invalidation was editing the CACHE constant by hand,
 * which nothing reminds you to do — so a reload after a deploy silently ran
 * the old code, and the "it works but the page disagrees" debugging that
 * follows is nasty.
 *
 * Cache-first exists to hide network latency. The daemon here is on loopback
 * or the LAN, so there is essentially none to hide, and the offline promise is
 * kept just as well by falling back to the cache when the fetch fails.
 *
 * /api/ and the SSE stream are never cached at all, in either direction.
 */
const CACHE = 'aion-v4';
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
    fetch(e.request)
      .then((r) => {
        // Only cache what actually arrived intact. Storing a 404 or an opaque
        // error response would make the offline fallback serve that instead.
        if (r && r.ok && r.type === 'basic') {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return r;
      })
      // Offline: whatever we last saw, then the shell for navigations so a
      // deep link still opens the app rather than the browser's error page.
      .catch(() => caches.match(e.request)
        .then((hit) => hit || caches.match('/index.html')))
  );
});
