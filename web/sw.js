/* Service worker: caches the dashboard shell so the PWA installs properly and
 * opens instantly. Live data (SSE) and the record/replay API are NEVER
 * intercepted — only same-origin GETs for static shell files are handled,
 * network-first so dashboard updates always win when the server is reachable. */
const CACHE = 'fh6-shell-v1';
const SHELL = ['/', '/index.html', '/manifest.json', '/icon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.origin !== location.origin) return;
  if (url.pathname === '/stream' || url.pathname.startsWith('/record') ||
      url.pathname.startsWith('/replay') || url.pathname === '/recordings') return;
  e.respondWith(
    fetch(e.request).then(resp => {
      const copy = resp.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy));
      return resp;
    }).catch(() => caches.match(e.request, {ignoreSearch: true}))
  );
});
