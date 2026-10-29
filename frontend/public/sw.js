/**
 * WebsiteLead Hunter - Service Worker
 * Version: websitelead-shell-v1
 *
 * Strict Security Mandate:
 * - Caches ONLY static application shell and approved public assets.
 * - NEVER caches authenticated API responses (/api/*).
 * - NEVER stores session tokens, CSRF tokens, passwords, or lead data in Cache Storage.
 * - Safe navigation fallback to pre-cached index.html shell when offline.
 */

const CACHE_NAME = 'websitelead-shell-v1';

// Static assets allowlist for application shell
const STATIC_SHELL_URLS = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/favicon.svg',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/icon-maskable.png',
  '/icons/icon-192.svg',
  '/icons/icon-512.svg',
];

// Install: pre-cache static shell assets
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(STATIC_SHELL_URLS);
    }).then(() => self.skipWaiting())
  );
});

// Activate: clean up obsolete cache versions and claim clients
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            console.log('[SW] Deleting obsolete cache:', key);
            return caches.delete(key);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

// Fetch: intercept requests with strict private-data exclusion
self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);

  // 1. NEVER intercept or cache non-GET requests (mutations)
  if (request.method !== 'GET') {
    return;
  }

  // 2. NEVER cache authenticated API responses or operational endpoints
  // This is a strict security boundary: private lead data, audit reports,
  // pitches, and session tokens must never enter shared Cache Storage.
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(request).catch(() => {
        // Return JSON offline error for API calls rather than caching private data
        return new Response(
          JSON.stringify({
            detail: 'Backend unreachable. You are currently offline.',
            offline: true,
          }),
          {
            status: 503,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      })
    );
    return;
  }

  // 3. Navigation requests (HTML shell)
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => {
        return caches.match('/index.html').then((cached) => {
          return cached || fetch(request);
        });
      })
    );
    return;
  }

  // 4. Static assets (JS, CSS, images, icons)
  // Use Stale-While-Revalidate for Vite assets and icons
  if (
    url.origin === self.location.origin &&
    (url.pathname.startsWith('/assets/') ||
     url.pathname.startsWith('/icons/') ||
     url.pathname === '/favicon.svg' ||
     url.pathname === '/manifest.webmanifest')
  ) {
    event.respondWith(
      caches.match(request).then((cachedResponse) => {
        const fetchPromise = fetch(request).then((networkResponse) => {
          if (networkResponse && networkResponse.status === 200) {
            const responseToCache = networkResponse.clone();
            caches.open(CACHE_NAME).then((cache) => {
              cache.put(request, responseToCache);
            });
          }
          return networkResponse;
        }).catch(() => cachedResponse);

        return cachedResponse || fetchPromise;
      })
    );
    return;
  }

  // Default: network with cache fallback for same-origin static files
  event.respondWith(
    caches.match(request).then((cached) => {
      return cached || fetch(request);
    })
  );
});

// Listen for message events (e.g. skip waiting when update is approved)
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

// Background Sync handler if supported
self.addEventListener('sync', (event) => {
  if (event.tag === 'sync-offline-leads') {
    event.waitUntil(
      self.clients.matchAll().then((clients) => {
        clients.forEach((client) => {
          client.postMessage({ type: 'TRIGGER_OFFLINE_SYNC' });
        });
      })
    );
  }
});
