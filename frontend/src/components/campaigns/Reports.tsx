"use client";

import { useEffect, useState } from "react";

import { formatDate } from "@/components/billing/Billing";
import { api, errorMessage } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

type Report = {
  id: string;
  period_start: string;
  period_end: string;
  metrics: Record<string, unknown> & { replies_by_intent?: Record<string, number> };
  summary: string | null;
  sent_to: string[];
  status: "pending" | "sent" | "failed" | "skipped";
  error_code: string | null;
  sent_at: string | null;
};

const LABELS: [string, string][] = [
  ["emails_sent", "Emails sent"],
  ["buyers_in_sequence", "Buyers in the sequence"],
  ["buyers_added_this_week", "Buyers added"],
  ["replies_this_week", "Replies"],
  ["replies_sent_this_week", "Replies you sent"],
  ["calls_booked_this_week", "Calls booked"],
];

/** Weekly reports for one campaign (written every Saturday for the week before). */
export function Reports({ campaignId, recipients }: { campaignId: string; recipients: string[] }) {
  const { toast } = useWorkspace();
  const [reports, setReports] = useState<Report[] | null>(null);

  useEffect(() => {
    api<Report[]>(`/v1/reports?campaign_id=${campaignId}`)
      .then(setReports)
      .catch((e) => toast(errorMessage(e)));
  }, [campaignId, toast]);

  if (!reports) return <p className="muted">Loading…</p>;
  return (
    <div>
      <p className="muted small" style={{ marginBottom: 20 }}>
        Every Saturday Gazi writes a short report for the week before
        {recipients.length > 0 ? ` and emails it to ${recipients.join(", ")}.` : ". Add recipients in Settings to have it emailed."}
      </p>
      {reports.length === 0 && <p className="muted">The first report arrives on the Saturday after launch.</p>}
      {reports.map((r) => (
        <article key={r.id} className="report">
          <header>
            <span className="row-name">
              {formatDate(r.period_start)} to {formatDate(r.period_end)}
            </span>
            <span className="muted small">
              {r.status === "sent" && `Emailed ${r.sent_at ? formatDate(r.sent_at) : ""}`}
              {r.status === "skipped" && "Not emailed (email sending isn't set up)"}
              {r.status === "failed" && "Couldn't be emailed"}
            </span>
          </header>
          {r.summary && <p className="report-summary">{r.summary}</p>}
          <dl className="facts facts-small">
            {LABELS.filter(([key]) => typeof r.metrics[key] === "number").map(([key, label]) => (
              <span key={key} style={{ display: "contents" }}>
                <dt>{label}</dt>
                <dd className="num">{String(r.metrics[key])}</dd>
              </span>
            ))}
          </dl>
        </article>
      ))}
    </div>
  );
}
