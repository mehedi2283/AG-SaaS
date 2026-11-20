"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import { api, errorMessage, type Integration, type Provider } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

type Meta = { name: string; use: string; fields: { key: string; label: string; secret: boolean; hint?: string }[] };

const PROVIDERS: Record<Provider, Meta> = {
  instantly: { name: "Instantly", use: "Sends your email sequences", fields: [{ key: "api_key", label: "API key", secret: true }] },
  heyreach: { name: "HeyReach", use: "Sends your LinkedIn sequences", fields: [{ key: "api_key", label: "API key", secret: true }] },
  apollo: { name: "Apollo", use: "Finds and enriches buyers", fields: [{ key: "api_key", label: "API key", secret: true }] },
  calendly: {
    name: "Calendly",
    use: "Your booking link, and calls that get booked",
    fields: [
      { key: "booking_url", label: "Booking link", secret: false, hint: "The link buyers use to pick a time." },
      { key: "api_token", label: "Personal access token", secret: true, hint: "Lets Gazi see when a call is booked." },
    ],
  },
};
const ORDER: Provider[] = ["instantly", "heyreach", "apollo", "calendly"];

export function ConnectedAccounts() {
  const { can, toast } = useWorkspace();
  const [items, setItems] = useState<Integration[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Provider | null>(null);

  const load = useCallback(async () => {
    try {
      setItems(await api<Integration[]>("/v1/integrations"));
      setLoadError(null);
    } catch (e) {
      setLoadError(errorMessage(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function disconnect(provider: Provider) {
    if (!window.confirm(`Disconnect ${PROVIDERS[provider].name}? Gazi will stop using its key straight away.`)) return;
    try {
      await api(`/v1/integrations/${provider}`, { method: "DELETE" });
      toast(`Disconnected ${PROVIDERS[provider].name}`);
      await load();
    } catch (e) {
      toast(errorMessage(e), "error");
    }
  }

  return (
    <section className="section">
      <div className="section-head">
        <h2 className="section-title">Connected accounts</h2>
        <p className="muted small">Keys are encrypted for this workspace. After you save one, only its last four characters are shown.</p>
      </div>
      {loadError && <p className="notice notice-error">{loadError}</p>}
      <div className="rows">
        {ORDER.map((provider) => {
          const meta = PROVIDERS[provider];
          const saved = items?.find((i) => i.provider === provider);
          const isEditing = editing === provider;
          return (
            <div className="row" key={provider}>
              <div className="row-main">
                <div>
                  <div className="row-name">{meta.name}</div>
                  <div className="row-desc">{meta.use}</div>
                  <div className="row-status">
                    <span className={saved ? "dot dot-on" : "dot"} aria-hidden="true" />
                    {items === null ? (
                      <span className="muted">Checking…</span>
                    ) : saved ? (
                      <span>
                        Connected{saved.key_last4 ? <>, key ending <span className="num">{saved.key_last4}</span></> : null}
                      </span>
                    ) : (
                      <span className="muted">Not connected</span>
                    )}
                  </div>
                </div>
                {!isEditing && (
                  <div className="row-actions">
                    {saved && can("owner") && (
                      <button className="btn btn-quiet btn-danger" onClick={() => void disconnect(provider)}>
                        Disconnect
                      </button>
                    )}
                    <button className="btn" onClick={() => setEditing(provider)}>
                      {saved ? "Replace key" : "Connect"}
                    </button>
                  </div>
                )}
              </div>
              {isEditing && (
                <KeyForm
                  provider={provider}
                  meta={meta}
                  onCancel={() => setEditing(null)}
                  onSaved={async () => {
                    setEditing(null);
                    toast(`Saved ${meta.name} key`);
                    await load();
                  }}
                />
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function KeyForm({
  provider,
  meta,
  onCancel,
  onSaved,
}: {
  provider: Provider;
  meta: Meta;
  onCancel: () => void;
  onSaved: () => Promise<void>;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const complete = meta.fields.every((f) => (values[f.key] ?? "").trim().length > 0);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const body = Object.fromEntries(meta.fields.map((f) => [f.key, values[f.key].trim()]));
      await api(`/v1/integrations/${provider}`, { method: "PUT", body: JSON.stringify(body) });
      await onSaved();
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  return (
    <form className="row-panel" onSubmit={submit} noValidate>
      {meta.fields.map((f, index) => (
        <div className="field" key={f.key}>
          <label htmlFor={`${provider}-${f.key}`}>{f.label}</label>
          <input
            id={`${provider}-${f.key}`}
            className="input"
            type={f.secret ? "password" : "url"}
            autoComplete="off"
            spellCheck={false}
            autoFocus={index === 0}
            value={values[f.key] ?? ""}
            onChange={(e) => setValues((v) => ({ ...v, [f.key]: e.target.value }))}
          />
          {f.hint && <span className="hint">{f.hint}</span>}
        </div>
      ))}
      {error && (
        <p className="notice notice-error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button className="btn btn-primary" type="submit" disabled={busy || !complete}>
          {busy ? "Saving…" : `Save ${meta.name} key`}
        </button>
        <button className="btn btn-quiet" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}
