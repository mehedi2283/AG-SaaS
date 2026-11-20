"use client";

import { useCallback, useEffect, useState } from "react";

import { api, errorMessage, type WebhookEndpoint, type WebhookEndpointCreated, type WebhookProvider } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

const PUBLIC_API = (process.env.NEXT_PUBLIC_API_PUBLIC_URL ?? "http://localhost:8000").replace(/\/$/, "");

const PROVIDERS: { id: WebhookProvider; name: string; use: string; secretHeader: boolean }[] = [
  { id: "instantly", name: "Instantly", use: "Tells Gazi when a buyer replies by email", secretHeader: true },
  { id: "heyreach", name: "HeyReach", use: "Tells Gazi when a buyer replies on LinkedIn", secretHeader: false },
  { id: "calendly", name: "Calendly", use: "Tells Gazi when a call is booked", secretHeader: true },
];

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function WebhookAddresses() {
  const { toast } = useWorkspace();
  const [endpoints, setEndpoints] = useState<WebhookEndpoint[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [created, setCreated] = useState<WebhookEndpointCreated | null>(null);
  const [busy, setBusy] = useState<WebhookProvider | null>(null);
  const [withSecret, setWithSecret] = useState<Record<string, boolean>>({});

  const load = useCallback(async () => {
    try {
      setEndpoints(await api<WebhookEndpoint[]>("/v1/webhook-endpoints"));
      setLoadError(null);
    } catch (e) {
      setLoadError(errorMessage(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function run(provider: WebhookProvider, action: () => Promise<WebhookEndpointCreated | null>, done: string) {
    setBusy(provider);
    try {
      const result = await action();
      setCreated(result);
      toast(done);
      await load();
    } catch (e) {
      toast(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="section">
      <div className="section-head">
        <h2 className="section-title">Webhook addresses</h2>
        <p className="muted small">
          Paste each address into the matching tool so it can tell Gazi what happened. Tools can only reach it
          once Gazi runs on a public domain.
        </p>
      </div>
      {loadError && <p className="notice notice-error">{loadError}</p>}
      <div className="rows">
        {PROVIDERS.map((p) => {
          const active = endpoints?.find((e) => e.provider === p.id && !e.revoked_at);
          const justCreated = created?.provider === p.id ? created : null;
          return (
            <div className="row" key={p.id}>
              <div className="row-main">
                <div>
                  <div className="row-name">{p.name}</div>
                  <div className="row-desc">{p.use}</div>
                  <div className="row-status">
                    <span className={active ? "dot dot-on" : "dot"} aria-hidden="true" />
                    {endpoints === null ? (
                      <span className="muted">Checking…</span>
                    ) : active ? (
                      <span>
                        Address created {formatDate(active.created_at)}
                        {active.last_used_at ? `, last used ${formatDate(active.last_used_at)}` : ", not used yet"}
                      </span>
                    ) : (
                      <span className="muted">No address</span>
                    )}
                  </div>
                </div>
                <div className="row-actions">
                  {active ? (
                    <>
                      <button
                        className="btn btn-quiet btn-danger"
                        disabled={busy === p.id}
                        onClick={() =>
                          window.confirm(`Turn off the ${p.name} address? ${p.name} won't be able to reach Gazi.`) &&
                          void run(p.id, async () => {
                            await api(`/v1/webhook-endpoints/${active.id}/revoke`, { method: "POST" });
                            return null;
                          }, `Turned off the ${p.name} address`)
                        }
                      >
                        Turn off
                      </button>
                      <button
                        className="btn"
                        disabled={busy === p.id}
                        onClick={() =>
                          window.confirm(`Replace the ${p.name} address? The old one stops working immediately.`) &&
                          void run(
                            p.id,
                            () => api<WebhookEndpointCreated>(`/v1/webhook-endpoints/${active.id}/rotate`, { method: "POST" }),
                            `Replaced the ${p.name} address`,
                          )
                        }
                      >
                        Replace address
                      </button>
                    </>
                  ) : (
                    <button
                      className="btn"
                      disabled={busy === p.id || endpoints === null}
                      onClick={() =>
                        void run(
                          p.id,
                          () =>
                            api<WebhookEndpointCreated>("/v1/webhook-endpoints", {
                              method: "POST",
                              body: JSON.stringify({ provider: p.id, require_secret: Boolean(withSecret[p.id]) }),
                            }),
                          `Created the ${p.name} address`,
                        )
                      }
                    >
                      Create address
                    </button>
                  )}
                </div>
              </div>
              {!active && p.secretHeader && endpoints !== null && (
                <div className="row-panel" style={{ paddingTop: 0 }}>
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={Boolean(withSecret[p.id])}
                      onChange={(e) => setWithSecret((v) => ({ ...v, [p.id]: e.target.checked }))}
                    />
                    <span>
                      Also require a secret header
                      <span className="muted small" style={{ display: "block" }}>
                        Extra protection. {p.name} must send it as X-Gazi-Secret.
                      </span>
                    </span>
                  </label>
                </div>
              )}
              {justCreated && <Reveal created={justCreated} onDone={() => setCreated(null)} />}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function Reveal({ created, onDone }: { created: WebhookEndpointCreated; onDone: () => void }) {
  const url = `${PUBLIC_API}${created.url_path}`;
  const [copied, setCopied] = useState<string | null>(null);

  async function copy(label: string, text: string) {
    await navigator.clipboard.writeText(text);
    setCopied(label);
  }

  return (
    <div className="row-panel">
      <div className="reveal" role="region" aria-label="New webhook address">
        <strong>Copy this now. Gazi won&apos;t show it again.</strong>
        <code>{url}</code>
        {created.secret && (
          <>
            <span className="small muted">Secret header value (X-Gazi-Secret)</span>
            <code>{created.secret}</code>
          </>
        )}
        <div className="actions" style={{ display: "flex", gap: 8 }}>
          <button className="btn" onClick={() => void copy("address", url)}>
            {copied === "address" ? "Address copied" : "Copy address"}
          </button>
          {created.secret && (
            <button className="btn" onClick={() => void copy("secret", created.secret ?? "")}>
              {copied === "secret" ? "Secret copied" : "Copy secret"}
            </button>
          )}
          <button className="btn btn-quiet" onClick={onDone}>
            Done
          </button>
        </div>
      </div>
    </div>
  );
}
