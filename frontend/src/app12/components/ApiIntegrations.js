/**
 * API integrations of a Bot (restoration R3): create/edit, authentication (secret stored as an
 * encrypted Organisation credential, never shown again), Test connection, Discover endpoints
 * (documentation URL or an uploaded OpenAPI JSON), endpoint list with enable/disable and Test,
 * activity and usage. Every external call happens in the API through the outbound guard.
 */
import React, { useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge, CheckField, ConfirmButton, DataTable, ErrorBox, KeyValues, Loading, Notice, Section, SelectField, TextField } from "./common";
import { fmtDate, useAction, useLoad } from "../util";

export const AUTH_TYPES = [["none", "No authentication"], ["bearer", "Bearer token"],
  ["api_key_header", "API key in a header"], ["api_key_query", "API key in a query parameter"]];
const DOC_TYPES = [["openapi", "OpenAPI / Swagger (JSON)"], ["custom_reference", "Reference list (JSON)"], ["manual", "Manual endpoints"]];
const EMPTY = { name: "", base_url: "", auth_type: "none", auth_header_name: "", auth_query_param: "", secret: "",
  doc_source_type: "openapi", doc_source_url: "" };

/** Client-side check of the integration form (the API validates again). */
export function integrationProblem(f, { creating = true } = {}) {
  if (!f.name || !f.name.trim()) return "Enter a name.";
  if (!/^https:\/\/[^\s/?#]+/i.test((f.base_url || "").trim())) return "The base URL must start with https://";
  if ((f.base_url || "").includes("?")) return "The base URL must not contain a query string.";
  if (creating && f.auth_type !== "none" && !(f.secret || "").trim()) return "Enter the API key or token.";
  if (f.doc_source_url && !/^https:\/\//i.test(f.doc_source_url.trim())) return "The documentation URL must start with https://";
  return "";
}

function body(f) {
  const out = { name: f.name.trim(), base_url: f.base_url.trim(), auth_type: f.auth_type, doc_source_type: f.doc_source_type,
    doc_source_url: (f.doc_source_url || "").trim() };
  if (f.auth_type === "api_key_header") out.auth_header_name = (f.auth_header_name || "X-API-Key").trim();
  if (f.auth_type === "api_key_query") out.auth_query_param = (f.auth_query_param || "api_key").trim();
  if ((f.secret || "").trim()) out.secret = f.secret.trim();
  return out;
}

function IntegrationForm({ initial, onSubmit, submitLabel, creating, busy }) {
  const [f, setF] = useState({ ...EMPTY, ...initial, secret: "" });
  const set = (k) => (v) => setF({ ...f, [k]: v });
  const [problem, setProblem] = useState("");
  return (
    <form className="max-w-lg" aria-label={submitLabel} onSubmit={(e) => {
      e.preventDefault();
      const p = integrationProblem(f, { creating });
      setProblem(p);
      if (!p) onSubmit(body(f));
    }}>
      <ErrorBox>{problem}</ErrorBox>
      <TextField label="Name" value={f.name} onChange={set("name")} maxLength={100} />
      <TextField label="Base URL" value={f.base_url} onChange={set("base_url")} placeholder="https://api.example.com/v1" />
      <SelectField label="Authentication" value={f.auth_type} onChange={set("auth_type")} options={AUTH_TYPES} />
      {f.auth_type === "api_key_header" ? <TextField label="Header name" value={f.auth_header_name} onChange={set("auth_header_name")} placeholder="X-API-Key" /> : null}
      {f.auth_type === "api_key_query" ? <TextField label="Query parameter name" value={f.auth_query_param} onChange={set("auth_query_param")} placeholder="api_key" /> : null}
      {f.auth_type !== "none" ? (
        <TextField label={creating ? "API key or token" : "Replace API key or token (optional)"} type="password" autoComplete="off"
          value={f.secret} onChange={set("secret")} hint="Stored encrypted as an Organisation credential; it is never shown again and is sent only to this base URL." />
      ) : null}
      <SelectField label="Documentation" value={f.doc_source_type} onChange={set("doc_source_type")} options={DOC_TYPES} />
      {f.doc_source_type !== "manual" ? (
        <TextField label="Documentation URL (optional)" value={f.doc_source_url} onChange={set("doc_source_url")}
          placeholder="https://api.example.com/openapi.json" hint="Or import an OpenAPI JSON file after saving." />
      ) : null}
      <Button size="sm" type="submit" disabled={busy}>{submitLabel}</Button>
    </form>
  );
}

function EndpointTester({ api, integrationId, endpoint, onDone }) {
  const params = (endpoint.parameters || []);
  const [values, setValues] = useState({});
  const { busy, error, run } = useAction();
  const [result, setResult] = useState(null);
  return (
    <div className="mt-2 rounded border border-white/10 bg-[#050B14] p-3" aria-label={`Test ${endpoint.path}`}>
      <ErrorBox>{error}</ErrorBox>
      {params.map((p) => (
        <TextField key={p.name} label={`${p.name} (${p.in}${p.required ? ", required" : ""})`} value={values[p.name] ?? ""}
          onChange={(v) => setValues({ ...values, [p.name]: v })} hint={p.description || undefined} />
      ))}
      <div className="flex gap-2">
        <Button size="sm" type="button" disabled={busy} onClick={() => run(async () => {
          const filled = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ""));
          setResult(await api.testApiEndpoint(integrationId, endpoint.id, filled)); if (onDone) await onDone();
        })}>Send test request</Button>
      </div>
      {result ? (
        <div className="mt-2 text-xs">
          <Badge tone={result.success ? "ok" : "error"}>{result.success ? `HTTP ${result.status}` : "Failed"}</Badge>{" "}
          <span className="text-slate-400">{result.duration_ms} ms</span>
          {result.error ? <div role="alert" className="text-red-300 mt-1">{result.error}</div> : null}
          {result.success ? <pre className="whitespace-pre-wrap text-slate-200 max-h-64 overflow-auto mt-1">{typeof result.data === "string" ? result.data : JSON.stringify(result.data, null, 2)}</pre> : null}
        </div>
      ) : null}
    </div>
  );
}

export function ApiIntegrationDetail({ api, integrationId, canManage, onChanged, onDeleted }) {
  const integ = useLoad(() => api.apiIntegration(integrationId), [api, integrationId]);
  const eps = useLoad(() => api.apiEndpoints(integrationId), [api, integrationId]);
  const activity = useLoad(() => api.apiActivity(integrationId), [api, integrationId]);
  const stats = useLoad(() => api.apiStats(integrationId), [api, integrationId]);
  const { busy, error, notice, run } = useAction();
  const [testing, setTesting] = useState(null);
  const [conn, setConn] = useState(null);
  const [editing, setEditing] = useState(false);
  const [manual, setManual] = useState({ path: "", summary: "" });
  const reloadAll = async () => { await Promise.all([integ.reload(), eps.reload(), activity.reload(), stats.reload()]); if (onChanged) await onChanged(); };
  if (integ.loading && !integ.data) return <Loading />;
  if (integ.error) return <ErrorBox error={integ.error} />;
  const d = integ.data;
  const rows = (eps.data && eps.data.endpoints) || [];
  const importFile = (file) => run(async () => {
    if (!file) throw new Error("Choose a JSON file.");
    if (file.size > 2 * 1024 * 1024) throw new Error("The file is larger than 2 MB.");
    let doc;
    try { doc = JSON.parse(await file.text()); } catch (e) { throw new Error("The file is not valid JSON (YAML is not supported; convert it to JSON)."); }
    const r = await api.discoverApi(integrationId, doc);
    await reloadAll();
    return r;
  }, (r) => `OpenAPI imported: ${r.new} new, ${r.updated} updated endpoints.`);
  const s = stats.data || {};
  return (
    <Section title={d.name} description={`${d.base_url} · ${(AUTH_TYPES.find(([k]) => k === d.auth_type) || [, d.auth_type])[1]}`}>
      <ErrorBox>{error}</ErrorBox>
      <Notice tone="ok">{notice}</Notice>
      <KeyValues rows={[
        ["Status", d.enabled ? "Enabled" : "Disabled"],
        ["Credential", d.credential_id ? "Stored (encrypted)" : (d.auth_type === "none" ? "Not needed" : "Missing")],
        ["Endpoints", `${d.enabled_endpoint_count || 0} enabled of ${d.endpoint_count || 0}`],
        ["Last connection test", d.last_connection_test ? `${d.last_connection_test.success ? "OK" : "Failed"} — ${d.last_connection_test.message || ""} (${fmtDate(d.last_connection_test.at)})` : "Never"],
        ["Last discovery", fmtDate(d.last_doc_sync)],
        ["Calls (recent)", s.calls === undefined ? "—" : `${s.calls} (${s.succeeded} ok, ${s.failed} failed${s.average_duration_ms ? `, avg ${s.average_duration_ms} ms` : ""})`],
      ]} />
      {canManage ? (
        <div className="flex flex-wrap gap-2 my-3 items-center">
          <Button size="sm" type="button" disabled={busy} onClick={() => run(async () => { setConn(await api.testApiConnection(integrationId)); await reloadAll(); })}>Test connection</Button>
          <Button size="sm" variant="outline" type="button" disabled={busy || !d.doc_source_url} onClick={() => run(async () => {
            const r = await api.discoverApi(integrationId); await reloadAll(); return r; }, (r) => `Endpoints discovered: ${r.new} new, ${r.updated} updated.`)}>Discover endpoints</Button>
          <label className="text-xs text-slate-300">Import OpenAPI (JSON){" "}
            <input type="file" accept=".json,application/json" aria-label="Import OpenAPI JSON file" onChange={(e) => importFile(e.target.files[0])} className="text-xs" />
          </label>
          <Button size="sm" variant="ghost" type="button" onClick={() => setEditing(!editing)}>{editing ? "Close settings" : "Edit settings"}</Button>
          <Button size="sm" variant="ghost" type="button" onClick={() => run(async () => { await api.patchApiIntegration(integrationId, { enabled: !d.enabled }); await reloadAll(); },
            d.enabled ? "Integration disabled." : "Integration enabled.")}>{d.enabled ? "Disable" : "Enable"}</Button>
          <ConfirmButton confirmLabel="Delete integration" onConfirm={() => run(async () => { await api.deleteApiIntegration(integrationId); if (onDeleted) await onDeleted(); })}>Delete</ConfirmButton>
        </div>
      ) : null}
      {conn ? (
        <div role="status" className="text-sm mb-3">
          <Badge tone={conn.success ? "ok" : "error"}>{conn.success ? "Reachable" : "Not reachable"}</Badge> {conn.message}
        </div>
      ) : null}
      {editing && canManage ? (
        <IntegrationForm initial={d} creating={false} busy={busy} submitLabel="Save settings"
          onSubmit={(b) => run(async () => { await api.patchApiIntegration(integrationId, b); setEditing(false); await reloadAll(); }, "Settings saved.")} />
      ) : null}
      <h4 className="text-sm font-semibold text-white mt-4 mb-1">Endpoints</h4>
      {canManage && rows.length ? (
        <div className="flex gap-2 mb-2">
          <Button size="sm" variant="ghost" type="button" onClick={() => run(async () => { await api.bulkApiEndpoints(integrationId, { enabled: true }); await reloadAll(); }, "All GET endpoints enabled.")}>Enable all</Button>
          <Button size="sm" variant="ghost" type="button" onClick={() => run(async () => { await api.bulkApiEndpoints(integrationId, { enabled: false }); await reloadAll(); }, "All endpoints disabled.")}>Disable all</Button>
        </div>
      ) : null}
      {eps.loading ? <Loading /> : <ErrorBox error={eps.error} />}
      <DataTable caption="Endpoints" rows={rows} empty="No endpoints yet. Discover them from the documentation, import an OpenAPI file or add one manually." columns={[
        { key: "path", label: "Endpoint", render: (e) => (
          <span><code className="text-cyan-200">{e.method} {e.path}</code>
            {e.summary ? <span className="block text-xs text-slate-400">{e.summary}</span> : null}</span>) },
        { key: "category", label: "Category" },
        { key: "enabled", label: "Used by the bot", render: (e) => e.executable ? (
          <input type="checkbox" aria-label={`Enable ${e.method} ${e.path}`} checked={Boolean(e.enabled)} disabled={!canManage || busy}
            onChange={() => run(async () => { await api.patchApiEndpoint(integrationId, e.id, { enabled: !e.enabled }); await reloadAll(); })} />
        ) : <span className="text-xs text-slate-500">read-only calls only</span> },
        { key: "x", label: "", render: (e) => canManage && e.executable ? (
          <Button size="sm" variant="ghost" type="button" onClick={() => setTesting(testing === e.id ? null : e.id)}>Test endpoint</Button>) : null },
      ]} />
      {testing ? <EndpointTester api={api} integrationId={integrationId} endpoint={rows.find((e) => e.id === testing) || { id: testing }} onDone={() => Promise.all([activity.reload(), stats.reload()])} /> : null}
      {canManage ? (
        <form className="mt-3 flex flex-wrap gap-2 items-end" onSubmit={(e) => { e.preventDefault(); run(async () => {
          await api.addApiEndpoint(integrationId, { path: manual.path.trim(), summary: manual.summary.trim() });
          setManual({ path: "", summary: "" }); await reloadAll(); }, "Endpoint added."); }}>
          <TextField label="Add GET endpoint (path)" value={manual.path} onChange={(v) => setManual({ ...manual, path: v })} placeholder="/orders/{order_id}" />
          <TextField label="What it returns" value={manual.summary} onChange={(v) => setManual({ ...manual, summary: v })} />
          <Button size="sm" type="submit" disabled={busy || !manual.path.trim().startsWith("/")}>Add endpoint</Button>
        </form>
      ) : null}
      <h4 className="text-sm font-semibold text-white mt-4 mb-1">Activity</h4>
      <DataTable caption="API activity" rows={(activity.data && activity.data.activity) || []} empty="No API calls yet." columns={[
        { key: "at", label: "Time", render: (r) => fmtDate(r.at) },
        { key: "source", label: "Source", render: (r) => ({ conversation: "Conversation", test: "Endpoint test", connection_test: "Connection test" }[r.source] || r.source) },
        { key: "endpoint", label: "Endpoint" },
        { key: "status", label: "Result", render: (r) => <Badge tone={r.success ? "ok" : "error"}>{r.status ? `HTTP ${r.status}` : "Failed"}</Badge> },
        { key: "duration_ms", label: "Duration", render: (r) => `${r.duration_ms} ms` },
        { key: "error", label: "Error", render: (r) => r.error || "" },
      ]} />
    </Section>
  );
}

export default function ApiIntegrationsPanel({ api, botId, perms, cfg, onConfigChanged }) {
  const list = useLoad(() => api.apiIntegrations(botId), [api, botId]);
  const { busy, error, notice, run } = useAction();
  const [selected, setSelected] = useState(null);
  const [creating, setCreating] = useState(false);
  const rows = (list.data && list.data.integrations) || [];
  const enabled = Boolean(cfg && cfg.values && cfg.values["tools.api_calls_enabled"]);
  const mode = cfg && cfg.values && cfg.values["knowledge.mode"];
  return (
    <>
      <Section title="API integrations" description="Connect external APIs so the bot can look up live data (read-only GET requests) while answering. API data is treated as untrusted and is never used in Strict Knowledge Only mode.">
        <ErrorBox>{error}</ErrorBox>
        <Notice tone="ok">{notice}</Notice>
        {perms.edit_config ? (
          <CheckField label="Use API data in conversations" checked={enabled} disabled={busy}
            hint={mode === "strict_knowledge_only" ? "This bot is in Strict Knowledge Only mode, so API calls are not made." : "Requires the Enterprise policy to allow external API calls."}
            onChange={(v) => run(async () => { await api.patchConfig("bot", botId, { "tools.api_calls_enabled": v }); if (onConfigChanged) await onConfigChanged(); },
              v ? "API calls enabled for this bot." : "API calls disabled for this bot.")} />
        ) : <p className="text-sm text-slate-300 mb-2">API data in conversations: {enabled ? "on" : "off"}</p>}
        {list.loading ? <Loading /> : <ErrorBox error={list.error} />}
        <DataTable caption="API integrations" rows={rows} empty="No API integrations yet." columns={[
          { key: "name", label: "Name", render: (r) => (
            <Button size="sm" variant="link" type="button" className="px-0 text-cyan-300" onClick={() => setSelected(r.id)}>{r.name}</Button>) },
          { key: "base_url", label: "Base URL" },
          { key: "enabled", label: "Status", render: (r) => <Badge tone={r.enabled ? "ok" : "neutral"}>{r.enabled ? "enabled" : "disabled"}</Badge> },
          { key: "endpoints", label: "Endpoints", render: (r) => `${r.enabled_endpoint_count || 0} / ${r.endpoint_count || 0}` },
          { key: "test", label: "Last test", render: (r) => r.last_connection_test ? (r.last_connection_test.success ? "OK" : "Failed") : "—" },
        ]} />
        {perms.manage_channel ? (
          creating ? (
            <div className="mt-3">
              <h4 className="text-sm font-semibold text-white mb-2">Create API integration</h4>
              <IntegrationForm initial={{}} creating busy={busy} submitLabel="Create API integration"
                onSubmit={(b) => run(async () => { const r = await api.createApiIntegration(botId, b); setCreating(false); await list.reload(); setSelected(r.id); },
                  "API integration created. Discover or import its endpoints next.")} />
              <Button size="sm" variant="ghost" type="button" onClick={() => setCreating(false)}>Cancel</Button>
            </div>
          ) : <Button size="sm" className="mt-3" type="button" onClick={() => setCreating(true)}>Create API integration</Button>
        ) : null}
      </Section>
      {selected ? (
        <ApiIntegrationDetail key={selected} api={api} integrationId={selected} canManage={Boolean(perms.manage_channel)}
          onChanged={list.reload} onDeleted={async () => { setSelected(null); await list.reload(); }} />
      ) : null}
    </>
  );
}
