"use client";

import { useCallback, useEffect, useState } from "react";

import { api, errorMessage, type Campaign, type Message, type MessagePage } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";
import { SkeletonLetters } from "@/components/Skeleton";

const PAGE = 30;

/** Review what Gazi wrote, buyer by buyer. Edits follow the same rules as the AI. */
export function Messages({ campaign, refreshKey }: { campaign: Campaign; refreshKey: number }) {
  const { toast } = useWorkspace();
  const [page, setPage] = useState<MessagePage | null>(null);
  const [onlyFailed, setOnlyFailed] = useState(false);
  const [offset, setOffset] = useState(0);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    if (onlyFailed) params.set("status", "failed");
    try {
      setPage(await api<MessagePage>(`/v1/campaigns/${campaign.id}/messages?${params}`));
    } catch (e) {
      toast(errorMessage(e), "error");
    }
  }, [campaign.id, onlyFailed, offset, toast]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  if (!page) return <SkeletonLetters />;

  // Group by buyer, keeping step order.
  const byLead = new Map<string, Message[]>();
  for (const m of page.items) byLead.set(m.lead_id, [...(byLead.get(m.lead_id) ?? []), m]);

  return (
    <div>
      <div className="filters">
        <label className="check">
          <input
            type="checkbox"
            checked={onlyFailed}
            onChange={(e) => {
              setOffset(0);
              setOnlyFailed(e.target.checked);
            }}
          />
          <span>Only messages that need a look</span>
        </label>
      </div>
      {page.items.length === 0 && (
        <p className="muted" style={{ marginTop: 16 }}>
          {onlyFailed ? "Nothing needs a look." : "No messages yet. Use Write messages above."}
        </p>
      )}
      {[...byLead.entries()].map(([leadId, messages]) => (
        <article key={leadId} className="thread">
          <header className="thread-head">
            <span className="row-name">{messages[0].lead_name ?? messages[0].lead_email}</span>
            <span className="row-desc">
              {messages[0].lead_company ? `${messages[0].lead_company}, ` : ""}
              {messages[0].lead_email}
            </span>
          </header>
          {messages.map((m) => (
            <MessageCard key={m.id} message={m} onSaved={load} />
          ))}
          {messages.some((m) => m.status === "failed") && <Rewrite leadId={leadId} onDone={load} />}
        </article>
      ))}
      {page.total > PAGE && (
        <div className="pager">
          <span className="muted small num">
            {offset + 1}–{Math.min(offset + PAGE, page.total)} of {page.total} messages
          </span>
          <button className="btn btn-quiet" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
            Previous
          </button>
          <button className="btn btn-quiet" disabled={offset + PAGE >= page.total} onClick={() => setOffset(offset + PAGE)}>
            Next
          </button>
        </div>
      )}
    </div>
  );
}

function MessageCard({ message: m, onSaved }: { message: Message; onSaved: () => Promise<void> }) {
  const { can, toast } = useWorkspace();
  const [editing, setEditing] = useState(false);
  const [subject, setSubject] = useState(m.subject ?? "");
  const [body, setBody] = useState(m.body ?? "");
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string | null>(null);
  const editable = can("member") && m.status !== "pushed" && m.status !== "pending";

  async function save() {
    setBusy(true);
    setProblems(null);
    try {
      await api(`/v1/lead-messages/${m.id}`, {
        method: "PATCH",
        body: JSON.stringify({ subject: m.channel === "email" ? subject : null, body }),
      });
      toast("Message saved");
      setEditing(false);
      await onSaved();
    } catch (e) {
      setProblems(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`message${m.status === "failed" ? " message-failed" : ""}`}>
      <div className="message-meta">
        <span className="small">Step {m.step}</span>
        <span className="muted small">
          {m.status === "pending" && "Writing…"}
          {m.status === "failed" && <span className="status-off">Needs a look: {m.validation_errors.join(", ") || m.error_code}</span>}
          {m.status === "approved" && "Edited"}
          {m.status === "pushed" && "Sent to the sequence"}
        </span>
      </div>
      {editing ? (
        <div className="message-edit">
          {m.channel === "email" && (
            <input className="input" aria-label="Subject" value={subject} onChange={(e) => setSubject(e.target.value)} />
          )}
          <textarea className="input textarea letter-input" rows={7} aria-label="Message" value={body} onChange={(e) => setBody(e.target.value)} />
          {problems && <p className="notice notice-error">{problems}</p>}
          <div className="inline-form">
            <button className="btn btn-primary" disabled={busy || !body.trim()} onClick={() => void save()}>
              {busy ? "Saving…" : "Save"}
            </button>
            <button className="btn btn-quiet" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <>
          {m.subject && <p className="message-subject">{m.subject}</p>}
          {m.body ? <p className="letter message-body">{m.body}</p> : <p className="muted small">Not written yet.</p>}
          {editable && m.body && (
            <button className="btn btn-quiet" style={{ paddingLeft: 0 }} onClick={() => setEditing(true)}>
              Edit
            </button>
          )}
        </>
      )}
    </div>
  );
}

function Rewrite({ leadId, onDone }: { leadId: string; onDone: () => Promise<void> }) {
  const { can, toast } = useWorkspace();
  const [busy, setBusy] = useState(false);
  if (!can("member")) return null;
  return (
    <button
      className="btn"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          await api(`/v1/leads/${leadId}/regenerate`, { method: "POST" });
          toast("Rewriting this buyer's messages");
          await onDone();
        } catch (e) {
          toast(errorMessage(e), "error");
        } finally {
          setBusy(false);
        }
      }}
    >
      Write again
    </button>
  );
}
