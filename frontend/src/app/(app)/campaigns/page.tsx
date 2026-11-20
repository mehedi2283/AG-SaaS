"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { CHANNEL_LABEL, campaignState } from "@/components/campaigns/labels";
import { api, errorMessage, type Campaign } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

export default function CampaignsPage() {
  const { can } = useWorkspace();
  const [campaigns, setCampaigns] = useState<Campaign[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<Campaign[]>("/v1/campaigns")
      .then(setCampaigns)
      .catch((e) => setError(errorMessage(e)));
  }, []);

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1 className="title">Campaigns</h1>
          <p className="lede">Each campaign is one offer to one group of buyers, sent by email or on LinkedIn.</p>
        </div>
        {can("member") && (
          <Link href="/campaigns/new" className="btn btn-primary">
            New campaign
          </Link>
        )}
      </header>

      {error && <p className="notice notice-error">{error}</p>}
      {campaigns && campaigns.length === 0 && (
        <section className="empty">
          <p className="letter">Your first campaign starts here.</p>
          <p className="muted">
            Describe what you sell, then import buyers from an Apollo export. Gazi writes each buyer their own sequence
            and sends it through Instantly or HeyReach.
          </p>
          {can("member") && (
            <div className="actions">
              <Link href="/campaigns/new" className="btn btn-primary">
                New campaign
              </Link>
            </div>
          )}
        </section>
      )}
      {campaigns && campaigns.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Campaign</th>
              <th>Channel</th>
              <th className="right">Leads</th>
              <th className="right">Replies</th>
              <th className="right">Calls booked</th>
              <th>State</th>
            </tr>
          </thead>
          <tbody>
            {campaigns.map((c) => (
              <tr key={c.id}>
                <td>
                  <Link href={`/campaigns/${c.id}`} className="cell-link row-name">
                    {c.name}
                  </Link>
                  {c.counts.pending_duplicates > 0 && (
                    <span className="row-desc">{c.counts.pending_duplicates} duplicates to review</span>
                  )}
                </td>
                <td>{CHANNEL_LABEL[c.channel]}</td>
                <td className="right num">{c.counts.leads}</td>
                <td className="right num">{c.counts.replied}</td>
                <td className="right num">{c.counts.bookings}</td>
                <td>{campaignState(c)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {!campaigns && !error && <p className="muted">Loading…</p>}
    </div>
  );
}
