// Service worker сайта команды: показывает уведомления (Web Push) и открывает приложение по нажатию.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { title: event.data && event.data.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || "Команда", {
    body: data.body || "",
    icon: "/api/app?asset=icon-192.png",
    badge: "/api/app?asset=icon-192.png",
    data: { url: data.url || "/app" },
    tag: data.tag || undefined,
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || "/app", self.location.origin).href;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) {
      if (w.url.startsWith(self.location.origin)) { await w.focus(); try { await w.navigate(url); } catch (e) {} return; }
    }
    await self.clients.openWindow(url);
  })());
});
