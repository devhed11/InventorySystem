const CACHE = 'holpers-inv-v6';

// Pre-cache the login page on install
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(['/login'])).then(() => self.skipWaiting())
  );
});

// Remove old caches on activate (clears any poisoned redirect entries from v3/v4)
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Fetch strategy:
//   - API calls, POSTs, and page NAVIGATIONS  → never intercept (let the
//     browser hit the network directly). Navigations must pass through so the
//     server's auth redirects (302 → /login or /r) are followed natively.
//     Intercepting them made the SW return a redirected response, which throws:
//       "a redirected response was used for a request whose redirect mode is not 'follow'"
//   - Everything else (static assets) → serve cache, revalidate in background,
//     and never cache a redirected/opaque response.
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  if (req.method !== 'GET' || req.mode === 'navigate' || url.pathname.startsWith('/api/')) return;

  event.respondWith(
    caches.open(CACHE).then(cache =>
      cache.match(req).then(cached => {
        const networkFetch = fetch(req).then(response => {
          if (response.ok && !response.redirected && response.type === 'basic') {
            cache.put(req, response.clone());
          }
          return response;
        }).catch(() => cached); // offline: fall back to cache
        return cached || networkFetch;
      })
    )
  );
});
