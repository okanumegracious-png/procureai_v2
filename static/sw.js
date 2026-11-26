const CACHE_NAME = "procureai-shell-v6";
const APP_SHELL = [
  "/",
  "/manifest.webmanifest",
  "/static/styles.css?v=20261126-quotation-fix",
  "/static/app.js?v=20261126-quotation-fix",
  "/static/auth.js",
  "/static/job-completion.js",
  "/static/procureai-icon.svg"
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;

  // Never cache authenticated API responses or user-specific procurement records.
  if (url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).then(response => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put("/", copy));
        }
        return response;
      }).catch(() => caches.match("/"))
    );
    return;
  }

  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    // Prefer the deployed asset on every load; use the cached shell only when offline.
    event.respondWith(
      fetch(request).then(response => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
        }
        return response;
      }).catch(() => caches.match(request))
    );
  }
});
