/* Talking to Campfire when a page is embedded (sidebar page, panel, drawer): adopt Campfire's light/dark theme,
   size the frame to the page, and show confirmations as Campfire toasts. Messages go only to Campfire's origin
   and are accepted only from it (the parent window). Standalone pages skip all of this. */
(function () {
  if (window.parent === window) return;
  var origin = document.body.getAttribute("data-campfire-origin");
  if (!origin) return;

  function post(type, payload, requestId) {
    var msg = { source: "campfire-app-bridge", version: 1, type: type, payload: payload || {} };
    if (requestId) msg.requestId = requestId;
    window.parent.postMessage(msg, origin);
  }

  window.addEventListener("message", function (e) {
    if (e.origin !== origin || e.source !== window.parent) return;
    var m = e.data || {};
    if (m.source === "campfire-host" && m.type === "context" && m.payload && m.payload.theme) {
      document.documentElement.setAttribute("data-theme", m.payload.theme);
      try { sessionStorage.setItem("fo-theme", m.payload.theme); } catch (_) {}
    }
  });
  post("ready", {}, "fo-ready-" + Date.now());

  // Fit the frame to the page (Campfire allows 120 to 2,000 px; longer pages scroll inside).
  var last = 0;
  function fit() {
    var h = Math.min(2000, Math.max(120, Math.ceil(document.documentElement.scrollHeight) + 2));
    if (Math.abs(h - last) > 2) { last = h; post("resize", { height: h }); }
  }
  if (window.ResizeObserver) new ResizeObserver(fit).observe(document.body);
  window.addEventListener("load", fit);
  fit();

  // Confirmations of the action just taken also appear as Campfire toasts.
  var toasts = document.querySelectorAll("[data-toast]");
  for (var i = 0; i < toasts.length; i++) {
    var text = (toasts[i].textContent || "").replace(/\s+/g, " ").trim();
    if (text) post("toast", { level: toasts[i].getAttribute("data-toast") || "success", message: text.slice(0, 200) });
  }
})();
