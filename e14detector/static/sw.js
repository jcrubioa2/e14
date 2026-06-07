/* Service worker for the E-14 veeduría PWA.
 *
 * Design goal: make the app installable and instant-loading WITHOUT ever serving
 * stale election data. The data here (reports, votes, the national feed) is live
 * and crowd-sourced, so the strategy is deliberately conservative:
 *
 *   - navigations            -> NETWORK-FIRST, fall back to a cached offline page
 *                               only when truly offline (never a stale snapshot).
 *   - /static/ shell + icons -> stale-while-revalidate (cheap, versioned assets).
 *   - Google Fonts           -> cache-first (immutable, big win on repeat loads).
 *   - /api, /crop, /geo, /c  -> NOT intercepted -> always straight to network.
 *   - anything non-GET       -> NOT intercepted -> votes/reports never touched.
 *
 * Bump CACHE_VERSION to roll all caches on the next visit.
 */
const CACHE_VERSION = 'e14-v1';
const SHELL_CACHE = `${CACHE_VERSION}-shell`;
const FONT_CACHE = `${CACHE_VERSION}-fonts`;
const OFFLINE_URL = '/static/offline.html';

const PRECACHE = [
  OFFLINE_URL,
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon-maskable-512.png',
  '/static/icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((c) => c.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => !k.startsWith(CACHE_VERSION)).map((k) => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

// Let the page tell a freshly-installed SW to take over immediately.
self.addEventListener('message', (event) => {
  if (event.data === 'skipWaiting') self.skipWaiting();
});

function isFontRequest(url) {
  return url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com';
}

// Paths whose responses are live/dynamic and must never be served from cache.
function isLivePath(path) {
  return path.startsWith('/api/') || path.startsWith('/crop') ||
         path.startsWith('/geo/') || path.startsWith('/c/') ||
         path.startsWith('/admin') || path === '/health';
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;                 // votes/reports → untouched
  const url = new URL(req.url);

  // Google Fonts: immutable → cache-first.
  if (isFontRequest(url)) {
    event.respondWith(
      caches.open(FONT_CACHE).then((cache) =>
        cache.match(req).then((hit) => hit || fetch(req).then((res) => {
          if (res.ok) cache.put(req, res.clone());
          return res;
        }).catch(() => hit))
      )
    );
    return;
  }

  if (url.origin !== self.location.origin) return;  // other third parties → untouched
  if (isLivePath(url.pathname)) return;             // live data → straight to network

  // Static shell (icons, css, js, manifest): stale-while-revalidate.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.open(SHELL_CACHE).then((cache) =>
        cache.match(req).then((hit) => {
          const net = fetch(req).then((res) => {
            if (res.ok) cache.put(req, res.clone());
            return res;
          }).catch(() => hit);
          return hit || net;
        })
      )
    );
    return;
  }

  // Page navigations: network-first, offline page only as a last resort.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).catch(() => caches.match(OFFLINE_URL))
    );
    return;
  }
});
