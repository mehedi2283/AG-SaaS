"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";

import { DAY_LABEL } from "@/components/campaigns/labels";
import { api, errorMessage, type Campaign, type Channel, type Day, type Sender } from "@/lib/api";

const DAYS: Day[] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

export type CampaignValues = {
  name: string;
  channel?: Channel;
  offer_summary: string | null;
  offer_details: string | null;
  attachment_url: string | null;
  report_emails: string[];
  timezone: string;
  sending_days: Day[];
  send_from: string;
  send_to: string;
  daily_limit: number;
  sender_identity_id: string | null;
  sequence_count?: number;
};

function zones(): string[] {
  try {
    return (Intl as unknown as { supportedValuesOf(k: string): string[] }).supportedValuesOf("timeZone");
  } catch {
    return ["UTC"];
  }
}

/** Create or edit a campaign. After launch, the schedule and sender are fixed (they live in Instantly/HeyReach). */
export function CampaignForm({
  initial,
  submitLabel,
  onSubmit,
}: {
  initial?: Campaign;
  submitLabel: string;
  onSubmit: (values: CampaignValues) => Promise<void>;
}) {
  const editing = !!initial;
  const launched = !!initial && initial.launch_status !== "not_launched";
  const [senders, setSenders] = useState<Sender[]>([]);
  const [name, setName] = useState(initial?.name ?? "");
  const [channel, setChannel] = useState<Channel>(initial?.channel ?? "email");
  const [summary, setSummary] = useState(initial?.offer_summary ?? "");
  const [details, setDetails] = useState(initial?.offer_details ?? "");
  const [attachment, setAttachment] = useState(initial?.attachment_url ?? "");
  const [reportEmails, setReportEmails] = useState((initial?.report_emails ?? []).join(", "));
  const [timezone, setTimezone] = useState(
    initial?.timezone ?? (Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"),
  );
  const [days, setDays] = useState<Day[]>(initial?.sending_days ?? ["mon", "tue", "wed", "thu", "fri"]);
  const [from, setFrom] = useState((initial?.send_from ?? "09:00").slice(0, 5));
  const [to, setTo] = useState((initial?.send_to ?? "17:00").slice(0, 5));
  const [limit, setLimit] = useState(initial?.daily_limit ?? 50);
  const [sender, setSender] = useState<string>(initial?.sender_identity_id ?? "");
  const [steps, setSteps] = useState(5);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const allZones = useMemo(zones, []);

  useEffect(() => {
    api<Sender[]>("/v1/sender-identities")
      .then((list) => {
        setSenders(list);
        if (!initial && !sender) setSender(list.find((s) => s.is_default)?.id ?? "");
      })
      .catch(() => setSenders([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const values: CampaignValues = {
      name: name.trim(),
      offer_summary: summary.trim() || null,
      offer_details: details.trim() || null,
      attachment_url: attachment.trim() || null,
      report_emails: reportEmails
        .split(/[,\s]+/)
        .map((e) => e.trim())
        .filter(Boolean),
      ...(launched
        ? {}
        : { timezone, sending_days: days, send_from: from, send_to: to, daily_limit: limit, sender_identity_id: sender || null }),
      ...(editing ? {} : { channel, sequence_count: steps }),
    } as CampaignValues;
    try {
      await onSubmit(values);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form" onSubmit={submit} noValidate>
      <div className="field">
        <label htmlFor="c-name">Name</label>
        <input id="c-name" className="input" required maxLength={200} value={name} onChange={(e) => setName(e.target.value)} />
        <span className="hint">For you and your team, for example &quot;UK fashion buyers, spring&quot;.</span>
      </div>

      {!editing && (
        <fieldset className="field">
          <legend>Channel</legend>
          <div className="choice-row">
            {(["email", "linkedin"] as Channel[]).map((c) => (
              <label key={c} className="choice">
                <input type="radio" name="channel" checked={channel === c} onChange={() => setChannel(c)} />
                <span>
                  <span className="row-name">{c === "email" ? "Email" : "LinkedIn"}</span>
                  <span className="row-desc">{c === "email" ? "Sent through Instantly" : "Sent through HeyReach"}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>
      )}

      <div className="field">
        <label htmlFor="c-summary">What you sell</label>
        <textarea
          id="c-summary"
          className="input textarea"
          rows={4}
          maxLength={4000}
          value={summary}
          onChange={(e) => setSummary(e.target.value)}
        />
        <span className="hint">The offer, what makes it different, and anything Gazi must never say.</span>
      </div>
      <div className="field">
        <label htmlFor="c-details">Details and proof</label>
        <textarea
          id="c-details"
          className="input textarea"
          rows={5}
          maxLength={12000}
          value={details}
          onChange={(e) => setDetails(e.target.value)}
        />
        <span className="hint">Who you sell to, case studies, minimum order, certifications. Gazi only uses facts written here.</span>
      </div>

      {!editing && (
        <div className="field">
          <label htmlFor="c-steps">Messages in the sequence</label>
          <select id="c-steps" className="input select" value={steps} onChange={(e) => setSteps(Number(e.target.value))}>
            {[1, 2, 3, 4, 5, 6, 7].map((n) => (
              <option key={n} value={n}>
                {n} {n === 1 ? "message" : "messages"}
              </option>
            ))}
          </select>
          <span className="hint">You can change each step&apos;s angle and timing after creating the campaign.</span>
        </div>
      )}

      {launched ? (
        <p className="notice notice-plain">
          This campaign is launched, so its schedule and sender are managed in {initial?.channel === "email" ? "Instantly" : "HeyReach"} now.
        </p>
      ) : (
        <>
          <div className="field">
            <label htmlFor="c-sender">Send as</label>
            <select id="c-sender" className="input select" value={sender} onChange={(e) => setSender(e.target.value)}>
              <option value="">Choose a sender…</option>
              {senders.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.display_name}
                  {s.instantly_eaccount ? ` (${s.instantly_eaccount})` : ""}
                </option>
              ))}
            </select>
            {senders.length === 0 && <span className="hint">Add a sender in Settings first. It needs a name and the mailbox or LinkedIn account.</span>}
          </div>
          <fieldset className="field">
            <legend>Sending days</legend>
            <div className="day-row">
              {DAYS.map((d) => (
                <label key={d} className="day">
                  <input
                    type="checkbox"
                    checked={days.includes(d)}
                    onChange={(e) => setDays(e.target.checked ? DAYS.filter((x) => x === d || days.includes(x)) : days.filter((x) => x !== d))}
                  />
                  <span>{DAY_LABEL[d]}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <div className="field-row">
            <div className="field">
              <label htmlFor="c-from">From</label>
              <input id="c-from" type="time" className="input" value={from} onChange={(e) => setFrom(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="c-to">Until</label>
              <input id="c-to" type="time" className="input" value={to} onChange={(e) => setTo(e.target.value)} />
            </div>
            <div className="field" style={{ flex: 2 }}>
              <label htmlFor="c-tz">Time zone</label>
              <select id="c-tz" className="input select" value={timezone} onChange={(e) => setTimezone(e.target.value)}>
                {allZones.map((z) => (
                  <option key={z} value={z}>
                    {z}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="field">
            <label htmlFor="c-limit">New buyers a day</label>
            <input
              id="c-limit"
              type="number"
              className="input"
              min={1}
              max={1000}
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
              style={{ maxWidth: 140 }}
            />
          </div>
        </>
      )}

      <div className="field">
        <label htmlFor="c-attachment">Link for interested buyers (optional)</label>
        <input
          id="c-attachment"
          className="input"
          type="url"
          placeholder="https://"
          value={attachment}
          onChange={(e) => setAttachment(e.target.value)}
        />
        <span className="hint">A catalogue or price list. Gazi adds it only to replies for buyers who are interested or ask a question.</span>
      </div>
      <div className="field">
        <label htmlFor="c-reports">Weekly report goes to</label>
        <input
          id="c-reports"
          className="input"
          placeholder="you@company.com, colleague@company.com"
          value={reportEmails}
          onChange={(e) => setReportEmails(e.target.value)}
        />
      </div>

      {error && (
        <p className="notice notice-error" role="alert">
          {error}
        </p>
      )}
      <div>
        <button className="btn btn-primary" type="submit" disabled={busy || !name.trim() || days.length === 0}>
          {busy ? "Saving…" : submitLabel}
        </button>
      </div>
    </form>
  );
}
