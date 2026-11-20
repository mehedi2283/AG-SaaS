"use client";

import { useState, type FormEvent } from "react";

import { errorMessage } from "@/lib/api";

function toSlug(name: string): string {
  return name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63);
}

/** Create a workspace. The address is suggested from the name until the user edits it. */
export function WorkspaceForm({
  onCreate,
  submitLabel = "Create workspace",
}: {
  onCreate: (name: string, slug: string) => Promise<void>;
  submitLabel?: string;
}) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const effectiveSlug = slugEdited ? slug : toSlug(name);
  const slugValid = /^[a-z0-9][a-z0-9-]{1,62}$/.test(effectiveSlug);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onCreate(name.trim(), effectiveSlug);
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} noValidate>
      <div className="field">
        <label htmlFor="ws-name">Company name</label>
        <input
          id="ws-name"
          className="input"
          autoComplete="organization"
          required
          maxLength={200}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div className="field">
        <label htmlFor="ws-slug">Workspace address</label>
        <input
          id="ws-slug"
          className="input"
          required
          value={effectiveSlug}
          onChange={(e) => {
            setSlugEdited(true);
            setSlug(e.target.value.toLowerCase());
          }}
          aria-describedby="ws-slug-hint"
        />
        <span id="ws-slug-hint" className="hint">
          Lowercase letters, numbers and dashes. You can&apos;t change it later.
        </span>
      </div>
      {error && (
        <p className="notice notice-error" role="alert">
          {error}
        </p>
      )}
      <button className="btn btn-primary" type="submit" disabled={busy || !name.trim() || !slugValid}>
        {busy ? "Creating…" : submitLabel}
      </button>
    </form>
  );
}
