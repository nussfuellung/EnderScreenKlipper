// S1 Mobile – Service Worker: App-Hülle offline verfügbar, alles Live-Daten bleibt Netzwerk
const C = 's1m-v1';
const SHELL = ['/', '/manifest.webmanifest', '/icons/icon-192.png', '/icons/icon-512.png'];
self.addEventListener('install', e => { e.waitUntil(caches.open(C).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== C).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.origin !== location.origin) return;
  if (/^\/(api|server|printer|machine|access|websocket|webcam|ha)\b/.test(u.pathname)) return;
  // Netzwerk zuerst (immer aktuelle Version), Cache als Fallback
  e.respondWith(fetch(e.request).then(r => { if (r.ok && (u.pathname === '/' || SHELL.includes(u.pathname))) { const c = r.clone(); caches.open(C).then(x => x.put(u.pathname === '/' ? '/' : e.request, c)); } return r; })
    .catch(() => caches.match(u.pathname.startsWith('/icons') ? e.request : (SHELL.includes(u.pathname) ? e.request : '/'))));
});
self.addEventListener('notificationclick', e => { e.notification.close(); e.waitUntil(clients.matchAll({type: 'window'}).then(ws => ws.length ? ws[0].focus() : clients.openWindow('/?tab=printer'))); });
