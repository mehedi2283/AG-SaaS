"use client";

import { useState, type FormEvent } from "react";

import { api, errorMessage, type LeadImport } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

const SIZES = [
  { value: "1,10", label: "1–10" },
  { value: "11,50", label: "11–50" },
  { value: "51,200", label: "51–200" },
  { value: "201,500", label: "201–500" },
  { value: "501,1000", label: "501–1,000" },
  { value: "1001,5000", label: "1,001–5,000" },
  { value: "5001,1000000", label: "5,000+" },
];

const list = (text: string) =>
  text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);

/** Find buyers in Apollo (the workspace's own Apollo key). Gazi reveals work emails and scores each person. */
export function ApolloSearch({ campaignId, onStarted }: { campaignId: string; onStarted: () => void }) {
  const { toast } = useWorkspace();
  const [open, setOpen] = useState(false);
  const [titles, setTitles] = useState("");
  const [locations, setLocations] = useState("");
  const [keywords, setKeywords] = useState("");
  const [sizes, setSizes] = useState<string[]>([]);
  const [max, setMax] = useState(25);
  const [minScore, setMinScore] = useState(6);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api<LeadImport>(`/v1/campaigns/${campaignId}/apollo-search`, {
        method: "POST",
        body: JSON.stringify({
          titles: list(titles),
          locations: list(locations),
          keywords: keywords.trim() || null,
          employee_ranges: sizes,
          max_people: max,
          min_score: minScore,
        }),
      });
      toast("Searching Apollo");
      setOpen(false);
      onStarted();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button type="button" className="btn" onClick={() => setOpen(true)}>
        Find buyers in Apollo
      </button>
    );
  }

  return (
    <form className="row-panel apollo-form" onSubmit={submit} noValidate>
      <div className="field">
        <label htmlFor="ap-titles">Job titles</label>
        <input id="ap-titles" className="input" placeholder="Head of Buying, Sourcing Manager" value={titles} onChange={(e) => setTitles(e.target.value)} />
        <span className="hint">Separate with commas.</span>
      </div>
      <div className="field">
        <label htmlFor="ap-locations">Where they are</label>
        <input id="ap-locations" className="input" placeholder="Portugal, Spain" value={locations} onChange={(e) => setLocations(e.target.value)} />
      </div>
      <div className="field">
        <label htmlFor="ap-keywords">Company keyword (optional)</label>
        <input id="ap-keywords" className="input" placeholder="knitwear" value={keywords} onChange={(e) => setKeywords(e.target.value)} />
      </div>
      <fieldset className="field">
        <legend>Company size</legend>
        <div className="day-row">
          {SIZES.map((s) => (
            <label key={s.value} className="day">
              <input
                type="checkbox"
                checked={sizes.includes(s.value)}
                onChange={(e) => setSizes(e.target.checked ? [...sizes, s.value] : sizes.filter((x) => x !== s.value))}
              />
              <span>{s.label}</span>
            </label>
          ))}
        </div>
      </fieldset>
      <div className="field-row">
        <div className="field">
          <label htmlFor="ap-max">How many people</label>
          <select id="ap-max" className="input select" value={max} onChange={(e) => setMax(Number(e.target.value))}>
            {[10, 25, 50, 100].map((n) => (
              <option key={n} value={n}>
                Up to {n}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="ap-score">Keep buyers scoring at least</label>
          <select id="ap-score" className="input select" value={minScore} onChange={(e) => setMinScore(Number(e.target.value))}>
            {[0, 4, 5, 6, 7, 8].map((n) => (
              <option key={n} value={n}>
                {n === 0 ? "Keep everyone" : `${n} out of 10`}
              </option>
            ))}
          </select>
        </div>
      </div>
      <p className="muted small">
        Revealing each person&apos;s email uses your Apollo credits. Gazi then scores how likely each one is to buy from
        you, using your offer, and keeps those above your threshold.
      </p>
      {error && (
        <p className="notice notice-error" role="alert">
          {error}
        </p>
      )}
      <div className="actions">
        <button className="btn btn-primary" type="submit" disabled={busy || !(titles.trim() || locations.trim() || keywords.trim())}>
          {busy ? "Starting…" : "Search Apollo"}
        </button>
        <button className="btn btn-quiet" type="button" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </form>
  );
}
