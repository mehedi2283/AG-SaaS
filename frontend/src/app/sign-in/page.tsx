"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";

import { supabase } from "@/lib/supabase";

type Mode = "sign-in" | "sign-up";

export default function SignInPage() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("sign-in");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function forgot() {
    setError(null);
    if (!email.trim()) {
      setError("Type your email first, then choose Forgot password.");
      return;
    }
    setBusy(true);
    const { error: resetError } = await supabase.auth.resetPasswordForEmail(email.trim(), {
      redirectTo: `${window.location.origin}/account`,
    });
    setBusy(false);
    if (resetError) setError(resetError.message);
    else setNotice("Check your inbox for a link. It signs you in so you can set a new password.");
  }

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      if (data.session) router.replace("/replies");
    });
  }, [router]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const result =
      mode === "sign-in"
        ? await supabase.auth.signInWithPassword({ email, password })
        : await supabase.auth.signUp({ email, password });
    setBusy(false);
    if (result.error) {
      setError(
        result.error.message === "Invalid login credentials"
          ? "That email and password don't match. Check them and try again."
          : result.error.message,
      );
      return;
    }
    if (!result.data.session) {
      setError("Check your inbox to confirm your email, then sign in.");
      return;
    }
    router.replace("/replies");
  }

  const isSignIn = mode === "sign-in";

  return (
    <main className="auth">
      <section className="auth-form">
        <div className="wordmark" style={{ padding: 0 }}>
          Gazi
        </div>
        <h1 className="title" style={{ marginTop: 56 }}>
          {isSignIn ? "Sign in" : "Create your account"}
        </h1>
        <p className="lede">
          {isSignIn
            ? "Pick up where your buyers left off."
            : "Start with your email. You'll set up your workspace next."}
        </p>

        <form onSubmit={submit} noValidate>
          <div className="field">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              className="input"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              className="input"
              type="password"
              autoComplete={isSignIn ? "current-password" : "new-password"}
              minLength={8}
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            {!isSignIn && <span className="hint">At least 8 characters.</span>}
          </div>
          {error && (
            <p className="notice notice-error" role="alert">
              {error}
            </p>
          )}
          {notice && (
            <p className="notice notice-plain" role="status">
              {notice}
            </p>
          )}
          <button className="btn btn-primary btn-block" type="submit" disabled={busy || !email || !password}>
            {busy ? "One moment…" : isSignIn ? "Sign in" : "Create account"}
          </button>
        </form>

        {isSignIn && (
          <p className="switch-mode" style={{ marginTop: 12 }}>
            <button type="button" className="link-button" onClick={() => void forgot()} disabled={busy}>
              Forgot password?
            </button>
          </p>
        )}
        <p className="switch-mode">
          {isSignIn ? "New to Gazi? " : "Already have an account? "}
          <button
            type="button"
            className="link-button"
            onClick={() => {
              setMode(isSignIn ? "sign-up" : "sign-in");
              setError(null);
            }}
          >
            {isSignIn ? "Create an account" : "Sign in"}
          </button>
        </p>
      </section>

      <aside className="auth-aside" aria-hidden="true">
        <figure>
          <blockquote className="letter">
            Hello, yes this is timely. We are reviewing suppliers for the spring range now. Could you share
            your MOQ per colour, and whether you can do custom labels?
          </blockquote>
          <figcaption>A buyer&apos;s reply. Gazi drafts the answer; you decide when it goes.</figcaption>
        </figure>
      </aside>
    </main>
  );
}
