import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import api from "@/utils/api";
import { T } from "@/theme";

const outcomes = {
  connected: "Zendesk authorized successfully.",
  cancelled: "Zendesk authorization was cancelled.",
  expired: "This authorization request expired. Select Connect to Zendesk to try again.",
  failed: "Zendesk couldn’t complete the connection. Check the OAuth client settings and try again.",
};

const reviewStyles = {
  needs_summary: { color: "#fecaca", borderColor: "#ef4444", label: "Needs summary" },
  summary_posted: { color: "#fed7aa", borderColor: "#f97316", label: "Summary posted" },
  solved: { color: "#bbf7d0", borderColor: "#22c55e", label: "Solved" },
  unavailable: { color: "#cbd5e1", borderColor: "#64748b", label: "Status unavailable" },
};

function ZendeskTicketHistory({ instanceId, ticket, canPostNotes, onSummaryPosted, onSavedToKb }) {
  const [history, setHistory] = useState(null);
  const [summary, setSummary] = useState(null);
  const [historyError, setHistoryError] = useState("");
  const [summaryError, setSummaryError] = useState("");
  const [posting, setPosting] = useState(false);
  const [posted, setPosted] = useState(false);
  const [postError, setPostError] = useState("");
  const [solution, setSolution] = useState("");
  const [savingSolution, setSavingSolution] = useState(false);
  const [solutionMessage, setSolutionMessage] = useState("");
  const [solutionError, setSolutionError] = useState("");
  const solved = ["solved", "closed"].includes(ticket.status);

  useEffect(() => {
    const controller = new AbortController();
    const options = { headers: { "X-Instance-ID": instanceId }, signal: controller.signal };
    const path = `/zendesk/tickets/${ticket.id}`;

    api.get(`${path}/history`, options).then(({ data }) => {
      if (controller.signal.aborted) return;
      setHistory(data.comments);
      api.post(`${path}/summary`, null, options).then(({ data: result }) => {
        if (!controller.signal.aborted) {
          setSummary(result);
          setSolution(result.solution || "");
        }
      }).catch((error) => {
        if (!controller.signal.aborted) setSummaryError(
          typeof error.response?.data?.detail === "string" ? error.response.data.detail : "Couldn’t summarize the ticket."
        );
      });
    }).catch(() => {
      if (!controller.signal.aborted) setHistoryError("Couldn’t load ticket history. Select the ticket again to retry.");
    });

    return () => controller.abort();
  }, [instanceId, ticket.id]);

  async function postSummary() {
    setPosting(true);
    setPostError("");
    const options = { headers: { "X-Instance-ID": instanceId } };
    const path = `/zendesk/tickets/${ticket.id}`;
    try {
      await api.post(`${path}/notes`, {
        body: summary.summary, summary: true, requester_comment_id: summary.requester_comment_id,
      }, options);
      setPosted(true);
      onSummaryPosted(ticket.id, solved ? "solved" : "summary_posted");
    } catch (error) {
      const detail = error.response?.data?.detail;
      setPostError(typeof detail === "string" ? detail : "Couldn’t confirm the summary was posted. Check Zendesk before retrying.");
      return;
    } finally {
      setPosting(false);
    }

    try {
      const { data } = await api.get(`${path}/history`, options);
      setHistory(data.comments);
    } catch {
      // Posting succeeded. Keep the conversation visible if refreshing it fails.
    }
  }

  async function saveSolution() {
    setSavingSolution(true);
    setSolutionMessage("");
    setSolutionError("");
    try {
      await api.post(`/zendesk/tickets/${ticket.id}/knowledge-source`, { solution }, {
        headers: { "X-Instance-ID": instanceId },
      });
      setSolutionMessage("Reviewed solution saved to the Knowledge Base.");
      onSavedToKb?.(ticket.id);
    } catch (error) {
      const detail = error.response?.data?.detail;
      setSolutionError(typeof detail === "string" ? detail : "Couldn’t save the reviewed solution.");
    } finally {
      setSavingSolution(false);
    }
  }

  return (
    <>
      <h4 style={T.sectionTitle}>Ticket history</h4>
      {historyError && <p role="alert">{historyError}</p>}
      {history === null && !historyError && <p role="status">Loading ticket history…</p>}
      {history?.length === 0 && <p style={T.sectionSub}>No comments on this ticket.</p>}
      {history?.length > 0 && (
        <ol style={{ display: "grid", gap: 12, padding: 0, listStyle: "none", maxHeight: "60vh", overflowY: "auto" }}>
          {history.map((comment) => (
            <li key={comment.id} style={{ ...T.card, padding: 12 }}>
              <p style={T.sectionSub}>
                {comment.public ? "Public comment" : "Internal note"}
                {comment.author_id && ` · Author #${comment.author_id}`}
                {comment.created_at && ` · ${new Date(comment.created_at).toLocaleString()}`}
              </p>
              <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{comment.body}</p>
              {comment.attachments.length > 0 &&
                <p style={{ ...T.sectionSub, overflowWrap: "anywhere" }}>Attachments: {comment.attachments.join(", ")}</p>}
            </li>
          ))}
        </ol>
      )}
      {history !== null && (
        <section aria-label="AI ticket summary" style={{
          ...T.card, marginTop: 20, marginBottom: 20, background: "#14263d",
          borderColor: "#365b86", borderLeft: "4px solid #7eb8ff", color: "#e4efff",
        }}>
          <h4 style={{ ...T.sectionTitle, color: "#a9d0ff" }}>AI ticket summary</h4>
          {summaryError && <p role="alert">{summaryError}</p>}
          {!summary && !summaryError && <p role="status">Summarizing ticket…</p>}
          {summary?.partial && <p style={T.sectionSub}>Summary based on the beginning and latest part of this long conversation.</p>}
          {summary && (
            <>
              <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{summary.summary}</p>
              <button type="button" style={{ ...T.btnPrimary, marginTop: 12 }}
                disabled={!canPostNotes || posting || posted || !summary.summary.trim()}
                onClick={postSummary}>
                {posted ? "Summary posted" : posting ? "Posting summary…" : "Post summary as internal note"}
              </button>
              {!canPostNotes && <p style={T.sectionSub}>Reconnect Zendesk to grant ticket write access.</p>}
              {posted && <p role="status">Summary posted as an internal note to ticket #{ticket.id}.</p>}
              {postError && <p role="alert">{postError}</p>}
            </>
          )}
        </section>
      )}
      {solved && summary && (
        <section style={{ ...T.card, marginBottom: 20 }}>
          <h4 style={T.sectionTitle}>Solution</h4>
          <label style={T.label}>Review or edit the solution before adding it to the Knowledge Base.
            <textarea style={T.textarea} rows={6} value={solution} disabled={savingSolution}
              onChange={(event) => { setSolution(event.target.value); setSolutionMessage(""); }} />
          </label>
          <button type="button" style={T.btnPrimary} disabled={savingSolution || !solution.trim()}
            onClick={saveSolution}>
            {savingSolution ? "Saving solution…" : "Add to KB"}
          </button>
          {solutionMessage && <p role="status">{solutionMessage}</p>}
          {solutionError && <p role="alert">{solutionError}</p>}
        </section>
      )}
    </>
  );
}

const SOLVED_STATUSES = ["solved", "closed"];

function SaveToKnowledgeBase({ instanceId, ticketId, onSaved }) {
  const [draft, setDraft] = useState(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const options = { headers: { "X-Instance-ID": instanceId } };

  async function run(action) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (requestError) {
      const detail = requestError.response?.data?.detail;
      setError(typeof detail === "string" ? detail : "Knowledge Base request failed.");
    } finally {
      setBusy(false);
    }
  }

  function prepare() {
    run(async () => {
      const { data } = await api.post(`/zendesk/tickets/${ticketId}/kb-draft`, null, options);
      setDraft({ title: data.title, content: data.content });
      setSaved(data.already_saved);
      if (data.already_saved) onSaved?.(ticketId);
    });
  }

  function save(event) {
    event.preventDefault();
    run(async () => {
      await api.post(`/zendesk/tickets/${ticketId}/kb`, draft, options);
      setSaved(true);
      onSaved?.(ticketId);
    });
  }

  return (
    <div style={{ marginTop: 20, display: "grid", gap: 12 }}>
      <h3 style={T.sectionTitle}>Save to Knowledge Base</h3>
      {error && <p role="alert">{error}</p>}
      {saved && (
        <p role="status">Ticket #{ticketId} is in the Knowledge Base. Manage it on the Knowledge Base page.</p>
      )}
      {!draft && (
        <button type="button" style={T.btnSecondary} onClick={prepare} disabled={busy}>
          {busy ? "Preparing…" : "Prepare entry"}
        </button>
      )}
      {draft && !saved && (
        <form onSubmit={save} style={{ display: "grid", gap: 12 }}>
          <p style={T.sectionSub}>
            Includes the customer’s question and public replies only. Internal notes are left out
            and email addresses are masked. Review and remove anything personal before saving:
            the bot can quote this to anyone.
          </p>
          <label style={T.label}>Title
            <input style={T.input} value={draft.title} maxLength={150} required disabled={busy}
              onChange={(e) => setDraft((d) => ({ ...d, title: e.target.value }))} />
          </label>
          <label style={T.label}>Content
            <textarea style={T.textarea} rows={12} value={draft.content} maxLength={20000} required
              disabled={busy} onChange={(e) => setDraft((d) => ({ ...d, content: e.target.value }))} />
          </label>
          <button type="submit" style={T.btnPrimary}
            disabled={busy || !draft.title.trim() || !draft.content.trim()}>
            {busy ? "Saving…" : "Save to Knowledge Base"}
          </button>
        </form>
      )}
    </div>
  );
}

function ZendeskTickets({ instanceId, canPostNotes }) {
  const [listMode, setListMode] = useState("open");
  const [queuePage, setQueuePage] = useState(1);
  const [queueRefresh, setQueueRefresh] = useState(0);
  const [queue, setQueue] = useState(null);
  const [queueError, setQueueError] = useState("");
  const [reviewStates, setReviewStates] = useState({});
  const [savedToKb, setSavedToKb] = useState({});
  const [ticketId, setTicketId] = useState("");
  const [ticket, setTicket] = useState(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const options = { headers: { "X-Instance-ID": instanceId } };

  useEffect(() => {
    const controller = new AbortController();
    const solved = listMode === "solved";
    api.get(solved ? "/zendesk/solved-tickets" : "/zendesk/open-tickets", {
      headers: { "X-Instance-ID": instanceId },
      params: solved ? undefined : { page: queuePage },
      signal: controller.signal,
    }).then(({ data }) => setQueue(data)).catch((requestError) => {
      if (controller.signal.aborted) return;
      const detail = requestError.response?.data?.detail;
      setQueueError(typeof detail === "string" ? detail
        : solved ? "Couldn’t load solved tickets." : "Couldn’t load open tickets.");
    });
    return () => controller.abort();
  }, [instanceId, listMode, queuePage, queueRefresh]);

  useEffect(() => {
    if (!queue?.tickets.length) return;
    const controller = new AbortController();
    // Solved/closed tickets are labelled from their status; only open ones need a history check.
    const ids = queue.tickets.filter(({ status }) => !["solved", "closed"].includes(status)).map(({ id }) => id);
    if (ids.length) {
      api.post("/zendesk/ticket-review-states", { ids }, {
        headers: { "X-Instance-ID": instanceId }, signal: controller.signal,
      }).then(({ data }) => {
        if (!controller.signal.aborted) setReviewStates(data.states);
      }).catch(() => {
        if (!controller.signal.aborted) setReviewStates(Object.fromEntries(ids.map((id) => [id, "unavailable"])));
      });
    }
    return () => controller.abort();
  }, [instanceId, queue]);

  function markSavedToKb(id) {
    setSavedToKb((current) => ({ ...current, [id]: true }));
  }

  function changeListMode(mode) {
    if (mode === listMode) return;
    setReviewStates({});
    setQueue(null);
    setQueueError("");
    setQueuePage(1);
    setListMode(mode);
  }

  function changeQueuePage(page) {
    setReviewStates({});
    setQueue(null);
    setQueueError("");
    setQueuePage(page);
    if (page === queuePage) setQueueRefresh((value) => value + 1);
  }

  async function run(action) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (requestError) {
      const detail = requestError.response?.data?.detail;
      setError(typeof detail === "string" ? detail : "Zendesk request failed.");
    } finally {
      setBusy(false);
    }
  }

  function loadTicket(id) {
    setTicketId(String(id));
    setTicket(null);
    setNote("");
    run(async () => {
      const { data } = await api.get(`/zendesk/tickets/${id}`, options);
      setTicket(data);
    });
  }

  function draftNote() {
    run(async () => {
      const { data } = await api.post(`/zendesk/tickets/${ticket.id}/draft`, null, options);
      setNote(data.draft);
    });
  }

  function postNote(event) {
    event.preventDefault();
    run(async () => {
      await api.post(`/zendesk/tickets/${ticket.id}/notes`, { body: note }, options);
      setNote("");
      setNotice(`Internal note posted to ticket #${ticket.id}.`);
    });
  }

  return (
    <div className="grid items-start gap-5 min-[950px]:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]" style={{ marginTop: 20 }}>
      <section style={{ ...T.card, minWidth: 0 }}>
      <h2 style={T.sectionTitle}>Zendesk tickets</h2>
      <div role="group" aria-label="Ticket list" style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
        {[["open", "Open"], ["solved", "Recently solved"]].map(([mode, label]) => (
          <button key={mode} type="button" aria-pressed={listMode === mode}
            style={listMode === mode ? T.btnPrimary : T.btnSecondary}
            onClick={() => changeListMode(mode)}>
            {label}
          </button>
        ))}
        <button type="button" style={T.btnSecondary} onClick={() => changeQueuePage(queuePage)}>
          Refresh
        </button>
      </div>
      {listMode === "solved" && queue && (
        <p style={T.sectionSub}>Last {queue.tickets.length} solved or closed tickets, most recently updated first.</p>
      )}
      {queueError && <p role="alert">{queueError}</p>}
      {!queue && !queueError && (
        <p role="status">{listMode === "solved" ? "Loading solved tickets…" : "Loading open tickets…"}</p>
      )}
      {queue && queue.tickets.length === 0 && (
        <p style={T.sectionSub}>{listMode === "solved" ? "No solved tickets found." : "No open tickets on this page."}</p>
      )}
      {queue?.tickets.length > 0 && (
        <ul style={{ display: "grid", gap: 8, paddingLeft: 0, listStyle: "none" }}>
          {queue.tickets.map(({ id, subject, status, in_kb: inKb }) => {
            const state = ["solved", "closed"].includes(status) ? "solved" : reviewStates[id];
            const appearance = reviewStyles[state];
            return (
              <li key={id}>
                <button type="button" style={{ ...T.btnSecondary, color: appearance?.color,
                  borderColor: appearance?.borderColor }} disabled={busy} onClick={() => loadTicket(id)}>
                  #{id} {subject || "Untitled ticket"}
                  {appearance && <span style={{ display: "block", fontSize: 11 }}>{appearance.label}</span>}
                  {(inKb || savedToKb[id]) && (
                    <span style={{ display: "inline-block", marginTop: 4, padding: "1px 6px", fontSize: 11,
                      color: "#00F5FF", border: "1px solid #00F5FF", borderRadius: 4 }}>Added to KB</span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {listMode === "open" && (queuePage > 1 || queue?.has_more) && (
        <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <button type="button" style={T.btnSecondary} disabled={queuePage === 1}
            onClick={() => changeQueuePage(queuePage - 1)}>Previous</button>
          <button type="button" style={T.btnSecondary} disabled={!queue?.has_more}
            onClick={() => changeQueuePage(queuePage + 1)}>Next</button>
        </div>
      )}
      <form onSubmit={(event) => { event.preventDefault(); loadTicket(ticketId.trim()); }} style={{ display: "grid", gap: 12 }}>
        <label style={T.label}>Ticket ID
          <input style={T.input} value={ticketId} onChange={(e) => setTicketId(e.target.value)}
            inputMode="numeric" pattern="[1-9][0-9]*" required disabled={busy} />
        </label>
        <button type="submit" style={T.btnPrimary} disabled={busy}>Read ticket</button>
      </form>
      </section>
      <section aria-label="Selected Zendesk ticket" style={{ ...T.card, minWidth: 0 }}>
        {!ticket && !busy && !error && <p style={T.sectionSub}>Select a ticket to view its conversation.</p>}
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {ticket && (
        <div style={{ marginTop: 20 }}>
          <h3 style={T.sectionTitle}>#{ticket.id} {ticket.subject}</h3>
          <p style={T.sectionSub}>{ticket.status}</p>
          <p style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{ticket.description}</p>
          <ZendeskTicketHistory key={ticket.id} instanceId={instanceId} ticket={ticket}
            canPostNotes={canPostNotes}
            onSummaryPosted={(id, state) => setReviewStates((current) => ({ ...current, [id]: state }))}
            onSavedToKb={markSavedToKb} />
          {SOLVED_STATUSES.includes(ticket.status) && (
            <SaveToKnowledgeBase key={ticket.id} instanceId={instanceId} ticketId={ticket.id}
              onSaved={markSavedToKb} />
          )}
          <form onSubmit={postNote} style={{ display: "grid", gap: 12 }}>
            <button type="button" style={T.btnPrimary} onClick={draftNote} disabled={busy}>
              Suggest solution or follow-up questions
            </button>
            <label style={T.label}>Internal note
              <textarea style={T.textarea} rows={7} value={note}
                onChange={(e) => setNote(e.target.value)} disabled={busy} required />
            </label>
            {!canPostNotes && <p style={T.sectionSub}>Reconnect Zendesk to grant ticket write access.</p>}
            <button type="submit" style={T.btnPrimary}
              disabled={busy || !canPostNotes || !note.trim()}>
              Post internal note to ticket #{ticket.id}
            </button>
          </form>
        </div>
      )}
      </section>
    </div>
  );
}

function ZendeskConnection({ instanceId }) {
  const [params] = useSearchParams();
  const [status, setStatus] = useState(null);
  const [error, setError] = useState("");
  const [subdomain, setSubdomain] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    api.get("/zendesk", {
      headers: { "X-Instance-ID": instanceId }, signal: controller.signal,
    }).then(({ data }) => {
      setStatus(data);
      setSubdomain(data.subdomain || "");
    }).catch(() => {
      if (!controller.signal.aborted) setError("Couldn’t load the Zendesk connection.");
    });
    return () => controller.abort();
  }, [instanceId]);

  async function connect(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const { data } = await api.post("/zendesk/oauth/start", {
        subdomain, client_id: clientId, client_secret: clientSecret,
      }, { headers: { "X-Instance-ID": instanceId } });
      window.location.assign(data.authorization_url);
    } catch (requestError) {
      const detail = requestError.response?.data?.detail;
      setError(typeof detail === "string" ? detail : "Couldn’t start Zendesk authorization.");
      setBusy(false);
    }
  }

  return (
    <>
      <div style={{ ...T.card, maxWidth: 640 }}>
        {status?.connected && <p style={T.sectionSub}>Connected to {status.subdomain}.zendesk.com</p>}
        {status?.expired && <p style={T.sectionSub}>The access token expired. Reconnect to authorize again.</p>}
        {outcomes[params.get("oauth")] && <p role="status">{outcomes[params.get("oauth")]}</p>}
        {error && <p role="alert">{error}</p>}
        {status && <>
          <p style={T.sectionSub}>Add this redirect URL to your Zendesk OAuth client.</p>
          <code style={{ overflowWrap: "anywhere" }}>{status.redirect_uri}</code>
          <form onSubmit={connect} style={{ display: "grid", gap: 12, marginTop: 20 }}>
            <label style={T.label}>Zendesk subdomain
              <input style={T.input} value={subdomain} onChange={(e) => setSubdomain(e.target.value)}
                placeholder="midnight-gd" autoComplete="off" required />
            </label>
            <label style={T.label}>OAuth client identifier
              <input style={T.input} value={clientId} onChange={(e) => setClientId(e.target.value)}
                autoComplete="off" required />
            </label>
            <label style={T.label}>OAuth client secret
              <input style={T.input} type="password" value={clientSecret}
                onChange={(e) => setClientSecret(e.target.value)} autoComplete="off" required />
            </label>
            <button type="submit" style={T.btnPrimary} disabled={busy}>
              {busy ? "Opening Zendesk…" : status.connected ? "Reconnect Zendesk" : "Connect to Zendesk"}
            </button>
          </form>
        </>}
      </div>
      {status?.connected && <ZendeskTickets instanceId={instanceId} canPostNotes={status.can_post_notes} />}
    </>
  );
}

export default function Zendesk() {
  const { selectedInstance } = useAuth();
  return (
    <div>
      <h1 style={T.h1}>Zendesk</h1>
      {selectedInstance && <ZendeskConnection key={selectedInstance.id} instanceId={selectedInstance.id} />}
    </div>
  );
}
