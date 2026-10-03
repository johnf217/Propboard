/* Prop Board offline support.
   - The app itself opens instantly from the saved copy and quietly saves the newest version in the background.
   - Data files (*.json) always try the network first; with no signal they fall back to the last saved copy,
     marked with an X-PB-Saved header so the app can say it's offline. */
const CACHE = "pb-v1";
const SHELL = ["./", "index.html", "manifest.webmanifest", "apple-touch-icon.png", "icon-192.png", "icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;   // headshots, logos: browser handles them
  if (url.searchParams.has("check")) return;                            // update checks go straight to the network
  if (url.pathname.endsWith(".json")) { e.respondWith(dataFirst(req, url)); return; }
  if (req.mode === "navigate" || url.pathname.endsWith("/") || url.pathname.endsWith("index.html")) { e.respondWith(appFirst()); return; }
  e.respondWith(caches.match(req).then(r => r || fetch(req)));
});

async function appFirst() {
  const cache = await caches.open(CACHE);
  const saved = await cache.match("index.html");
  const fresh = fetch("index.html", {cache: "no-store"})
    .then(r => { if (r.ok) cache.put("index.html", r.clone()); return r; }).catch(() => null);
  return saved || (await fresh) || new Response("Offline and nothing saved yet.", {status: 503});
}

function withTimeout(p, ms) { return Promise.race([p, new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), ms))]); }

async function dataFirst(req, url) {
  const cache = await caches.open(CACHE), key = url.pathname;
  try {
    const r = await withTimeout(fetch(req, {cache: "no-store"}), 8000);
    if (r.ok) { cache.put(key, r.clone()); return r; }
    if (r.status === 404) return r;                                      // file doesn't exist yet: not an offline case
    throw new Error("bad status");
  } catch (err) {
    const saved = await cache.match(key);
    if (!saved) return new Response("", {status: 504});
    const h = new Headers(saved.headers); h.set("X-PB-Saved", "1");
    return new Response(await saved.blob(), {status: 200, headers: h});
  }
}
