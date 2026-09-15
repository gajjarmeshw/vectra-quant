/* VECTRA_QUANT service worker.
   Shell is cached; /state is network-first because stale trading state is worse
   than no trading state. Push notifications land here. */

const SHELL = 'vectra_quant-shell-v2';
const SHELL_FILES = ['/', '/index.html', '/manifest.webmanifest'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;

  // Never serve trading data from cache.
  if (url.pathname.startsWith('/state') || url.pathname.startsWith('/report')) {
    e.respondWith(fetch(e.request).catch(() => new Response('{"offline":true}', {
      headers: { 'Content-Type': 'application/json' },
    })));
    return;
  }

  if (url.origin !== self.location.origin) return;

  // The HTML shell must be network-first. Cache-first pinned an installed home-screen
  // app to whatever index.html it first saw, so it kept loading the old hashed bundle
  // and never picked up a redeploy. Vite's /assets/ filenames are content-hashed, so
  // those stay cache-first and cost nothing to keep.
  const isShell =
    e.request.mode === 'navigate' ||
    url.pathname === '/' ||
    url.pathname === '/index.html' ||
    url.pathname === '/manifest.webmanifest' ||
    url.pathname === '/sw.js';

  if (isShell) {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(SHELL).then((c) => c.put(e.request, copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match(e.request).then((hit) => hit || caches.match('/index.html'))),
    );
    return;
  }

  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request).then((res) => {
      const copy = res.clone();
      caches.open(SHELL).then((c) => c.put(e.request, copy)).catch(() => {});
      return res;
    })),
  );
});

self.addEventListener('push', (e) => {
  let data = { title: 'VECTRA_QUANT', body: 'Update', kind: 'SYSTEM' };
  try {
    data = e.data ? e.data.json() : data;
  } catch (_) {
    /* keep the default */
  }
  const urgent = data.urgent || ['SUGGESTION', 'GUARDIAN', 'SYSTEM'].includes(data.kind);
  e.waitUntil(
    self.registration.showNotification(data.title || 'VECTRA_QUANT', {
      body: data.body || '',
      tag: data.kind || 'SYSTEM',
      renotify: urgent,
      requireInteraction: urgent,
      badge: '/icon-192.png',
      icon: '/icon-192.png',
      data: data.data || {},
    }),
  );
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const c of list) if ('focus' in c) return c.focus();
      return self.clients.openWindow('/');
    }),
  );
});
