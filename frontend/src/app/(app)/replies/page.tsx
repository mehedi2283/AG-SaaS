"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { api, errorMessage, type Intent, type Reply, type ReplyPage } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

type View = "waiting" | "scheduled" | "sent" | "all";

const VIEWS: { key: View; label: string }[] = [
  { key: "waiting", label: "To answer" },
  { key: "scheduled", label: "Scheduled" },
  { key: "sent", label: "Sent" },
  { key: "all", label: "All" },
];

const INTENT_LABEL: Record<Intent, string> = {
  interested: "Interested",
  question: "Question",
  not_interested: "Not interested",
  referral: "Referral",
  out_of_office: "Out of office",
  unsubscribe: "Unsubscribe",
  bounce: "Bounce",
  other: "Other",
};

function when(iso: string): string {
  const date = new Date(iso);
  const sameDay = date.toDateString() === new Date().toDateString();
  return sameDay
    ? date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
    : date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export default function RepliesPage() {
  const { toast } = useWorkspace();
  const [view, setView] = useState<View>("waiting");
  const [page, setPage] = useState<ReplyPage | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await api<ReplyPage>(`/v1/replies?view=${view}&limit=100`);
      setPage(next);
      setSelected((current) => (current && next.items.some((r) => r.id === current) ? current : next.items[0]?.id ?? null));
    } catch (e) {
      toast(errorMessage(e), "error");
    }
  }, [view, toast]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 20_000); // new replies arrive by webhook
    return () => window.clearInterval(timer);
  }, [load]);

  const reply = page?.items.find((r) => r.id === selected) ?? null;

  return (
    <div className="desk">
      <aside className="desk-list" aria-label="Replies">
        <header className="desk-head">
          <h1 className="title">Replies</h1>
          <div className="tabs tabs-compact" role="tablist">
            {VIEWS.map((v) => (
              <button key={v.key} role="tab" type="button" className="tab" aria-selected={view === v.key} onClick={() => setView(v.key)}>
                {v.label}
                {v.key === "waiting" && page && page.waiting > 0 && <span className="tab-count num">{page.waiting}</span>}
              </button>
            ))}
          </div>
        </header>
        {page && page.items.length === 0 && (
          <div className="desk-empty">
            <p className="letter">{view === "waiting" ? "Nothing to answer right now." : "Nothing here yet."}</p>
            <p className="muted small">
              Replies from buyers arrive here within a minute. Gazi sorts each one and drafts an answer for you.
            </p>
          </div>
        )}
        <ul className="reply-list">
          {page?.items.map((r) => (
            <li key={r.id}>
              <button
                type="button"
                className="reply-item"
                aria-current={r.id === selected}
                onClick={() => setSelected(r.id)}
              >
                <span className="reply-item-top">
                  <span className="row-name">{r.lead_name ?? r.lead_email ?? "Unknown sender"}</span>
                  <span className="muted small num">{when(r.received_at)}</span>
                </span>
                {r.intent && <span className={`intent intent-${r.intent}`}>{INTENT_LABEL[r.intent]}</span>}
                <span className="reply-snippet">{(r.inbound_body ?? "").slice(0, 120)}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <section className="desk-thread" aria-live="polite">
        {reply ? <Thread key={reply.id} reply={reply} onChanged={load} /> : page && page.items.length > 0 && <p className="muted">Choose a reply.</p>}
      </section>
    </div>
  );
}

function linkNote(intent: Intent | null): string {
  if (intent === "interested") return "Gazi adds your booking link (and the campaign link, if set) when it sends.";
  if (intent === "question") return "Gazi adds the campaign's link for interested buyers, if set, when it sends.";
  return "No link is added: links only go to buyers who are interested or ask a question.";
}

function Thread({ reply, onChanged }: { reply: Reply; onChanged: () => Promise<void> }) {
  const { can, toast } = useWorkspace();
  const [draft, setDraft] = useState(reply.draft_body ?? "");
  const [intent, setIntent] = useState<Intent | null>(reply.intent);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [showOurs, setShowOurs] = useState(false);
  const editable = can("member") && !["sent", "drafted"].includes(reply.status);
  const dirty = draft !== (reply.draft_body ?? "") || intent !== reply.intent;

  async function call(path: string, init: RequestInit, message: string) {
    setBusy(true);
    setProblem(null);
    try {
      await api(path, init);
      toast(message);
      await onChanged();
    } catch (e) {
      setProblem(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function saveThenSend() {
    if (dirty) {
      setBusy(true);
      try {
        await api(`/v1/replies/${reply.id}`, {
          method: "PATCH",
          body: JSON.stringify({ ...(intent !== reply.intent ? { intent } : {}), ...(draft !== (reply.draft_body ?? "") ? { draft_body: draft } : {}) }),
        });
      } catch (e) {
        setProblem(errorMessage(e));
        setBusy(false);
        return;
      }
    }
    await call(`/v1/replies/${reply.id}/send`, { method: "POST" }, "Sending");
  }

  return (
    <article className="thread-view">
      <header className="thread-view-head">
        <div>
          <h2 className="section-title">{reply.lead_name ?? reply.lead_email ?? "Unknown sender"}</h2>
          <p className="muted small">
            {[reply.lead_company, reply.lead_email, reply.campaign_name && `from ${reply.campaign_name}`].filter(Boolean).join(", ")}
          </p>
        </div>
        {reply.lead_id && reply.campaign_id && (
          <Link href={`/campaigns/${reply.campaign_id}`} className="btn btn-quiet">
            Campaign
          </Link>
        )}
      </header>

      {reply.our_message && (
        <div className="ours">
          <button type="button" className="link-button small" onClick={() => setShowOurs((v) => !v)} aria-expanded={showOurs}>
            {showOurs ? "Hide our last message" : "Show our last message"}
          </button>
          {showOurs && <p className="letter letter-muted">{reply.our_message}</p>}
        </div>
      )}

      <div className="theirs">
        <p className="muted small">They wrote, {new Date(reply.received_at).toLocaleString()}</p>
        <p className="letter">{reply.inbound_body}</p>
        {reply.rationale && (
          <p className="muted small">
            Gazi read this as {reply.intent ? INTENT_LABEL[reply.intent].toLowerCase() : "unclear"}
            {reply.confidence != null && ` (${Math.round(reply.confidence * 100)}% sure)`}: {reply.rationale}
          </p>
        )}
      </div>

      {reply.status === "sent" ? (
        <div className="draft">
          <p className="muted small">You replied, {reply.sent_at && new Date(reply.sent_at).toLocaleString()}</p>
          <p className="letter">{reply.final_body}</p>
        </div>
      ) : reply.status === "ignored" ? (
        <p className="notice notice-plain">Dismissed. Nothing was sent.</p>
      ) : (
        <div className="draft">
          <label className="muted small" htmlFor="draft">
            {reply.status === "drafted" ? "Scheduled to send" : "Your reply"}
          </label>
          {reply.draft_body == null && reply.status !== "drafted" && (
            <p className="muted small">
              {["interested", "question", "referral", "not_interested"].includes(reply.intent ?? "")
                ? reply.error_code
                  ? "Gazi couldn't draft this one. Write it yourself, or try again."
                  : "Drafting…"
                : "No answer is drafted for this kind of reply. Write one if you'd like to answer."}
            </p>
          )}
          <textarea
            id="draft"
            className="input textarea letter-input"
            rows={8}
            value={draft}
            disabled={!editable || busy}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="draft-bar">
            <select
              className="input select select-small"
              aria-label="Intent"
              value={intent ?? ""}
              disabled={!editable || busy}
              onChange={(e) => setIntent((e.target.value || null) as Intent | null)}
            >
              <option value="">Unclassified</option>
              {Object.entries(INTENT_LABEL).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <span className="muted small">{linkNote(intent)}</span>
          </div>
          {problem && <p className="notice notice-error">{problem}</p>}
          {editable && (
            <div className="inline-form">
              <button className="btn btn-primary" disabled={busy || !draft.trim()} onClick={() => void saveThenSend()}>
                {busy ? "Sending…" : "Send"}
              </button>
              {reply.intent && (
                <button
                  className="btn btn-quiet"
                  disabled={busy}
                  onClick={() => void call(`/v1/replies/${reply.id}/redraft`, { method: "POST" }, "Drafting again")}
                >
                  Draft again
                </button>
              )}
              <button
                className="btn btn-quiet"
                disabled={busy}
                onClick={() => void call(`/v1/replies/${reply.id}/dismiss`, { method: "POST" }, "Dismissed")}
              >
                Dismiss
              </button>
            </div>
          )}
          {reply.status === "drafted" && (
            <p className="muted small">Gazi sends this shortly. To change it, dismiss it and write a new reply.</p>
          )}
        </div>
      )}
    </article>
  );
}
