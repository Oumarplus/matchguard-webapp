// Service worker minimal — juste ce qu'il faut pour que le navigateur
// propose "Ajouter à l'écran d'accueil". Pas de cache offline agressif,
// les données sportives doivent toujours être fraîches.
self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  // Passthrough réseau simple — pas de mise en cache des réponses API.
  event.respondWith(fetch(event.request));
});
