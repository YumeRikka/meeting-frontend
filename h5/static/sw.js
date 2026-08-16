/* 会议创建 PWA · Service Worker
   作用：缓存应用壳（HTML/JS/CSS/图标），实现离线打开界面；
   所有 /api/ 请求始终走网络，保证建会/卡密实时有效。 */

const CACHE = "meeting-bot-v1";
const SHELL = [
  "./",
  "index.html",
  "config.js",
  "config.prod.js",
  "manifest.webmanifest",
  "icon-192.png",
  "icon-512.png",
  "sw.js"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  // API 永远走网络，不缓存
  if (url.pathname.startsWith("/api/")) return;

  // 应用壳：缓存优先，未命中再走网络并回填缓存
  event.respondWith(
    caches.match(req).then((cached) => {
      if (cached) return cached;
      return fetch(req).then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return resp;
      });
    })
  );
});
