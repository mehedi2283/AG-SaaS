"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import { api, errorMessage, type Sender } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

type Draft = {
  display_name: string;
  title: string;
  company: string;
  signature: string;
  signature_image_url: string;
  instantly_eaccount: string;
  heyreach_account_id: string;
  is_default: boolean;
};

const EMPTY: Draft = {
  display_name: "",
  title: "",
  company: "",
  signature: "",
  signature_image_url: "",
  instantly_eaccount: "",
  heyreach_account_id: "",
  is_default: false,
};

/** Who campaigns send as. The signature is encrypted; once saved it is never shown again, only replaced. */
export function Senders() {
  const { toast } = useWorkspace();
  const [senders, setSenders] = useState<Sender[] | null>(null);
  const [editing, setEditing] = useState<string | "new" | null>(null);

  const load = useCallback(async () => {
    try {
      setSenders(await api<Sender[]>("/v1/sender-identities"));
    } catch (e) {
      toast(errorMessage(e), "error");
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section className="section">
      <div className="section-head">
        <h2 className="section-title">Senders</h2>
        <p className="muted small">
          The person a campaign writes as, and the Instantly mailbox or HeyReach LinkedIn account that sends.
        </p>
      </div>
      <div className="rows">
        {senders?.map((s) => (
          <div className="row" key={s.id}>
            <div className="row-main">
              <div>
                <div className="row-name">
                  {s.display_name}
                  {s.is_default && <span className="muted small"> (default)</span>}
                </div>
                <div className="row-desc">
                  {[s.title, s.instantly_eaccount, s.heyreach_account_id && `HeyReach ${s.heyreach_account_id}`]
                    .filter(Boolean)
                    .join(", ") || "No sending account yet"}
                </div>
              </div>
              {editing !== s.id && (
                <div className="row-actions">
                  <button className="btn" onClick={() => setEditing(s.id)}>
                    Edit
                  </button>
                </div>
              )}
            </div>
            {editing === s.id && (
              <SenderForm
                sender={s}
                onDone={async (message) => {
                  setEditing(null);
                  if (message) {
                    toast(message);
                    await load();
                  }
                }}
              />
            )}
          </div>
        ))}
        {editing === "new" ? (
          <div className="row">
            <SenderForm
              onDone={async (message) => {
                setEditing(null);
                if (message) {
                  toast(message);
                  await load();
                }
              }}
            />
          </div>
        ) : (
          <div style={{ padding: "16px 0" }}>
            <button className="btn" onClick={() => setEditing("new")}>
              Add a sender
            </button>
          </div>
        )}
      </div>
    </section>
  );
}

function SenderForm({ sender, onDone }: { sender?: Sender; onDone: (message: string | null) => Promise<void> }) {
  const [draft, setDraft] = useState<Draft>(
    sender
      ? {
          ...EMPTY,
          display_name: sender.display_name,
          title: sender.title ?? "",
          company: sender.company ?? "",
          signature_image_url: sender.signature_image_url ?? "",
          instantly_eaccount: sender.instantly_eaccount ?? "",
          heyreach_account_id: sender.heyreach_account_id ?? "",
          is_default: sender.is_default,
        }
      : { ...EMPTY, is_default: true },
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (key: keyof Draft) => (value: string | boolean) => setDraft((d) => ({ ...d, [key]: value }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const body: Record<string, unknown> = {
      display_name: draft.display_name.trim(),
      title: draft.title.trim() || null,
      company: draft.company.trim() || null,
      signature_image_url: draft.signature_image_url.trim() || null,
      instantly_eaccount: draft.instantly_eaccount.trim() || null,
      heyreach_account_id: draft.heyreach_account_id.trim() || null,
      is_default: draft.is_default,
    };
    if (draft.signature.trim()) body.signature = draft.signature.trim();
    try {
      await api(sender ? `/v1/sender-identities/${sender.id}` : "/v1/sender-identities", {
        method: sender ? "PATCH" : "POST",
        body: JSON.stringify(body),
      });
      await onDone(sender ? "Sender saved" : "Sender added");
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  async function remove() {
    if (!sender || !window.confirm(`Remove ${sender.display_name}?`)) return;
    try {
      await api(`/v1/sender-identities/${sender.id}`, { method: "DELETE" });
      await onDone("Sender removed");
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  const text = (key: keyof Draft, label: string, hint?: string, type = "text") => (
    <div className="field">
      <label htmlFor={`s-${key}`}>{label}</label>
      <input
        id={`s-${key}`}
        className="input"
        type={type}
        value={draft[key] as string}
        onChange={(e) => set(key)(e.target.value)}
      />
      {hint && <span className="hint">{hint}</span>}
    </div>
  );

  return (
    <form className="row-panel" onSubmit={submit} noValidate>
      {text("display_name", "Name", "As it appears to buyers.")}
      {text("title", "Job title")}
      {text("company", "Company")}
      <div className="field">
        <label htmlFor="s-signature">{sender?.has_signature ? "Replace signature" : "Signature"}</label>
        <textarea
          id="s-signature"
          className="input textarea"
          rows={3}
          placeholder={sender?.has_signature ? "Saved. Type a new one to replace it." : ""}
          value={draft.signature}
          onChange={(e) => set("signature")(e.target.value)}
        />
        <span className="hint">Added by Gazi under every message. The AI never writes a sign-off itself.</span>
      </div>
      {text("signature_image_url", "Signature image link (optional)", "An https:// image, such as your logo. The only link allowed in follow-ups.", "url")}
      {text("instantly_eaccount", "Instantly mailbox", "The email address that sends, as set up in Instantly.", "email")}
      {text("heyreach_account_id", "HeyReach LinkedIn account ID", "For LinkedIn campaigns. Find it under LinkedIn accounts in HeyReach.")}
      <label className="check">
        <input type="checkbox" checked={draft.is_default} onChange={(e) => set("is_default")(e.target.checked)} />
        <span>Use for new campaigns</span>
      </label>
      {error && (
        <p className="notice notice-error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button className="btn btn-primary" type="submit" disabled={busy || !draft.display_name.trim()}>
          {busy ? "Saving…" : "Save sender"}
        </button>
        <button className="btn btn-quiet" type="button" onClick={() => void onDone(null)}>
          Cancel
        </button>
        {sender && (
          <button className="btn btn-quiet btn-danger" type="button" onClick={() => void remove()}>
            Remove
          </button>
        )}
      </div>
    </form>
  );
}
