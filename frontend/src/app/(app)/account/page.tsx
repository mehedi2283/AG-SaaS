"use client";

import { useState, type FormEvent } from "react";

import { errorMessage } from "@/lib/api";
import { supabase } from "@/lib/supabase";
import { useWorkspace } from "@/lib/workspace";

/** The signed-in person's own account: email and password. Reachable whatever the workspace's plan. */
export default function AccountPage() {
  const { me, toast } = useWorkspace();
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const tooShort = password.length > 0 && password.length < 8;
  const mismatch = confirm.length > 0 && confirm !== password;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { error: updateError } = await supabase.auth.updateUser({ password });
      if (updateError) throw updateError;
      setPassword("");
      setConfirm("");
      toast("Your password is changed");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page" style={{ maxWidth: 560 }}>
      <header className="page-head">
        <div>
          <h1 className="title">Your account</h1>
          <p className="lede">Signed in as {me.email}.</p>
        </div>
      </header>

      <section className="section">
        <div className="section-head">
          <h2 className="section-title">Change password</h2>
          <p className="muted small">You stay signed in here. Use the new password next time you sign in.</p>
        </div>
        <form onSubmit={submit} noValidate style={{ display: "grid", gap: 18 }}>
          <div className="field">
            <label htmlFor="new-password">New password</label>
            <input
              id="new-password"
              className="input"
              type="password"
              autoComplete="new-password"
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              aria-describedby="new-password-hint"
            />
            <span id="new-password-hint" className="hint">
              {tooShort ? "Use at least 8 characters." : "At least 8 characters."}
            </span>
          </div>
          <div className="field">
            <label htmlFor="confirm-password">Type it again</label>
            <input
              id="confirm-password"
              className="input"
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
            {mismatch && <span className="hint">The two passwords don&apos;t match yet.</span>}
          </div>
          {error && (
            <p className="notice notice-error" role="alert">
              {error}
            </p>
          )}
          <div>
            <button
              className="btn btn-primary"
              type="submit"
              disabled={busy || password.length < 8 || confirm !== password}
            >
              {busy ? "Saving…" : "Change password"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
