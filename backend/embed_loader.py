"""Embeddable chat loader served at GET /api/embed.js (spec 016).

Adapted from BitBotPro: the legacy loader (``aa55976^:backend/server.py`` ``serve_embed_script``)
for features, and the hardened Spec 012 loader (``origin/bitbotpro-v1.0.0:backend/api/
webchat_assets.py`` ``EMBED_JS_TEMPLATE``) for safety:
- no server-side interpolation at all: the script is static, so nothing from a request can be
  reflected into it;
- the widget origin is the origin of the script's own URL (``/api/embed.js`` and ``/widget`` are
  served from the same site);
- values only reach the page through DOM APIs and ``encodeURIComponent``;
- configuration is fetched asynchronously (the legacy loader used a synchronous XHR);
- the iframe is sandboxed.
"""

THEME_KEYS = ("accent", "accentLight", "bg", "surface", "panel", "text", "textSecondary", "border",
              "userBubble", "botBubble", "inputBg", "font", "radius")

EMBED_JS = r"""(function () {
  "use strict";
  var script = document.currentScript || document.querySelector('script[src*="/api/embed.js"]');
  if (!script) return;
  var ORIGIN;
  try { ORIGIN = new URL(script.src, document.baseURI).origin; } catch (e) { return; }
  function attr(n) { return script.getAttribute("data-" + n) || ""; }
  var ID = attr("instance");
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(ID)) { if (window.console) console.warn("BridgeBot: missing or invalid data-instance"); return; }
  var MODE = attr("mode"); if (["popup", "inline", "fullpage"].indexOf(MODE) < 0) MODE = "popup";
  var SIDE = attr("position") === "bottom-left" ? "left" : "right";
  var THEME = __THEME_KEYS__;
  var q = "instance=" + encodeURIComponent(ID) + "&mode=" + encodeURIComponent(MODE);
  THEME.forEach(function (k) {
    var v = attr(k.replace(/[A-Z]/g, function (m) { return "-" + m.toLowerCase(); })) || attr(k);
    if (v) q += "&" + k + "=" + encodeURIComponent(v.slice(0, 120));
  });
  var welcome = attr("welcome"); if (welcome) q += "&welcome=" + encodeURIComponent(welcome.slice(0, 2000));
  var src = ORIGIN + "/widget?" + q;
  var HEX = /^#[0-9a-fA-F]{3,8}$/;
  function frame(w, h) {
    var f = document.createElement("iframe");
    f.src = src; f.title = "BridgeBot Chat";
    f.setAttribute("sandbox", "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox");
    f.setAttribute("allow", "clipboard-write"); f.setAttribute("referrerpolicy", "strict-origin");
    f.style.border = "0"; f.style.width = w; f.style.height = h; f.style.display = "block";
    return f;
  }
  if (MODE === "inline") {
    var target = document.getElementById("bridgebot-container") || document.getElementById("bitbotpro-container") ||
                 document.querySelector("[data-bridgebot],[data-bitbotpro]");
    if (!target) { if (window.console) console.warn("BridgeBot: no #bridgebot-container found"); return; }
    var inl = frame("100%", "100%"); inl.style.minHeight = "500px"; inl.setAttribute("data-testid", "bridgebot-inline-iframe");
    while (target.firstChild) target.removeChild(target.firstChild);
    target.appendChild(inl);
    return;
  }
  if (MODE === "fullpage") {
    var full = frame("100%", "100%");
    full.style.position = "fixed"; full.style.top = "0"; full.style.left = "0"; full.style.zIndex = "2147483647";
    full.setAttribute("data-testid", "bridgebot-fullpage-iframe");
    document.body.appendChild(full);
    return;
  }
  var accent = HEX.test(attr("accent")) ? attr("accent") : "#3B82F6";
  var NS = "http://www.w3.org/2000/svg";
  function icon(paths, size) {
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("width", size); svg.setAttribute("height", size); svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none"); svg.setAttribute("stroke", "white"); svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round");
    paths.forEach(function (d) { var p = document.createElementNS(NS, "path"); p.setAttribute("d", d); svg.appendChild(p); });
    return svg;
  }
  var chatIcon = icon(["M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"], "24");
  var closeIcon = icon(["M18 6L6 18", "M6 6l12 12"], "20");
  var btn = document.createElement("button");
  btn.type = "button"; btn.setAttribute("aria-label", "Open chat"); btn.setAttribute("aria-expanded", "false");
  btn.setAttribute("data-testid", "bridgebot-popup-trigger");
  btn.style.cssText = "position:fixed;bottom:20px;width:56px;height:56px;border-radius:50%;border:0;cursor:pointer;" +
    "z-index:2147483646;padding:0;display:flex;align-items:center;justify-content:center;" +
    "box-shadow:0 4px 20px rgba(0,0,0,.25);transition:transform .2s ease";
  btn.style[SIDE] = "20px"; btn.style.background = accent;
  btn.appendChild(chatIcon);
  var panel = document.createElement("div");
  panel.setAttribute("data-testid", "bridgebot-popup-panel");
  panel.style.cssText = "position:fixed;bottom:88px;width:380px;max-width:calc(100vw - 32px);height:580px;" +
    "max-height:calc(100vh - 120px);display:none;z-index:2147483647;border-radius:16px;overflow:hidden;" +
    "box-shadow:0 12px 48px rgba(0,0,0,.3)";
  panel.style[SIDE] = "20px";
  var pf = frame("100%", "100%"); pf.setAttribute("data-testid", "bridgebot-popup-iframe");
  panel.appendChild(pf);
  var open = false;
  function toggle(v) {
    open = (v === undefined) ? !open : v;
    panel.style.display = open ? "block" : "none";
    btn.setAttribute("aria-expanded", String(open)); btn.setAttribute("aria-label", open ? "Close chat" : "Open chat");
    while (btn.firstChild) btn.removeChild(btn.firstChild);
    btn.appendChild(open ? closeIcon : chatIcon);
  }
  btn.addEventListener("click", function () { toggle(); });
  btn.addEventListener("mouseenter", function () { btn.style.transform = "scale(1.08)"; });
  btn.addEventListener("mouseleave", function () { btn.style.transform = "scale(1)"; });
  document.body.appendChild(panel); document.body.appendChild(btn);
  var openAttr = attr("open");
  if (openAttr === "true") { toggle(true); return; }
  if (openAttr === "false") return;
  fetch(ORIGIN + "/api/embed/config/" + encodeURIComponent(ID), {credentials: "omit"})
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (cfg) { if (cfg && cfg.default_open === true) toggle(true); })
    .catch(function () {});
})();
"""

import json as _json

EMBED_JS = EMBED_JS.replace("__THEME_KEYS__", _json.dumps(list(THEME_KEYS)))
