"""Static Webchat assets served by the API (Phase 4B, parity W1–W3).

* ``EMBED_JS``: the loader customers paste (``<script src=".../api/embed.js"
  data-instance="pk_...">``). Modes popup/inline/fullpage, position, the 13
  legacy theme attributes, welcome, open, and a launcher with the bot logo.
  The configuration is fetched asynchronously (no sync XHR). Values never
  touch HTML: DOM APIs and ``encodeURIComponent`` only.
* ``WIDGET_JS`` / ``WIDGET_CSS``: the chat UI inside the iframe, served from
  the API origin. No inline scripts, ``textContent`` only, theme values
  validated before use, and the session kept in browser storage per public
  id.
* The widget HTML is rendered per request (``render_widget_html``) with an
  escaped public id and optional https custom CSS link. The response carries a
  CSP whose ``frame-ancestors`` is the instance's effective allowed origins.
"""
import html
import json

EMBED_JS_TEMPLATE = r"""(function () {
  "use strict";
  var API = __API_ORIGIN__;
  var script = document.currentScript || document.querySelector('script[src*="embed.js"]');
  function attr(n) { return (script && script.getAttribute("data-" + n)) || ""; }
  var PID = attr("instance") || attr("public-id");
  if (!/^pk_[A-Za-z0-9_-]{22,64}$/.test(PID)) { if (window.console) console.warn("BitBotPro: invalid data-instance"); return; }
  var MODE = attr("mode"); if (["popup", "inline", "fullpage"].indexOf(MODE) < 0) MODE = "popup";
  var POS = attr("position") === "bottom-left" ? "bottom-left" : "bottom-right";
  var THEME = {accent: "accent", accentLight: "accent_light", bg: "background", surface: "surface", panel: "panel",
               text: "text", textSecondary: "text_secondary", border: "border", userBubble: "user_bubble",
               botBubble: "bot_bubble", inputBg: "input_background", font: "font", radius: "radius"};
  var q = "mode=" + encodeURIComponent(MODE);
  Object.keys(THEME).forEach(function (k) { var v = attr(k); if (v) q += "&t_" + THEME[k] + "=" + encodeURIComponent(v.slice(0, 80)); });
  var welcome = attr("welcome"); if (welcome) q += "&welcome=" + encodeURIComponent(welcome.slice(0, 2000));
  var src = API + "/api/public/widget/" + encodeURIComponent(PID) + "?" + q;
  function frame(w, h) {
    var f = document.createElement("iframe");
    f.src = src; f.title = "Chat";
    f.setAttribute("sandbox", "allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox");
    f.setAttribute("allow", "clipboard-write"); f.setAttribute("referrerpolicy", "strict-origin");
    f.style.border = "0"; f.style.width = w; f.style.height = h;
    return f;
  }
  if (MODE === "inline") {
    var target = document.getElementById("bitbotpro-container") || document.querySelector("[data-bitbotpro]");
    if (!target) { if (window.console) console.warn("BitBotPro: no #bitbotpro-container found"); return; }
    target.appendChild(frame("100%", "600px"));
    return;
  }
  if (MODE === "fullpage") {
    var full = frame("100vw", "100vh");
    full.style.position = "fixed"; full.style.inset = "0"; full.style.zIndex = "2147483647";
    document.body.appendChild(full);
    return;
  }
  var side = POS === "bottom-left" ? "left" : "right";
  var btn = document.createElement("button");
  btn.type = "button"; btn.setAttribute("aria-label", "Open chat");
  btn.style.cssText = "position:fixed;bottom:20px;width:56px;height:56px;border-radius:50%;border:0;cursor:pointer;z-index:2147483646;background:#00F5FF;padding:0;overflow:hidden";
  btn.style[side] = "20px";
  var panel = document.createElement("div");
  panel.style.cssText = "position:fixed;bottom:88px;width:380px;max-width:calc(100vw - 32px);height:600px;max-height:calc(100vh - 120px);display:none;z-index:2147483647;box-shadow:0 8px 32px rgba(0,0,0,.35);border-radius:12px;overflow:hidden";
  panel.style[side] = "20px";
  panel.appendChild(frame("100%", "100%"));
  var open = false;
  function toggle(v) { open = (v === undefined) ? !open : v; panel.style.display = open ? "block" : "none"; btn.setAttribute("aria-expanded", String(open)); }
  btn.addEventListener("click", function () { toggle(); });
  document.body.appendChild(panel); document.body.appendChild(btn);
  var HEX = /^#[0-9a-fA-F]{3,8}$/;
  if (HEX.test(attr("accent"))) btn.style.background = attr("accent");
  fetch(API + "/api/public/instances/" + encodeURIComponent(PID), {credentials: "omit"})
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (cfg) {
      if (!cfg) return;
      if (!attr("accent") && cfg.theme && HEX.test(cfg.theme.accent || "")) btn.style.background = cfg.theme.accent;
      if (cfg.logo_url) { var img = document.createElement("img"); img.alt = ""; img.src = API + cfg.logo_url; img.style.cssText = "width:100%;height:100%;object-fit:cover"; btn.appendChild(img); }
      var openAttr = attr("open");
      if (openAttr === "true" || (openAttr !== "false" && cfg.default_open)) toggle(true);
    })
    .catch(function () {});
})();
"""

