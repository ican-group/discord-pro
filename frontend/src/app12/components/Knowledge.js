/**
 * KnowledgePanel (C11): sources owned by an Enterprise or Organisation,
 * Enterprise approval (policy `knowledge.allowed_sources`), document upload,
 * website sources (single page or bounded same-site crawl, refresh; restoration R1/R2),
 * processing status, versions, validity, ACLs, reprocess and deletion.
 * Fetching, retrieval, access filtering and evidence validation happen only in the API.
 */
import React, { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge, ConfirmButton, DataTable, ErrorBox, Loading, Notice, Section, SelectField, StateBadge, TextField } from "./common";
import { fmtDate, useAction, useLoad } from "../util";

export const ACCEPT = ".txt,.md,.markdown,.html,.htm,.csv,.json,.docx,.pdf";
export const MAX_UPLOAD = 2 * 1024 * 1024;

export function uploadProblem(file) {
  if (!file) return "Choose a file.";
  if (file.size > MAX_UPLOAD) return "Files are limited to 2 MB.";
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  if (!ACCEPT.split(",").includes(`.${ext}`)) return "Supported: text, Markdown, HTML, CSV, JSON, DOCX and PDF (text-based).";
  return "";
}

export const MAX_CRAWL_PAGES = 200;

/** Client-side check of the "Add website" form (the API validates again). */
export function websiteProblem({ name, url, mode, maxPages }) {
  if (!name || !name.trim()) return "Enter a name for the source.";
  const u = (url || "").trim();
  if (!u) return "Enter the website address.";
  if (/^[a-z][a-z0-9+.-]*:/i.test(u) && !/^https?:\/\//i.test(u)) return "Only http(s) website addresses are supported.";
  if (mode === "crawl") {
    const n = Number(maxPages);
    if (!Number.isInteger(n) || n < 1 || n > MAX_CRAWL_PAGES) return `The page limit must be between 1 and ${MAX_CRAWL_PAGES}.`;
  }
  return "";
}

const RUN_LABEL = { queued: "Queued", running: "Importing…", done: "Imported", failed: "Failed" };

function WebsiteStatus({ w }) {
  if (!w) return null;
  const tone = { done: "ok", failed: "error", running: "info", queued: "info" }[w.state] || "neutral";
  return (
    <div className="text-xs text-slate-300 mt-1 space-y-0.5">
      <div className="flex flex-wrap gap-2 items-center">
        <Badge tone={tone}>{RUN_LABEL[w.state] || w.state}</Badge>
        <a href={w.url} target="_blank" rel="noopener noreferrer" className="text-cyan-300 break-all">{w.url}</a>
        <span className="text-slate-400">{w.mode === "crawl" ? `Crawl, up to ${w.max_pages} pages` : "Single page"}</span>
      </div>
      {w.state === "running" ? <div>Progress: {w.pages_done} of {w.pages_found || "?"} pages, {w.pages_imported} imported</div> : null}
      {w.state === "done" || w.state === "failed" ? (
        <div>{w.pages_imported} page{w.pages_imported === 1 ? "" : "s"} imported{w.pages_failed ? `, ${w.pages_failed} failed` : ""}</div>
      ) : null}
      <div className="text-slate-400">Last successful fetch: {w.last_success_at ? fmtDate(w.last_success_at) : "never"}</div>
      {w.error ? <div role="alert" className="text-red-300">{w.error}</div> : null}
    </div>
  );
}

function ContentViewer({ api, documentId, onClose }) {
  const content = useLoad(() => api.documentContent(documentId), [api, documentId]);
  return (
    <div className="mt-2 rounded border border-white/10 bg-[#050B14] p-2">
      <div className="flex justify-between items-center mb-1">
        <span className="text-xs text-slate-400">Imported text{content.data && content.data.truncated ? " (first 200,000 characters)" : ""}</span>
        <Button size="sm" variant="ghost" type="button" onClick={onClose}>Close</Button>
      </div>
      {content.loading ? <Loading /> : <ErrorBox error={content.error} />}
      {content.data ? <pre className="whitespace-pre-wrap text-xs text-slate-200 max-h-80 overflow-auto">{content.data.text || "(empty)"}</pre> : null}
    </div>
  );
}

function Documents({ api, source, canManage }) {
  const docs = useLoad(() => api.documents(source.id), [api, source.id]);
  const { busy, error, notice, run } = useAction();
  const [file, setFile] = useState(null);
  const [acl, setAcl] = useState({});
  const [viewing, setViewing] = useState(null);
  const isWebsite = source.kind === "website";
  const runState = source.website && source.website.state;
  // a website import finishing changes the pages: reload them when it does
  useEffect(() => { if (isWebsite && (runState === "done" || runState === "failed")) docs.reload(); },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [runState]);
  const upload = (e) => {
    e.preventDefault();
    const problem = uploadProblem(file);
    if (problem) return run(() => Promise.reject(new Error(problem)));
    return run(async () => { await api.uploadDocument(source.id, file.name, file); setFile(null); e.target.reset(); await docs.reload(); },
      "Uploaded. Processing runs in the background; refresh to see the status.");
  };
  const rows = (docs.data && docs.data.documents) || [];
  return (
    <div className="mt-2 ml-2 border-l border-white/10 pl-3">
      <ErrorBox>{error}</ErrorBox>
      <Notice tone="ok">{notice}</Notice>
      {docs.loading ? <Loading /> : <ErrorBox error={docs.error} />}
      <DataTable caption={`${isWebsite ? "Pages" : "Documents"} in ${source.name}`} rows={rows}
        empty={isWebsite ? "No pages imported yet." : "No documents yet."} columns={[
        { key: "name", label: isWebsite ? "Page" : "Document", render: (d) => d.url ? (
          <span>
            <a href={d.url} target="_blank" rel="noopener noreferrer" className="text-cyan-300 break-all">{d.url}</a>
            {d.fetched_at ? <span className="block text-xs text-slate-400">Fetched {fmtDate(d.fetched_at)}</span> : null}
          </span>) : d.name },
        { key: "status", label: "Status", render: (d) => (
          <span>
            <StateBadge state={d.status} />
            {d.job && d.job.error ? <span className="block text-xs text-red-300">{d.job.error}</span> : null}
          </span>) },
        { key: "content_version", label: "Version" },
        { key: "chunk_count", label: "Chunks", render: (d) => d.chunk_count ?? "—" },
        { key: "acl", label: "Access", render: (d) => {
          const a = d.acl || {};
          const parts = [...(a.organisations || []).map((x) => `org:${x.slice(0, 6)}…`), ...(a.bots || []).map((x) => `bot:${x.slice(0, 6)}…`)];
          return parts.length ? parts.join(", ") : "Whole source";
        } },
        { key: "updated_at", label: "Updated", render: (d) => fmtDate(d.updated_at) },
        { key: "x", label: "", render: (d) => canManage ? (
          <span className="flex flex-wrap gap-1 items-center">
            {d.status === "ready" ? <Button size="sm" variant="ghost" type="button" onClick={() => setViewing(viewing === d.id ? null : d.id)}>View content</Button> : null}
            <Button size="sm" variant="ghost" type="button" onClick={() => run(async () => { await api.reprocessDocument(d.id); await docs.reload(); }, "Reprocessing.")}>Reprocess</Button>
            <label className="sr-only" htmlFor={`acl-${d.id}`}>Restrict {d.name} to Organisation ids</label>
            <input id={`acl-${d.id}`} placeholder="org ids (comma)" value={acl[d.id] ?? ""} onChange={(e) => setAcl({ ...acl, [d.id]: e.target.value })}
              className="rounded border border-white/15 bg-[#050B14] px-2 py-1 text-xs text-white w-32" />
            <Button size="sm" variant="ghost" type="button" onClick={() => run(async () => {
              const organisations = (acl[d.id] || "").split(",").map((s) => s.trim()).filter(Boolean);
              await api.documentAcl(d.id, { organisations, bots: [] }); await docs.reload(); }, "Access updated.")}>Set access</Button>
            <ConfirmButton confirmLabel="Delete" onConfirm={() => run(async () => { await api.deleteDocument(d.id); await docs.reload(); }, "Document deleted.")}>Delete</ConfirmButton>
          </span>) : (d.status === "ready" ? <Button size="sm" variant="ghost" type="button" onClick={() => setViewing(viewing === d.id ? null : d.id)}>View content</Button> : null) },
      ]} />
      {viewing ? <ContentViewer api={api} documentId={viewing} onClose={() => setViewing(null)} /> : null}
      {canManage && !isWebsite ? (
        <form onSubmit={upload} className="mt-2 flex flex-wrap gap-2 items-end">
          <div>
            <label htmlFor={`up-${source.id}`} className="text-xs text-slate-400 block">Upload a document (max 2 MB)</label>
            <input id={`up-${source.id}`} type="file" accept={ACCEPT} onChange={(e) => setFile(e.target.files[0] || null)} className="text-sm text-slate-200" />
          </div>
          <Button size="sm" type="submit" disabled={busy || !file}>Upload</Button>
          <p className="text-xs text-slate-400 w-full">Scanned or image-only PDFs contain no text and need OCR, which is not supported; upload a text-based PDF or another format.</p>
        </form>
      ) : null}
    </div>
  );
}

function AddWebsite({ api, type, ownerId, onCreated, busy, run }) {
  const [form, setForm] = useState({ name: "", url: "", mode: "single", maxPages: "25" });
  const set = (k) => (v) => setForm({ ...form, [k]: v });
  const submit = (e) => {
    e.preventDefault();
    const problem = websiteProblem(form);
    if (problem) return run(() => Promise.reject(new Error(problem)));
    const website = { url: form.url.trim(), mode: form.mode, ...(form.mode === "crawl" ? { max_pages: Number(form.maxPages) } : {}) };
    return run(async () => {
      await api.createSource(type, ownerId, { name: form.name.trim(), website });
      setForm({ name: "", url: "", mode: "single", maxPages: "25" });
      await onCreated();
    }, "Website added. The import runs in the background; its status appears below.");
  };
  return (
    <form className="mt-3 max-w-md" onSubmit={submit} aria-label="Add website">
      <h4 className="text-sm font-semibold text-white mb-2">Add website</h4>
      <TextField label="Website address" value={form.url} onChange={set("url")} placeholder="https://example.com/help" maxLength={2000} />
      <TextField label="Source name" value={form.name} onChange={set("name")} maxLength={120} />
      <SelectField label="Import" value={form.mode} onChange={set("mode")}
        options={[["single", "This page only"], ["crawl", "Crawl the site (same-site links and sitemap)"]]} />
      {form.mode === "crawl" ? (
        <TextField label="Page limit" type="number" min={1} max={MAX_CRAWL_PAGES} value={form.maxPages} onChange={set("maxPages")}
          hint={`Up to ${MAX_CRAWL_PAGES} pages. The crawler stays on this site and respects robots.txt.`} />
      ) : null}
      <Button size="sm" type="submit" disabled={busy}>Add website</Button>
    </form>
  );
}

export default function KnowledgePanel({ api, type, ownerId, canManage, approved, canApprove, onApprove }) {
  const sources = useLoad(() => api.sources(type, ownerId), [api, type, ownerId]);
  const { busy, error, notice, run } = useAction();
  const [name, setName] = useState("");
  const [open, setOpen] = useState({});
  const approvedSet = new Set(approved || []);
  const list = (sources.data && sources.data.sources) || [];
  const importing = list.some((s) => s.website && (s.website.state === "queued" || s.website.state === "running"));
  // while an import is queued or running, refresh the status quietly every 5 seconds
  useEffect(() => {
    if (!importing) return undefined;
    const t = setInterval(async () => {
      try { sources.setData(await api.sources(type, ownerId)); } catch (e) { /* keep the last status */ }
    }, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [importing, api, type, ownerId]);
  return (
    <Section title="Knowledge" description="Sources must be approved by the Enterprise before any bot can use them. Answers in knowledge modes cite only approved, accessible, currently valid documents.">
      <ErrorBox>{error}</ErrorBox>
      <Notice tone="ok">{notice}</Notice>
      {sources.loading ? <Loading /> : <ErrorBox error={sources.error} />}
      <ul>
        {list.map((s) => (
          <li key={s.id} className="py-2 border-b border-white/5">
            <div className="flex flex-wrap items-center gap-2 justify-between">
              <span>
                <strong className="text-white">{s.name}</strong> {s.kind === "website" ? <Badge tone="info">website</Badge> : null}{" "}
                <StateBadge state={s.status} />{" "}
                {approved ? (approvedSet.has(s.id) ? <StateBadge state="approved" /> : <span className="text-xs text-amber-300">Not approved for use</span>) : null}
              </span>
              <span className="flex flex-wrap gap-1">
                <Button size="sm" variant="ghost" type="button" aria-expanded={Boolean(open[s.id])}
                  onClick={() => setOpen({ ...open, [s.id]: !open[s.id] })}>{open[s.id] ? (s.kind === "website" ? "Hide pages" : "Hide documents") : (s.kind === "website" ? "Pages" : "Documents")}</Button>
                {canManage && s.kind === "website" ? (
                  <Button size="sm" variant="outline" type="button"
                    disabled={busy || s.status !== "active" || ["queued", "running"].includes(s.website && s.website.state)}
                    onClick={() => run(async () => { await api.refreshSource(s.id); await sources.reload(); }, "Refresh queued.")}>Refresh</Button>
                ) : null}
                {canApprove ? (
                  <Button size="sm" variant="outline" type="button" disabled={busy} onClick={() => run(() => onApprove(s.id, !approvedSet.has(s.id)),
                    approvedSet.has(s.id) ? "Approval withdrawn." : "Source approved.")}>{approvedSet.has(s.id) ? "Withdraw approval" : "Approve"}</Button>
                ) : null}
                {canManage ? (
                  <>
                    <Button size="sm" variant="ghost" type="button" onClick={() => run(async () => {
                      await api.patchSource(s.id, { status: s.status === "active" ? "disabled" : "active" }); await sources.reload(); },
                    s.status === "active" ? "Source disabled." : "Source enabled.")}>{s.status === "active" ? "Disable" : "Enable"}</Button>
                    <ConfirmButton confirmLabel="Delete source" onConfirm={() => run(async () => { await api.deleteSource(s.id); await sources.reload(); }, "Source deleted.")}>Delete</ConfirmButton>
                  </>
                ) : null}
              </span>
            </div>
            {s.kind === "website" ? <WebsiteStatus w={s.website} /> : null}
            {open[s.id] ? <Documents api={api} source={s} canManage={canManage} /> : null}
          </li>
        ))}
      </ul>
      {canManage ? (
        <div className="flex flex-wrap gap-8">
          <form className="mt-3 max-w-sm" onSubmit={(e) => { e.preventDefault(); run(async () => { await api.createSource(type, ownerId, { name }); setName(""); await sources.reload(); }, "Source created."); }}>
            <h4 className="text-sm font-semibold text-white mb-2">Add document source</h4>
            <TextField label="New source name" value={name} onChange={setName} required maxLength={120} />
            <Button size="sm" type="submit" disabled={busy || !name.trim()}>Create source</Button>
          </form>
          <AddWebsite api={api} type={type} ownerId={ownerId} busy={busy} run={run} onCreated={sources.reload} />
        </div>
      ) : null}
    </Section>
  );
}
