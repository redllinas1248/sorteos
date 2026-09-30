const CACHE_NAME = 'sorteos-v1';

self.addEventListener('install', function(event) {
    self.skipWaiting();
});

self.addEventListener('activate', function(event) {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', function(event) {
    // Modo minimalista: pasa todo a la red.
    // No cachea para no romper contenido dinámico.
});