WIDGET_CSS = r""":root{--bbp-accent:#00F5FF;--bbp-accent_light:#7DF9FF;--bbp-background:#030712;--bbp-surface:#050B14;--bbp-panel:#0B132B;--bbp-text:#E5E7EB;--bbp-text_secondary:#9CA3AF;--bbp-border:#1F2937;--bbp-user_bubble:#0088FF;--bbp-bot_bubble:#0B132B;--bbp-input_background:#050B14;--bbp-radius:12px;--bbp-font:'IBM Plex Sans',system-ui,sans-serif}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bbp-background);color:var(--bbp-text);font-family:var(--bbp-font)}
.bbp{display:flex;flex-direction:column;height:100%}
.bbp-header{display:flex;align-items:center;gap:10px;padding:12px 16px;background:var(--bbp-panel);border-bottom:1px solid var(--bbp-border)}
.bbp-header img{width:32px;height:32px;border-radius:50%;object-fit:cover}
.bbp-title{font-weight:600}
.bbp-messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px}
.bbp-msg{max-width:85%;padding:10px 14px;border-radius:var(--bbp-radius);white-space:pre-wrap;word-wrap:break-word;line-height:1.45}
.bbp-user{align-self:flex-end;background:var(--bbp-user_bubble);color:#fff}
.bbp-bot{align-self:flex-start;background:var(--bbp-bot_bubble);border:1px solid var(--bbp-border)}
.bbp-error{align-self:center;color:var(--bbp-text_secondary);font-size:.9em}
.bbp-form{display:flex;gap:8px;padding:12px;border-top:1px solid var(--bbp-border);background:var(--bbp-surface)}
.bbp-input{flex:1;padding:10px 12px;border-radius:var(--bbp-radius);border:1px solid var(--bbp-border);background:var(--bbp-input_background);color:var(--bbp-text);font:inherit}
.bbp-send{padding:10px 16px;border:0;border-radius:var(--bbp-radius);background:var(--bbp-accent);color:#030712;font-weight:600;cursor:pointer}
.bbp-send:disabled{opacity:.5;cursor:default}
"""

