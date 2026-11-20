/**
 * AI provider, model and API credential (Stage 0). Users pick saved credentials by
 * name; nobody types internal ids. Authorised users can add a key inline (it goes
 * to the existing Enterprise/Organisation credential store) and run a real
 * connection test. Minimal UI on purpose: Emergent owns the final design; the
 * server decides what is allowed and usable (GET/PUT /{type}/{id}/ai).
 */
import React, { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { ErrorBox, KeyValues, Loading, Notice, Section, SelectField, TextField } from "./common";
import { sourceLabel, useAction, useLoad } from "../util";

export const ADD_NEW = "__add_new__";

/** "Support key — Acme Ltd (Organisation)"; platform keys are labelled as shared. */
export function credentialLabel(c) {
  const owner = c.owner === "platform" ? "shared by BitBotPro" : `${c.owner_name || ""} (${c.owner === "organisation" ? "Organisation" : "Enterprise"})`.trim();
  return `${c.name} — ${owner}`;
}

export function modelOptions(provider) {
  if (!provider) return [];
  return [["", `Provider default (${(provider.models.find((m) => m.id === provider.default_model) || {}).label || provider.default_model})`],
    ...provider.models.filter((m) => m.allowed).map((m) => [m.id, m.label])];
}

/** The draft starts from the stored overrides when they are set here, else from the effective values. */
function draftFrom(d) {
  const set = (k) => d.current.sources && d.current.sources[k] && d.current.sources[k].kind === "override" &&
    d.current.sources[k].object_id === d.target.id;
  return { provider: d.current.provider || "", model: set("ai.model") ? d.current.model : "",
    credential_id: d.current.credential_id || "" };
}

function AddCredential({ api, owners, provider, onCreated, onCancel }) {
  const [owner, setOwner] = useState(owners[0] ? `${owners[0].type}:${owners[0].id}` : "");
  const [name, setName] = useState("");
  const [secret, setSecret] = useState("");
  const { busy, error, run } = useAction();
  const save = (e) => {
    e.preventDefault();
    const [type, id] = owner.split(":");
    run(async () => {
      const created = await api.createCredential(type === "enterprises" ? "enterprise" : "organisation", id,
        { provider: provider.id, name: name.trim(), secret: secret.trim() });
      setSecret("");
      await onCreated(created);
    });
    setSecret("");
  };
  return (
    <form aria-label="Add AI credential" className="mt-3 rounded-md border border-white/10 p-3 max-w-md" autoComplete="off" onSubmit={save}>
      <h4 className="text-sm font-semibold text-white mb-1">Add a {provider.label} API key</h4>
      <ErrorBox>{error}</ErrorBox>
      <SelectField label="Save the key for" value={owner} onChange={setOwner}
        options={owners.map((o) => [`${o.type}:${o.id}`, `${o.name} (${o.type === "enterprises" ? "Enterprise" : "Organisation"})`])}
        hint="Enterprise keys can be used by all its Organisations; Organisation keys only inside that Organisation." />
      <TextField label="Credential name" value={name} onChange={setName} required maxLength={100} placeholder="e.g. Support team Claude key" />
      <TextField label="API key" type="password" autoComplete="new-password" value={secret} onChange={setSecret} required
        hint="Stored encrypted. It is never shown again; only a fingerprint is kept for reference." />
      <div className="flex gap-2">
        <Button size="sm" type="submit" disabled={busy || !name.trim() || !secret.trim()}>Save key</Button>
        <Button size="sm" variant="ghost" type="button" onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}

export default function AiSettingsPanel({ api, type, id, onChanged }) {
  const opts = useLoad(() => api.aiSettings(type, id), [api, type, id]);
  const { busy, error, notice, run, setNotice, setError } = useAction();
  const [draft, setDraft] = useState(null);
  const [adding, setAdding] = useState(false);
  const [test, setTest] = useState(null);
  const pick = useRef(null);                       // a key just added: select it after the reload
  const d = opts.data;
  useEffect(() => {
    if (!d) return;
    const next = draftFrom(d);
    if (pick.current) { if (next.provider !== pick.current.provider) next.model = ""; next.provider = pick.current.provider; next.credential_id = pick.current.id; pick.current = null; }
    setDraft(next);
  }, [d]);
  if (opts.loading && !d) return <Section title="AI provider and model"><Loading /></Section>;
  if (!d || !draft) return <Section title="AI provider and model"><ErrorBox error={opts.error} /></Section>;

  const provider = d.providers.find((p) => p.id === draft.provider);
  const creds = d.credentials.filter((c) => c.provider === draft.provider);
  const current = d.current;
  const providerLabel = (pid) => (d.providers.find((p) => p.id === pid) || {}).label || pid || "—";
  const modelLabel = (pid, mid) => (((d.providers.find((p) => p.id === pid) || {}).models || []).find((m) => m.id === mid) || {}).label || mid || "—";
  const change = (k, v) => {
    setTest(null); setNotice(""); setError("");
    if (k === "credential_id" && v === ADD_NEW) { setAdding(true); return; }
    setDraft((x) => (k === "provider" ? { provider: v, model: "", credential_id: "" } : { ...x, [k]: v }));
  };
  const save = (e) => {
    e.preventDefault();
    run(async () => {
      await api.putAiSettings(type, id, { provider: draft.provider, model: draft.model || null,
        credential_id: draft.credential_id || null });
      await opts.reload();
      if (onChanged) await onChanged();
    }, "AI settings saved.");
  };
  const runTest = () => run(async () => {
    setTest(null);
    const r = await api.testAi(type, id, { provider: draft.provider, model: draft.model || undefined,
      credential_id: draft.credential_id || undefined });
    setTest(r);
  });
  const credOptions = [["", creds.length ? "Choose a saved key…" : "No saved key for this provider yet"],
    ...creds.map((c) => [c.id, credentialLabel(c)]),
    ...(d.can_add_credential_to.length ? [[ADD_NEW, "+ Add a new API key…"]] : [])];

  return (
    <Section title="AI provider and model" description="Choose which AI service answers for this object, the model, and the saved API key it uses. Children inherit these settings unless they choose their own.">
      <KeyValues rows={[
        ["Provider", `${providerLabel(current.provider)} · ${sourceLabel(current.sources["ai.provider"], d.target.id)}`],
        ["Model", `${modelLabel(current.provider, current.model)} · ${sourceLabel(current.sources["ai.model"], d.target.id)}`],
        ["API key", current.credential ? `${credentialLabel(current.credential)} · ${sourceLabel(current.sources["ai.credential_id"], d.target.id)}` : "None selected"],
      ]} />
      {current.blocked ? <Notice tone="warn">Not ready to answer: {current.blocked}. Choose an API key below.</Notice> : null}
      <ErrorBox>{error}</ErrorBox>
      <Notice tone="ok">{notice}</Notice>
      {d.can_edit ? (
        <form aria-label="AI settings" className="max-w-md mt-3" onSubmit={save}>
          <SelectField label="AI provider" value={draft.provider} onChange={(v) => change("provider", v)}
            options={d.providers.filter((p) => p.allowed).map((p) => [p.id, p.label])} />
          <SelectField label="Model" value={draft.model} onChange={(v) => change("model", v)} options={modelOptions(provider)} />
          <SelectField label="API key" value={draft.credential_id} onChange={(v) => change("credential_id", v)} options={credOptions}
            hint={d.can_add_credential_to.length ? "" : "Ask an Enterprise or Organisation admin to add an API key if none is listed."} />
          <div className="flex flex-wrap gap-2 mt-2">
            <Button size="sm" type="submit" disabled={busy || !draft.provider}>Save AI settings</Button>
            <Button size="sm" variant="outline" type="button" disabled={busy || !draft.credential_id} onClick={runTest}>Test connection</Button>
          </div>
          <p className="text-xs text-slate-400 mt-1">The test sends one very short real request to the provider with the choices above (a few tokens).</p>
        </form>
      ) : <p className="text-sm text-slate-400 mt-2">You can view these settings; an editor or admin can change them.</p>}
      {test ? (
        <div role="status" className="mt-2">
          <Notice tone={test.ok ? "ok" : "warn"}>{test.message}{test.ok && test.reply ? ` Reply: “${test.reply}” (${test.latency_ms} ms).` : ""}</Notice>
        </div>
      ) : null}
      {adding && provider ? (
        <AddCredential api={api} owners={d.can_add_credential_to} provider={provider} onCancel={() => setAdding(false)}
          onCreated={async (c) => { setAdding(false); pick.current = c; await opts.reload();
            setNotice("API key saved. Select Save AI settings to use it here, or Test connection first."); }} />
      ) : null}
    </Section>
  );
}
