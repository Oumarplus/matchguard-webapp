// Service worker minimal pour MatchGuard — assure l'installabilité PWA
// (icône sur l'écran d'accueil) et met en cache les fichiers statiques de
// base pour un chargement plus rapide. Les appels /api/* ne sont jamais mis
// en cache : les données (pronostics, cotes, live...) doivent toujours être
// fraîches, sinon l'appli afficherait des résultats périmés hors-ligne.

const CACHE_NAME = "matchguard-static-v1";
const STATIC_ASSETS = [
  "/",
  "/static/app.js",
  "/static/style.css",
  "/static/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names
          .filter((name) => name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Jamais de cache pour les appels API : les données doivent rester fraîches.
  if (url.pathname.startsWith("/api/")) {
    return;
  }

  // Stratégie "cache d'abord, réseau en repli" pour les fichiers statiques.
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).catch(() => cached);
    })
  );
});