WIDGET_JS = r"""(function () {
  "use strict";
  var PID = document.body.getAttribute("data-public-id");
  var BASE = "/api/public/instances/" + encodeURIComponent(PID);
  var params = new URLSearchParams(location.search);
  var MODE = params.get("mode") === "fullpage" ? "fullpage" : "widget";
  var KEY = "bbp:" + PID;
  var store = MODE === "fullpage" ? window.localStorage : window.sessionStorage;
  var KEYS = ["accent", "accent_light", "background", "surface", "panel", "text", "text_secondary", "border",
              "user_bubble", "bot_bubble", "input_background", "radius", "font"];
  var HEX = /^#[0-9a-fA-F]{3,8}$/, FONT = /^[A-Za-z0-9 ,'-]{1,80}$/, RADIUS = /^[0-9]{1,2}px$/;
  function valid(k, v) { return typeof v === "string" && (k === "font" ? FONT.test(v) : k === "radius" ? RADIUS.test(v) : HEX.test(v)); }
  function applyTheme(theme) {
    KEYS.forEach(function (k) {
      var v = params.get("t_" + k) || (theme || {})[k];
      if (valid(k, v)) document.documentElement.style.setProperty("--bbp-" + k, v);
    });
  }
  var list = document.getElementById("bbp-messages");
  var input = document.getElementById("bbp-input");
  var send = document.getElementById("bbp-send");
  function bubble(cls, text) { var d = document.createElement("div"); d.className = "bbp-msg " + cls; d.textContent = text; list.appendChild(d); list.scrollTop = list.scrollHeight; return d; }
  function session() { try { return JSON.parse(store.getItem(KEY) || "null"); } catch (e) { return null; } }
  function saveSession(s) { try { store.setItem(KEY, JSON.stringify(s)); } catch (e) {} }
  // F5 admin preview: a server-created preview session arrives once in the fragment; it is stored like any
  // session and the fragment is cleared. The preview label lives on the server, never in the URL.
  (function () {
    var m = /^#preview=([0-9a-f-]{36})\.([A-Za-z0-9_-]{20,200})$/.exec(location.hash || "");
    if (m) { saveSession({session_id: m[1], session_token: m[2], preview: true}); }
    if (location.hash) { try { history.replaceState(null, "", location.pathname + location.search); } catch (e) {} }
  })();
  function dropSession() { try { store.removeItem(KEY); } catch (e) {} }
  function uuid() { return (crypto.randomUUID ? crypto.randomUUID() : "10000000-1000-4000-8000-100000000000".replace(/[018]/g, function (c) { return (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16); })); }

  fetch(BASE).then(function (r) { return r.ok ? r.json() : null; }).then(function (cfg) {
    if (!cfg) { bubble("bbp-error", "This chat is not available."); input.disabled = true; send.disabled = true; return; }
    applyTheme(cfg.theme);
    document.getElementById("bbp-title").textContent = cfg.title || "Chat";
    if (cfg.logo_url) { var img = document.getElementById("bbp-logo"); img.src = cfg.logo_url; img.hidden = false; }
    var welcome = params.get("welcome") || cfg.welcome_message;
    var s = session();
    if (s) {
      fetch(BASE + "/sessions/" + encodeURIComponent(s.session_id) + "/history", {headers: {"X-Chat-Session-Token": s.session_token}})
        .then(function (r) { if (!r.ok) { dropSession(); return null; } return r.json(); })
        .then(function (h) {
          if (welcome) bubble("bbp-bot", welcome);
          ((h && h.messages) || []).forEach(function (m) { bubble(m.role === "user" ? "bbp-user" : "bbp-bot", m.text); });
        });
    } else if (welcome) { bubble("bbp-bot", welcome); }
  });

  function parseEvents(buffer, onEvent) {
    var parts = buffer.split("\n\n");
    var rest = parts.pop();
    parts.forEach(function (block) {
      var ev = "message", data = "";
      block.split("\n").forEach(function (line) {
        if (line.indexOf("event: ") === 0) ev = line.slice(7);
        else if (line.indexOf("data: ") === 0) data += line.slice(6);
      });
      try { onEvent(ev, JSON.parse(data || "{}")); } catch (e) {}
    });
    return rest;
  }

  document.getElementById("bbp-form").addEventListener("submit", function (e) {
    e.preventDefault();
    var text = input.value.trim();
    if (!text || send.disabled) return;
    input.value = ""; send.disabled = true;
    bubble("bbp-user", text);
    var reply = bubble("bbp-bot", "");
    var s = session();
    var headers = {"Content-Type": "application/json"};
    var body = {message: text, client_message_id: uuid()};
    if (s) { headers["X-Chat-Session-Token"] = s.session_token; body.session_id = s.session_id; }
    fetch(BASE + "/chat/stream", {method: "POST", headers: headers, body: JSON.stringify(body)}).then(function (r) {
      if (!r.ok) {
        if (r.status === 404 && s) dropSession();
        reply.className = "bbp-msg bbp-error";
        reply.textContent = r.status === 429 ? "Please wait a moment before sending another message." : "Sorry, something went wrong. Please try again.";
        send.disabled = false; return;
      }
      var reader = r.body.getReader(), decoder = new TextDecoder(), buffer = "";
      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) { send.disabled = false; return; }
          buffer = parseEvents(buffer + decoder.decode(chunk.value, {stream: true}), function (ev, data) {
            if (ev === "session" && data.session_token) saveSession({session_id: data.session_id, session_token: data.session_token});
            else if (ev === "token") reply.textContent += data.token || "";
            else if (ev === "done") reply.textContent = data.full_response || reply.textContent;
            else if (ev === "error") { reply.className = "bbp-msg bbp-error"; reply.textContent = data.message || "Sorry, something went wrong."; }
          });
          list.scrollTop = list.scrollHeight;
          return pump();
        });
      }
      return pump();
    }).catch(function () { reply.className = "bbp-msg bbp-error"; reply.textContent = "Connection lost. Please try again."; send.disabled = false; });
  });
})();
"""


def embed_js(api_origin: str) -> str:
    # The origin is a platform setting, emitted as a JSON string literal (never spliced raw).
    return EMBED_JS_TEMPLATE.replace("__API_ORIGIN__", json.dumps(api_origin).replace("<", "\\u003c"))


def render_widget_html(public_id: str, custom_css_url: str = "") -> str:
    css = ""
    if custom_css_url:
        css = f'<link rel="stylesheet" href="{html.escape(custom_css_url, quote=True)}">'
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Chat</title>"
            "<link rel=\"stylesheet\" href=\"/api/public/widget.css\">" + css + "</head>"
            f"<body data-public-id=\"{html.escape(public_id, quote=True)}\"><div class=\"bbp\">"
            "<div class=\"bbp-header\"><img id=\"bbp-logo\" alt=\"\" hidden><span id=\"bbp-title\" class=\"bbp-title\"></span></div>"
            "<div id=\"bbp-messages\" class=\"bbp-messages\" aria-live=\"polite\"></div>"
            "<form id=\"bbp-form\" class=\"bbp-form\"><input id=\"bbp-input\" class=\"bbp-input\" maxlength=\"4000\" "
            "autocomplete=\"off\" aria-label=\"Message\"><button id=\"bbp-send\" class=\"bbp-send\" type=\"submit\">Send</button></form>"
            "</div><script src=\"/api/public/widget.js\" defer></script></body></html>")
