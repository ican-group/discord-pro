import { useState, useEffect, useRef, useCallback, useMemo } from "react";
import axios from "axios";
import { Bot, Send, RotateCcw, Copy, Check } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { colors, fonts } from "../theme";

// Embeddable chat widget (spec 016). Ported from BitBotPro 5509ca6/1b8e0a3: modes, URL theme,
// welcome message, custom stylesheet and public bot name. Existing /widget?instance=<id> embeds
// render the same panel as before.

const API = `/api`;
const MODES = ["popup", "inline", "fullpage"];
const MISSING_BOT_MESSAGE = "This chat isn't set up correctly: the embed code has no bot ID. Copy it again from BridgeBot's Embed Code page.";

// Sandboxed or third-party frames can block storage; never let that break the chat.
const storage = () => { try { return window.sessionStorage; } catch { return null; } };
const store = {
  get: (k) => { try { return storage()?.getItem(k) || null; } catch { return null; } },
  set: (k, v) => { try { storage()?.setItem(k, v); } catch { /* ignore */ } },
  remove: (k) => { try { storage()?.removeItem(k); } catch { /* ignore */ } },
};

// Theme values come from the page that embeds the widget: accept only plain CSS values.
const COLOR = /^(#[0-9a-fA-F]{3,8}|(rgb|rgba|hsl|hsla)\([0-9.,%\s/]{1,60}\)|[a-zA-Z]{3,20})$/;
const FONT = /^[A-Za-z0-9 ,'"-]{1,100}$/;
const RADIUS = /^\d{1,3}(\.\d{1,2})?(px|rem|em|%)$/;
const THEME = {
  accent: ["--bb-accent", COLOR], accentLight: ["--bb-accent-light", COLOR], bg: ["--bb-bg", COLOR],
  surface: ["--bb-surface", COLOR], panel: ["--bb-panel", COLOR], text: ["--bb-text", COLOR],
  textSecondary: ["--bb-text-secondary", COLOR], border: ["--bb-border", COLOR],
  userBubble: ["--bb-user-bubble", COLOR], botBubble: ["--bb-bot-bubble", COLOR],
  inputBg: ["--bb-input-bg", COLOR], font: ["--bb-font", FONT], radius: ["--bb-radius", RADIUS],
};
const DEFAULTS = {
  "--bb-accent": colors.brand.blue, "--bb-accent-light": colors.brand.cyan, "--bb-bg": colors.bg.base,
  "--bb-surface": colors.bg.surface, "--bb-panel": colors.bg.panel, "--bb-text": colors.text.primary,
  "--bb-text-secondary": colors.text.secondary, "--bb-border": colors.border.default,
  "--bb-user-bubble": colors.brand.blue, "--bb-bot-bubble": colors.bg.panel, "--bb-input-bg": colors.bg.surface,
  "--bb-font": fonts.body, "--bb-radius": "12px",
};

// Defaults have zero specificity, so a custom stylesheet's :root { --bb-*: ... } overrides them;
// theme values from the embed code are set inline and win over both, as in the original widget.
const DEFAULTS_CSS = `:where(:root){${Object.entries(DEFAULTS).map(([k, v]) => `${k}:${v}`).join(";")}}`;

function themeVars(params) {
  const vars = {};
  Object.entries(THEME).forEach(([key, [cssVar, re]]) => {
    const v = (params.get(key) || "").trim();
    if (v && re.test(v)) vars[cssVar] = v;
  });
  // Derived defaults, as in the original widget.
  const accent = (params.get("accent") || "").trim();
  if (accent && COLOR.test(accent)) {
    if (!params.get("userBubble")) vars["--bb-user-bubble"] = accent;
    if (!params.get("accentLight")) vars["--bb-accent-light"] = accent;
  }
  return vars;
}

function CopyBtn({ text }) {
  const [copied, setCopied] = useState(false);
  const go = useCallback(() => {
    navigator.clipboard?.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }).catch(() => {});
  }, [text]);
  return (
    <button onClick={go} data-testid="bubble-copy-btn" title={copied ? "Copied" : "Copy message"} aria-label="Copy message"
      style={{ background: "none", border: "none", cursor: "pointer", color: "var(--bb-text-secondary)", padding: "2px", marginTop: "4px", display: "flex", alignItems: "center" }}>
      {copied ? <Check size={11} /> : <Copy size={11} />}
    </button>
  );
}

