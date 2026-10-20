// Service worker: показ Web Push уведомлений и переход по клику.
self.addEventListener("push", (event) => {
  let data = { title: "Клинический компас", body: "Новое уведомление" };
  if (event.data) {
    try {
      data = { title: "Клинический компас", ...JSON.parse(event.data.text()) };
    } catch {
      data.body = event.data.text();
    }
  }
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      tag: "compass",
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if ("focus" in w) return w.focus();
      }
      return self.clients.openWindow("/");
    })
  );
});