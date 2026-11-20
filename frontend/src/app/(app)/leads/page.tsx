"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { LeadsTable } from "@/components/leads/LeadsTable";
import { api, errorMessage, type Campaign } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

export default function LeadsPage() {
  const { toast } = useWorkspace();
  const [campaigns, setCampaigns] = useState<Campaign[] | null>(null);

  useEffect(() => {
    api<Campaign[]>("/v1/campaigns")
      .then(setCampaigns)
      .catch((e) => toast(errorMessage(e)));
  }, [toast]);

  const pending = campaigns?.filter((c) => c.counts.pending_duplicates > 0) ?? [];

  return (
    <div className="page page-wide">
      <header className="page-head">
        <div>
          <h1 className="title">Leads</h1>
          <p className="lede">Every buyer in your workspace. Import new ones from a campaign.</p>
        </div>
      </header>

      {campaigns && campaigns.length === 0 && (
        <section className="empty">
          <p className="letter">Buyers belong to a campaign.</p>
          <p className="muted">Create a campaign first, then import an Apollo export into it.</p>
          <div className="actions">
            <Link href="/campaigns/new" className="btn btn-primary">
              New campaign
            </Link>
          </div>
        </section>
      )}

      {pending.length > 0 && (
        <p className="notice notice-plain" style={{ marginBottom: 24 }}>
          Duplicates are waiting for review in{" "}
          {pending.map((c, i) => (
            <span key={c.id}>
              {i > 0 && ", "}
              <Link href={`/campaigns/${c.id}`}>{c.name}</Link> ({c.counts.pending_duplicates})
            </span>
          ))}
          .
        </p>
      )}

      {campaigns && campaigns.length > 0 && <LeadsTable campaigns={campaigns} />}
    </div>
  );
}