const avatar = (size, radius) => ({
  width: size, height: size, background: "linear-gradient(135deg, var(--bb-accent), var(--bb-accent-light))",
  borderRadius: radius, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
});

export default function ChatWidget() {
  const [searchParams] = useSearchParams();
  const instanceId = (searchParams.get("instance") || "").slice(0, 128);
  const mode = MODES.includes(searchParams.get("mode")) ? searchParams.get("mode") : "popup";
  const isPreview = searchParams.get("preview") === "1";
  const welcomeParam = (searchParams.get("welcome") || "").slice(0, 2000);
  const vars = useMemo(() => themeVars(searchParams), [searchParams]);
  const sessionKey = `widget_session_${instanceId || "default"}`;

  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const [botName, setBotName] = useState("Assistant");
  const [welcome, setWelcome] = useState(welcomeParam);
  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (!instanceId) return undefined;
    let cancelled = false;
    let link = null;
    axios.get(`${API}/embed/config/${encodeURIComponent(instanceId)}`).then(({ data: cfg }) => {
      if (cancelled || !cfg) return;
      if (cfg.name) setBotName(cfg.name);
      if (!welcomeParam && cfg.welcome_message) setWelcome(cfg.welcome_message);
      // https only (the server validates it too).
      if (!isPreview && typeof cfg.custom_css_url === "string" && cfg.custom_css_url.startsWith("https://")) {
        link = document.createElement("link");
        link.rel = "stylesheet"; link.href = cfg.custom_css_url; link.setAttribute("data-bridgebot-css", "true");
        document.head.appendChild(link);
      }
    }).catch(() => {});
    const saved = isPreview ? null : store.get(sessionKey);
    if (saved) {
      setSessionId(saved);
      axios.get(`${API}/chat/history/${encodeURIComponent(saved)}`).then(r => { if (!cancelled) setMessages(r.data.messages || []); }).catch(() => {});
    }
    return () => { cancelled = true; if (link) link.remove(); };
  }, [instanceId, sessionKey, welcomeParam, isPreview]);

  useEffect(() => {
    if (!isPreview || window.parent === window) return undefined;
    const origin = window.location.origin;
    let stylesheet = null;
    const report = (status) => window.parent.postMessage({ type: "bridgebot-preview-stylesheet-status", status }, origin);
    const receive = (event) => {
      if (event.origin !== origin || event.source !== window.parent ||
          event.data?.type !== "bridgebot-preview-stylesheet") return;
      const draft = event.data.stylesheet || {};
      stylesheet?.remove();
      stylesheet = null;
      if (typeof draft.css === "string" && draft.css.length <= 256 * 1024) {
        stylesheet = document.createElement("style");
        stylesheet.textContent = draft.css;
        document.head.appendChild(stylesheet);
        report(`Previewing ${draft.name || "local stylesheet"}`);
      } else if (typeof draft.url === "string" && draft.url.startsWith("https://")) {
        const link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = draft.url;
        link.onload = () => { if (stylesheet === link) report("Stylesheet loaded in Live Preview."); };
        link.onerror = () => { if (stylesheet === link) report("Couldn’t load the stylesheet. Check that the URL serves a CSS file."); };
        stylesheet = link;
        document.head.appendChild(link);
      } else {
        report("");
      }
    };
    window.addEventListener("message", receive);
    window.parent.postMessage({ type: "bridgebot-preview-ready" }, origin);
    return () => { window.removeEventListener("message", receive); stylesheet?.remove(); };
  }, [isPreview]);

  useEffect(() => { messagesEndRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, isLoading]);
  useEffect(() => { if (mode !== "inline") setTimeout(() => inputRef.current?.focus(), 100); }, [mode]);

  const sendMessage = async () => {
    if (!input.trim() || isLoading) return;
    const text = input.trim();
    if (!instanceId) {
      setMessages(prev => [...prev, { role: "assistant", content: MISSING_BOT_MESSAGE, timestamp: new Date().toISOString(), isError: true }]);
      return;
    }
    setInput("");
    setMessages(prev => [...prev, { role: "user", content: text, timestamp: new Date().toISOString() }]);
    setIsLoading(true);
    try {
      const res = await axios.post(`${API}/chat/send`, { message: text, session_id: sessionId, instance_id: instanceId });
      if (!sessionId) {
        setSessionId(res.data.session_id);
        if (!isPreview) store.set(sessionKey, res.data.session_id);
      }
      setMessages(prev => [...prev, { role: "assistant", content: res.data.response, lookup: res.data.lookup, timestamp: new Date().toISOString() }]);
    } catch {
      setMessages(prev => [...prev, { role: "assistant", content: "Sorry, an error occurred. Please try again.", timestamp: new Date().toISOString(), isError: true }]);
    } finally { setIsLoading(false); }
  };

  const reset = () => { if (!isPreview) store.remove(sessionKey); setSessionId(null); setMessages([]); };
  const handleKey = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); } };

  const example = isPreview && messages.length === 0 ? [
    { role: "user", content: "Example question", timestamp: "preview-user", isExample: true },
    { role: "assistant", content: "Example bot reply", timestamp: "preview-bot", isExample: true },
  ] : [];
  const shown = !instanceId
    ? [{ role: "assistant", content: MISSING_BOT_MESSAGE, timestamp: "missing", isError: true }, ...messages]
    // The welcome message stays at the top of the conversation (it isn't sent to the server).
    : (welcome ? [{ role: "assistant", content: welcome, timestamp: "welcome", isWelcome: true }, ...example, ...messages] : [...example, ...messages]);
  const canSend = !isLoading && input.trim();

  return (
    <div data-testid="chat-widget" data-mode={mode} style={{ ...vars, width: "100%", height: mode === "inline" ? "100%" : "100vh", minHeight: mode === "inline" ? "400px" : undefined, backgroundColor: "var(--bb-bg)" }}>
      <div style={{
        height: "100%", maxWidth: mode === "fullpage" ? "720px" : "none", margin: "0 auto",
        display: "flex", flexDirection: "column", fontFamily: "var(--bb-font)", overflow: "hidden", color: "var(--bb-text)",
        borderLeft: mode === "fullpage" ? "1px solid var(--bb-border)" : "none", borderRight: mode === "fullpage" ? "1px solid var(--bb-border)" : "none",
      }}>
        <style>{DEFAULTS_CSS}</style>
        {/* Header */}
        <div style={{ backgroundColor: "var(--bb-surface)", borderBottom: "1px solid var(--bb-border)", padding: "12px 16px", display: "flex", alignItems: "center", gap: "10px", flexShrink: 0 }}>
          <div style={avatar("30px", "10px")}><Bot size={16} color="#FFFFFF" /></div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <p data-testid="widget-bot-name" style={{ fontFamily: fonts.heading, fontSize: "14px", fontWeight: "700", color: "var(--bb-text)", margin: 0, letterSpacing: "-0.3px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {botName}
            </p>
            <div style={{ display: "flex", alignItems: "center", gap: "4px" }}>
              <div style={{ width: "5px", height: "5px", borderRadius: "50%", backgroundColor: colors.brand.success, boxShadow: `0 0 6px ${colors.brand.success}` }} />
              <span style={{ fontFamily: fonts.mono, fontSize: "9px", color: colors.brand.success, textTransform: "uppercase", letterSpacing: "0.1em" }}>Online</span>
            </div>
          </div>
          <button onClick={reset} data-testid="widget-reset-btn" title="New conversation" aria-label="New conversation"
            style={{ background: "none", border: "none", cursor: "pointer", color: "var(--bb-text-secondary)", padding: "4px", display: "flex", alignItems: "center" }}>
            <RotateCcw size={13} />
          </button>
        </div>

        {/* Messages */}
        <div style={{ flex: 1, overflowY: "auto", padding: "14px" }}>
          {shown.length === 0 && !isLoading && (
            <div style={{ textAlign: "center", paddingTop: "40px" }}>
              <div style={{ ...avatar("44px", "12px"), margin: "0 auto 14px" }}><Bot size={22} color="#FFFFFF" /></div>
              <p style={{ fontFamily: fonts.heading, fontSize: "18px", fontWeight: "700", color: "var(--bb-text)", margin: "0 0 6px", letterSpacing: "-0.3px" }}>Hi! How can I help?</p>
              <p style={{ fontSize: "13px", color: "var(--bb-text-secondary)", margin: 0 }}>Ask me anything.</p>
            </div>
          )}

          {shown.map((msg, i) => (
            <div key={`${msg.role}-${msg.timestamp || i}-${i}`} data-testid={msg.role === "assistant" ? "widget-bot-message" : "widget-user-message"}
              style={{ display: "flex", justifyContent: msg.role === "user" ? "flex-end" : "flex-start", marginBottom: "10px" }}>
              {msg.role === "assistant" && (
                <div style={{ ...avatar("22px", "10px"), marginRight: "7px", marginTop: "3px" }}><Bot size={11} color="#FFFFFF" /></div>
              )}
              <div style={{ maxWidth: "80%" }}>
                <div style={{
                  padding: "9px 12px", borderRadius: "var(--bb-radius)",
                  backgroundColor: msg.role === "user" ? "var(--bb-user-bubble)" : (msg.isError ? "rgba(244,63,94,0.08)" : "var(--bb-bot-bubble)"),
                  border: msg.role === "user" ? "1px solid transparent" : `1px solid ${msg.isError ? "rgba(244,63,94,0.2)" : "var(--bb-border)"}`,
                  color: msg.isError ? colors.brand.error : (msg.role === "user" ? "#FFFFFF" : "var(--bb-text)"),
                  fontSize: "13px", lineHeight: "1.6",
                }}>
                  <p style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{msg.content}</p>
                </div>
                {isPreview && msg.lookup && (
                  <small style={{ color: "var(--bb-text-secondary)" }}>
                    {msg.lookup.provider} · {msg.lookup.status === "unavailable" ? "unavailable"
                      : msg.lookup.status === "no_call" ? "no live lookup"
                      : `${msg.lookup.calls} API ${msg.lookup.calls === 1 ? "call" : "calls"}${msg.lookup.status === "partial" ? " · some calls failed" : ""}`}
                  </small>
                )}
                {msg.role === "assistant" && !msg.isError && !msg.isWelcome && !msg.isExample && <CopyBtn text={msg.content} />}
              </div>
            </div>
          ))}

          {isLoading && (
            <div style={{ display: "flex", justifyContent: "flex-start", marginBottom: "10px" }}>
              <div style={{ ...avatar("22px", "10px"), marginRight: "7px" }}><Bot size={11} color="#FFFFFF" /></div>
              <div style={{ padding: "10px 14px", backgroundColor: "var(--bb-bot-bubble)", border: "1px solid var(--bb-border)", borderRadius: "var(--bb-radius)", display: "flex", gap: "4px" }}>
                {[0, 1, 2].map(i => (
                  <div key={i} style={{ width: "6px", height: "6px", borderRadius: "50%", backgroundColor: "var(--bb-accent-light)", animation: "typing-dot 1.4s infinite ease-in-out", animationDelay: `${i * 0.16}s` }} />
                ))}
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div style={{ borderTop: "1px solid var(--bb-border)", padding: "10px 12px", backgroundColor: "var(--bb-bg)", flexShrink: 0 }}>
          <div style={{ display: "flex", gap: "8px" }}>
            <input ref={inputRef} value={input} onChange={e => setInput(e.target.value)} onKeyDown={handleKey}
              placeholder="Type a message..." data-testid="widget-input" maxLength={4000} aria-label="Message"
              style={{
                flex: 1, backgroundColor: "var(--bb-input-bg)", border: "1px solid var(--bb-border)",
                borderRadius: "var(--bb-radius)", padding: "9px 12px", color: "var(--bb-text)",
                fontFamily: "var(--bb-font)", fontSize: "13px", outline: "none",
              }}
            />
            <button onClick={sendMessage} disabled={!canSend} data-testid="widget-send-btn" aria-label="Send"
              style={{
                width: "36px", height: "36px", backgroundColor: canSend ? "var(--bb-accent)" : "var(--bb-panel)",
                border: "1px solid var(--bb-border)", borderRadius: "var(--bb-radius)", cursor: canSend ? "pointer" : "not-allowed",
                display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, transition: "all 0.2s ease",
              }}>
              <Send size={13} color={canSend ? "#FFFFFF" : "var(--bb-text-secondary)"} />
            </button>
          </div>
          <p style={{ fontFamily: fonts.mono, fontSize: "9px", color: "var(--bb-text-secondary)", margin: "6px 0 0", textTransform: "uppercase", letterSpacing: "0.1em", textAlign: "center" }}>
            Powered by BridgeBot
          </p>
        </div>
      </div>
    </div>
  );
}
