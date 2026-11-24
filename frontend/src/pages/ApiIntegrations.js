import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { Plug, Upload, Play, Save, Trash2, Plus, Loader2, X } from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import api from "@/utils/api";
import { colors, fonts, T, onFocus, onBlur } from "@/theme";

// API Integrations (spec 015): read-only transaction lookups. The tester calls the same
// server code as the bot, so the preview is exactly what the bot receives.

const AUTH_TYPES = [
  ["none", "None"], ["header", "API key in a header"], ["bearer", "Bearer token"], ["query", "API key in a query parameter"],
];
const SOURCES = [["tx_hash", "Transaction hash from the message"], ["fixed", "Fixed value"], ["none", "Not sent"]];
const FORMATS = [["auto", "Auto"], ["number", "Number"], ["text", "Text"], ["timestamp", "Unix timestamp"]];
const EMPTY_INTEGRATION = { name: "", base_url: "", auth_type: "none", auth_name: "", secret: "" };
const EMPTY_LOOKUP = { id: null, name: "Transaction lookup", endpoint_id: "", inputs: [], fields: [], not_found_on: "empty", enabled: true };
const STATUS_COLORS = { ok: colors.brand.success, not_found: colors.brand.warning, unavailable: colors.brand.error };
const STATUS_LABELS = { ok: "OK", not_found: "TRANSACTION NOT FOUND", unavailable: "LOOKUP UNAVAILABLE" };
const NOT_FOUND_MODES = [
  ["empty", "An empty result ([] or {})"],
  ["empty_or_404", "An empty result or HTTP 404 (e.g. Blockfrost)"],
];

const detail = (e, fallback) => (typeof e?.response?.data?.detail === "string" ? e.response.data.detail : fallback);
const small = { ...T.input, padding: "6px 10px", fontSize: "12px" };
const cell = { padding: "6px 8px", verticalAlign: "top" };

function Field({ label, hint, children }) {
  return (
    <div>
      <label style={T.label}>{label}</label>
      {children}
      {hint && <p style={T.hint}>{hint}</p>}
    </div>
  );
}

function IntegrationForm({ initial, onSaved, onCancel }) {
  const [f, setF] = useState({ ...EMPTY_INTEGRATION, ...initial, secret: "" });
  const [clearSecret, setClearSecret] = useState(false);
  const [saving, setSaving] = useState(false);
  const set = (k) => (e) => setF((p) => ({ ...p, [k]: e.target.value }));
  const save = async () => {
    setSaving(true);
    try {
      const body = { name: f.name, base_url: f.base_url, auth_type: f.auth_type, auth_name: f.auth_name,
        secret: clearSecret ? "" : (f.secret ? f.secret : (initial?.id ? null : "")) };
      const { data } = initial?.id ? await api.put(`/api-integrations/${initial.id}`, body) : await api.post("/api-integrations", body);
      toast.success(initial?.id ? "Integration saved" : "Integration added");
      onSaved(data);
    } catch (e) { toast.error(detail(e, "Couldn't save the integration")); }
    finally { setSaving(false); }
  };
  return (
    <div style={{ display: "grid", gap: "14px" }} data-testid="integration-form">
      <div style={{ display: "grid", gridTemplateColumns: "1fr 2fr", gap: "14px" }}>
        <Field label="Name"><input style={T.input} value={f.name} onChange={set("name")} onFocus={onFocus} onBlur={onBlur} placeholder="Koios (Cardano mainnet)" data-testid="integration-name" /></Field>
        <Field label="Base URL" hint="https only, public host. Every lookup stays on this host and path.">
          <input style={T.input} value={f.base_url} onChange={set("base_url")} onFocus={onFocus} onBlur={onBlur} placeholder="https://api.koios.rest/api/v1" data-testid="integration-base-url" />
        </Field>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "14px" }}>
        <Field label="Authentication">
          <select style={T.input} value={f.auth_type} onChange={set("auth_type")} data-testid="integration-auth-type">
            {AUTH_TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        </Field>
        {(f.auth_type === "header" || f.auth_type === "query") && (
          <Field label={f.auth_type === "header" ? "Header name" : "Query parameter"}>
            <input style={T.input} value={f.auth_name} onChange={set("auth_name")} onFocus={onFocus} onBlur={onBlur} placeholder={f.auth_type === "header" ? "project_id" : "api_key"} />
          </Field>
        )}
        {f.auth_type !== "none" && (
          <Field label="API key or token" hint={initial?.has_secret ? "A key is saved. Leave blank to keep it." : "Stored encrypted on the server; never shown again."}>
            <input type="password" autoComplete="new-password" style={T.input} value={f.secret} onChange={set("secret")} onFocus={onFocus} onBlur={onBlur} disabled={clearSecret} data-testid="integration-secret" />
            {initial?.has_secret && (
              <label style={{ ...T.hint, display: "flex", gap: "6px", alignItems: "center" }}>
                <input type="checkbox" checked={clearSecret} onChange={(e) => setClearSecret(e.target.checked)} /> Remove the saved key
              </label>
            )}
          </Field>
        )}
      </div>
      <div style={{ display: "flex", gap: "10px", justifyContent: "flex-end" }}>
        {onCancel && <button style={T.btnGhost} onClick={onCancel}><X size={14} /> Cancel</button>}
        <button style={{ ...T.btnPrimary, opacity: saving ? 0.6 : 1 }} disabled={saving || !f.name.trim() || !f.base_url.trim()} onClick={save} data-testid="integration-save">
          <Save size={14} /> {initial?.id ? "Save" : "Add integration"}
        </button>
      </div>
    </div>
  );
}

function ResultView({ result, onAddField, fieldPaths }) {
  if (!result) return null;
  return (
    <div style={{ display: "grid", gap: "12px", marginTop: "14px" }} data-testid="lookup-result">
      <div style={{ display: "flex", gap: "12px", alignItems: "center", flexWrap: "wrap", fontFamily: fonts.mono, fontSize: "12px" }}>
        <span data-testid="lookup-status" style={{ color: STATUS_COLORS[result.status] || colors.text.primary, fontWeight: 600 }}>
          {STATUS_LABELS[result.status] || result.status.toUpperCase()}
        </span>
        {result.reason && result.status !== "ok" && <span style={{ color: colors.text.muted }}>({result.reason.replace("_", " ")})</span>}
        {result.http_status != null && <span style={{ color: colors.text.secondary }}>HTTP {result.http_status} · {result.duration_ms} ms</span>}
        {result.message && <span style={{ color: colors.text.secondary }}>{result.message}</span>}
      </div>
      {result.request_url && (
        <p style={{ ...T.hint, fontFamily: fonts.mono, wordBreak: "break-all", margin: 0 }} data-testid="lookup-request">GET {result.request_url}</p>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)", gap: "14px" }}>
        <div>
          <p style={T.monoLabel}>Raw JSON{result.raw_truncated ? " (truncated)" : ""}</p>
          <pre data-testid="lookup-raw" style={{ margin: 0, padding: "10px", maxHeight: "360px", overflow: "auto", fontSize: "11px", fontFamily: fonts.mono, color: colors.text.secondary, backgroundColor: colors.bg.base, borderRadius: "8px", border: `1px solid ${colors.border.subtle}` }}>
            {result.raw ?? "(no response body)"}
          </pre>
        </div>
        <div style={{ display: "grid", gap: "12px", alignContent: "start" }}>
          <div>
            <p style={T.monoLabel}>Formatted preview</p>
            {result.fields?.length ? (
              <table data-testid="lookup-preview" style={{ width: "100%", borderCollapse: "collapse", fontSize: "13px", fontFamily: fonts.body }}>
                <tbody>
                  {result.fields.map((r) => (
                    <tr key={r.path} style={{ borderBottom: `1px solid ${colors.border.faint}` }}>
                      <td style={{ ...cell, color: colors.text.secondary }}>{r.label}</td>
                      <td style={{ ...cell, color: r.found ? colors.text.primary : colors.text.muted, fontWeight: 500 }}>
                        {r.found ? `${r.value}${r.unit ? ` ${r.unit}` : ""}` : "not available"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <p style={T.hint}>No fields selected yet. Add fields from the response below.</p>}
          </div>
          <div>
            <p style={T.monoLabel}>What the bot receives</p>
            <pre data-testid="lookup-bot-text" style={{ margin: 0, padding: "10px", whiteSpace: "pre-wrap", fontSize: "12px", fontFamily: fonts.mono, color: colors.text.primary, backgroundColor: colors.bg.base, borderRadius: "8px", border: `1px solid ${colors.border.subtle}` }}>{result.bot_text}</pre>
          </div>
        </div>
      </div>
      {result.paths?.length > 0 && (
        <div>
          <p style={T.monoLabel}>Response fields — add the ones the bot should see</p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", maxHeight: "180px", overflow: "auto" }}>
            {result.paths.map((p) => (
              <button key={p.path} disabled={fieldPaths.has(p.path)} onClick={() => onAddField(p.path)} title={p.sample}
                data-testid={`add-path-${p.path}`}
                style={{ ...T.btnGhost, padding: "3px 8px", fontSize: "11px", fontFamily: fonts.mono, opacity: fieldPaths.has(p.path) ? 0.4 : 1 }}>
                <Plus size={11} /> {p.path}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function LookupEditor({ integration, endpoints, initial, onSaved, onDeleted }) {
  const [lk, setLk] = useState(initial);
  const [filter, setFilter] = useState("");
  const [input, setInput] = useState("");
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState(null);
  useEffect(() => { setLk(initial); setResult(null); }, [initial]);

  const getEndpoints = useMemo(() => endpoints.filter((e) => e.method === "GET"), [endpoints]);
  const visible = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const list = q ? getEndpoints.filter((e) => `${e.path} ${e.summary}`.toLowerCase().includes(q)) : getEndpoints;
    return list.slice(0, 200);
  }, [getEndpoints, filter]);
  const endpoint = getEndpoints.find((e) => e.id === lk.endpoint_id);
  const fieldPaths = useMemo(() => new Set(lk.fields.map((f) => f.path)), [lk.fields]);

  const chooseEndpoint = (id) => {
    const ep = getEndpoints.find((e) => e.id === id);
    const inputs = (ep?.parameters || []).map((p, i) => ({
      param: p.name, in: p.in, required: p.required,
      source: i === 0 ? "tx_hash" : (p.in === "path" ? "fixed" : "none"), template: "$input", value: "",
    }));
    setLk((p) => ({ ...p, endpoint_id: id, inputs }));
    setResult(null);
  };
  const setInput_ = (i, k, v) => setLk((p) => ({ ...p, inputs: p.inputs.map((x, j) => (j === i ? { ...x, [k]: v } : x)) }));
  const setField = (i, k, v) => setLk((p) => ({ ...p, fields: p.fields.map((x, j) => (j === i ? { ...x, [k]: v } : x)) }));
  const addField = (path) => {
    const label = path.split(/[.[\]]/).filter(Boolean).pop() || path;
    setLk((p) => ({ ...p, fields: [...p.fields, { path, label: label.replace(/_/g, " "), unit: "", scale: 0, format: "auto" }] }));
  };
  const payload = () => ({ name: lk.name, endpoint_id: lk.endpoint_id, enabled: lk.enabled, not_found_on: lk.not_found_on || "empty",
    inputs: lk.inputs.map(({ param, source, template, value }) => ({ param, source, template, value })),
    fields: lk.fields.map((f) => ({ ...f, scale: Number(f.scale) || 0 })) });

  const test = async () => {
    setTesting(true);
    try {
      const { data } = await api.post(`/api-integrations/${integration.id}/lookups/test`, { lookup: payload(), input });
      setResult(data);
    } catch (e) { setResult(null); toast.error(detail(e, "Test failed")); }
    finally { setTesting(false); }
  };
  const save = async () => {
    setSaving(true);
    try {
      const { data } = lk.id
        ? await api.put(`/api-integrations/${integration.id}/lookups/${lk.id}`, payload())
        : await api.post(`/api-integrations/${integration.id}/lookups`, payload());
      toast.success("Lookup saved");
      onSaved(data);
    } catch (e) { toast.error(detail(e, "Couldn't save the lookup")); }
    finally { setSaving(false); }
  };
  const remove = async () => {
    try { await api.delete(`/api-integrations/${integration.id}/lookups/${lk.id}`); toast.success("Lookup deleted"); onDeleted(); }
    catch (e) { toast.error(detail(e, "Couldn't delete the lookup")); }
  };

  return (
    <div style={{ display: "grid", gap: "16px" }} data-testid="lookup-editor">
      <div style={{ display: "grid", gridTemplateColumns: "1fr 2fr", gap: "14px" }}>
        <Field label="Lookup name"><input style={T.input} value={lk.name} onChange={(e) => setLk({ ...lk, name: e.target.value })} onFocus={onFocus} onBlur={onBlur} data-testid="lookup-name" /></Field>
        <Field label={`Endpoint (${getEndpoints.length} GET endpoints)`} hint="Only read-only GET endpoints can be used.">
          <div style={{ display: "flex", gap: "8px" }}>
            <input style={{ ...T.input, maxWidth: "200px" }} placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} data-testid="endpoint-filter" />
            <select style={T.input} value={lk.endpoint_id} onChange={(e) => chooseEndpoint(e.target.value)} data-testid="endpoint-select">
              <option value="">Choose an endpoint…</option>
              {endpoint && !visible.includes(endpoint) && <option value={endpoint.id}>{endpoint.id}</option>}
              {visible.map((e) => <option key={e.id} value={e.id}>{e.id}{e.summary ? ` — ${e.summary}` : ""}</option>)}
            </select>
          </div>
        </Field>
      </div>

      {endpoint && (
        <div>
          <p style={T.monoLabel}>Inputs</p>
          {lk.inputs.length === 0 ? <p style={T.hint}>This endpoint has no parameters, so there is nowhere to send the transaction hash. Choose another endpoint.</p> : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "12px" }} data-testid="inputs-table">
              <thead><tr>{["Parameter", "Value", "Template / value"].map((h) => <th key={h} style={T.th}>{h}</th>)}</tr></thead>
              <tbody>
                {lk.inputs.map((p, i) => (
                  <tr key={p.param}>
                    <td style={{ ...cell, fontFamily: fonts.mono, color: colors.text.primary }}>{p.param} <span style={{ color: colors.text.muted }}>({p.in}{p.required ? ", required" : ""})</span></td>
                    <td style={cell}>
                      <select style={small} value={p.source} onChange={(e) => setInput_(i, "source", e.target.value)} data-testid={`input-source-${p.param}`}>
                        {SOURCES.filter(([v]) => !(v === "none" && p.in === "path")).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                      </select>
                    </td>
                    <td style={cell}>
                      {p.source === "tx_hash" && <input style={small} value={p.template} onChange={(e) => setInput_(i, "template", e.target.value)} data-testid={`input-template-${p.param}`} title="$input is replaced by the 64-character transaction hash" />}
                      {p.source === "fixed" && <input style={small} value={p.value} onChange={(e) => setInput_(i, "value", e.target.value)} />}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p style={T.hint}>The bot looks for a 64-character hexadecimal transaction hash in the user's message. <code>$input</code> in the template is replaced by it — for example <code>{"{$input}"}</code> for Koios.</p>
          <div style={{ display: "flex", gap: "10px", alignItems: "center", marginTop: "10px" }}>
            <label style={{ ...T.label, margin: 0 }}>This API reports an unknown transaction as</label>
            <select style={{ ...small, width: "auto" }} value={lk.not_found_on || "empty"} onChange={(e) => setLk({ ...lk, not_found_on: e.target.value })} data-testid="lookup-not-found-on">
              {NOT_FOUND_MODES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </div>
          <p style={T.hint}>Anything else — a missing endpoint, errors, or a response without the configured fields — is reported as "lookup unavailable", and the bot won't say the transaction doesn't exist.</p>
        </div>
      )}

      {endpoint && (
        <div>
          <p style={T.monoLabel}>Response fields the bot sees</p>
          {lk.fields.length === 0 ? <p style={T.hint}>None yet. Run a test, then add fields from the response.</p> : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "12px" }} data-testid="fields-table">
              <thead><tr>{["Path", "Label", "Unit", "Decimal shift", "Format", ""].map((h) => <th key={h} style={T.th}>{h}</th>)}</tr></thead>
              <tbody>
                {lk.fields.map((f, i) => (
                  <tr key={i}>
                    <td style={cell}><input style={{ ...small, fontFamily: fonts.mono }} value={f.path} onChange={(e) => setField(i, "path", e.target.value)} /></td>
                    <td style={cell}><input style={small} value={f.label} onChange={(e) => setField(i, "label", e.target.value)} data-testid={`field-label-${i}`} /></td>
                    <td style={cell}><input style={{ ...small, width: "80px" }} value={f.unit} onChange={(e) => setField(i, "unit", e.target.value)} data-testid={`field-unit-${i}`} /></td>
                    <td style={cell}><input type="number" min="0" max="18" style={{ ...small, width: "70px" }} value={f.scale} onChange={(e) => setField(i, "scale", e.target.value)} data-testid={`field-scale-${i}`} title="Divide by 10^n, e.g. 6 for lovelace to ADA" /></td>
                    <td style={cell}>
                      <select style={small} value={f.format} onChange={(e) => setField(i, "format", e.target.value)} data-testid={`field-format-${i}`}>
                        {FORMATS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                      </select>
                    </td>
                    <td style={cell}><button style={{ ...T.btnGhost, padding: "4px" }} onClick={() => setLk((p) => ({ ...p, fields: p.fields.filter((_, j) => j !== i) }))} title="Remove"><X size={13} /></button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {endpoint && (
        <div style={{ padding: "14px", borderRadius: "10px", backgroundColor: colors.bg.panel || colors.bg.base, border: `1px solid ${colors.border.subtle}` }}>
          <p style={T.monoLabel}>Test lookup</p>
          <div style={{ display: "flex", gap: "8px" }}>
            <input style={{ ...T.input, fontFamily: fonts.mono }} value={input} onChange={(e) => setInput(e.target.value)} onFocus={onFocus} onBlur={onBlur}
              placeholder="A user message or a 64-character transaction hash" data-testid="lookup-test-input" />
            <button style={{ ...T.btnPrimary, whiteSpace: "nowrap", opacity: testing ? 0.6 : 1 }} disabled={testing} onClick={test} data-testid="lookup-test-btn">
              {testing ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />} Test lookup
            </button>
          </div>
          <ResultView result={result} onAddField={addField} fieldPaths={fieldPaths} />
        </div>
      )}

      <div style={{ display: "flex", gap: "10px", justifyContent: "flex-end", alignItems: "center" }}>
        <label style={{ ...T.hint, display: "flex", gap: "6px", alignItems: "center", margin: 0 }}>
          <input type="checkbox" checked={lk.enabled} onChange={(e) => setLk({ ...lk, enabled: e.target.checked })} data-testid="lookup-enabled" /> Enabled
        </label>
        {lk.id && <button style={T.btnGhost} onClick={remove}><Trash2 size={14} /> Delete lookup</button>}
        <button style={{ ...T.btnPrimary, opacity: saving ? 0.6 : 1 }} disabled={saving || !endpoint} onClick={save} data-testid="lookup-save">
          <Save size={14} /> Save lookup
        </button>
      </div>
    </div>
  );
}

export default function ApiIntegrations() {
  const { selectedInstance } = useAuth();
  const instanceId = selectedInstance?.id;
  const [state, setState] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState(false);
  const [endpoints, setEndpoints] = useState([]);
  const [lookupId, setLookupId] = useState(null);
  const [importing, setImporting] = useState(false);
  const fileRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const { data } = await api.get("/api-integrations");
      setState(data);
      setSelectedId((cur) => (data.integrations.some((i) => i.id === cur) ? cur : data.integrations[0]?.id || null));
    } catch (e) { toast.error(detail(e, "Couldn't load API integrations")); }
  }, []);
  useEffect(() => { setState(null); setSelectedId(null); setLookupId(null); if (instanceId) load(); }, [instanceId, load]);

  const integration = state?.integrations.find((i) => i.id === selectedId);
  useEffect(() => {
    setEndpoints([]);
    if (!integration?.id || !integration.endpoint_count) return;
    api.get(`/api-integrations/${integration.id}/endpoints`).then(({ data }) => setEndpoints(data.endpoints))
      .catch((e) => toast.error(detail(e, "Couldn't load endpoints")));
  }, [integration?.id, integration?.import?.imported_at, integration?.endpoint_count]);

  const lookups = (state?.lookups || []).filter((l) => l.integration_id === selectedId);
  const lookup = lookups.find((l) => l.id === lookupId) || null;
  const editorLookup = useMemo(() => (lookup ? { ...lookup } : { ...EMPTY_LOOKUP }), [lookup]);

  const toggleAccess = async () => {
    try {
      const { data } = await api.put("/api-integrations/settings", { bot_access: !state.bot_access });
      setState((s) => ({ ...s, bot_access: data.bot_access }));
      toast.success(data.bot_access ? "Bot lookups turned on for this workspace" : "Bot lookups turned off");
    } catch (e) { toast.error(detail(e, "Couldn't change bot access")); }
  };
  const importFile = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setImporting(true);
    try {
      let doc;
      try { doc = JSON.parse(await file.text()); } catch { toast.error("The file isn't valid JSON. Convert YAML OpenAPI files to JSON first."); return; }
      const { data } = await api.post(`/api-integrations/${integration.id}/import`, { document: doc });
      toast.success(`Imported ${data.endpoints} endpoints (${data.get_endpoints} GET) from ${data.kind === "openapi" ? "an OpenAPI document" : "a custom reference"}`);
      await load();
    } catch (err) { toast.error(detail(err, "Import failed")); }
    finally { setImporting(false); if (fileRef.current) fileRef.current.value = ""; }
  };
  const removeIntegration = async () => {
    if (!window.confirm(`Delete ${integration.name} and its lookups?`)) return;
    try { await api.delete(`/api-integrations/${integration.id}`); toast.success("Integration deleted"); setLookupId(null); await load(); }
    catch (e) { toast.error(detail(e, "Couldn't delete the integration")); }
  };

  if (!instanceId) return <div style={T.page}><p style={T.sectionSub}>Select a workspace first.</p></div>;

  return (
    <div style={T.page}>
      <p style={T.overline}>Live data</p>
      <h1 style={T.h1 || { fontFamily: fonts.heading }}>API Integrations</h1>
      <p style={{ ...T.sectionSub, maxWidth: "760px" }}>
        Read-only lookups from a public API. When a user's message contains a transaction hash, the bot calls the
        configured endpoint and sees only the fields you choose. Credentials stay on the server.
      </p>

      {!state ? <p style={T.sectionSub}><Loader2 size={14} className="animate-spin" /> Loading…</p> : (<>
        <div style={{ ...T.card, display: "flex", justifyContent: "space-between", alignItems: "center", gap: "16px", marginBottom: "20px" }} data-testid="bot-access-card">
          <div>
            <p style={{ ...T.sectionTitle, margin: 0 }}>Bot lookups for {selectedInstance?.name || "this workspace"}</p>
            <p style={{ ...T.hint, margin: "4px 0 0" }}>Off by default. When on, web chat, Discord and the external API use enabled lookups.</p>
          </div>
          <button onClick={toggleAccess} data-testid="bot-access-toggle" aria-pressed={state.bot_access}
            style={state.bot_access ? T.btnPrimary : T.btnSecondary}>{state.bot_access ? "On" : "Off"}</button>
        </div>

        <div style={{ ...T.card, marginBottom: "20px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "14px" }}>
            <p style={{ ...T.sectionTitle, margin: 0 }}>Integrations</p>
            {!adding && <button style={T.btnSecondary} onClick={() => setAdding(true)} data-testid="integration-add"><Plus size={14} /> Add integration</button>}
          </div>
          {adding && <IntegrationForm onCancel={() => setAdding(false)} onSaved={async (d) => { setAdding(false); await load(); setSelectedId(d.id); }} />}
          {!adding && state.integrations.length === 0 && <p style={T.sectionSub}>No integrations yet.</p>}
          {!adding && state.integrations.length > 0 && (
            <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
              {state.integrations.map((i) => (
                <button key={i.id} onClick={() => { setSelectedId(i.id); setLookupId(null); setEditing(false); }} aria-pressed={i.id === selectedId}
                  style={i.id === selectedId ? T.btnPrimary : T.btnSecondary}><Plug size={13} /> {i.name}</button>
              ))}
            </div>
          )}
        </div>

        {integration && !adding && (
          <div style={{ ...T.card, marginBottom: "20px" }} data-testid="integration-detail">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "12px", marginBottom: "14px" }}>
              <div>
                <p style={{ ...T.sectionTitle, margin: 0 }}>{integration.name}</p>
                <p style={{ ...T.hint, fontFamily: fonts.mono, margin: "4px 0 0" }}>{integration.base_url} · {AUTH_TYPES.find(([v]) => v === integration.auth_type)?.[1]}{integration.has_secret ? " · key saved" : ""}</p>
              </div>
              <div style={{ display: "flex", gap: "8px" }}>
                <button style={T.btnGhost} onClick={() => setEditing((v) => !v)}>{editing ? "Close settings" : "Settings"}</button>
                <button style={T.btnGhost} onClick={removeIntegration}><Trash2 size={14} /> Delete</button>
              </div>
            </div>
            {editing && <div style={{ marginBottom: "16px" }}><IntegrationForm initial={integration} onCancel={() => setEditing(false)} onSaved={async () => { setEditing(false); await load(); }} /></div>}

            <div style={{ display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap", marginBottom: "16px" }}>
              <input ref={fileRef} type="file" accept=".json,application/json" style={{ display: "none" }} onChange={importFile} data-testid="import-input" />
              <button style={T.btnSecondary} onClick={() => fileRef.current?.click()} disabled={importing} data-testid="import-btn">
                {importing ? <Loader2 size={14} className="animate-spin" /> : <Upload size={14} />} Import OpenAPI or custom JSON
              </button>
              <span style={T.hint} data-testid="import-summary">
                {integration.import ? `${integration.endpoint_count} endpoints (${integration.get_endpoint_count} GET) from ${integration.import.kind === "openapi" ? "OpenAPI" : "custom reference"}` : "Nothing imported yet."}
              </span>
            </div>

            {integration.endpoint_count > 0 && (<>
              <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center", marginBottom: "14px" }}>
                <span style={T.monoLabel}>Lookups</span>
                {lookups.map((l) => (
                  <button key={l.id} onClick={() => setLookupId(l.id)} aria-pressed={l.id === lookupId}
                    style={l.id === lookupId ? T.btnPrimary : T.btnSecondary}>{l.name}{l.enabled ? "" : " (off)"}</button>
                ))}
                <button style={lookupId ? T.btnGhost : T.btnPrimary} onClick={() => setLookupId(null)} data-testid="lookup-new"><Plus size={13} /> New lookup</button>
              </div>
              <LookupEditor integration={integration} endpoints={endpoints} initial={editorLookup}
                onSaved={async (d) => { await load(); setLookupId(d.id); }} onDeleted={async () => { setLookupId(null); await load(); }} />
            </>)}
          </div>
        )}
      </>)}
    </div>
  );
}
