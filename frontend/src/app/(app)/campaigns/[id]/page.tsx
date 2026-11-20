"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { CampaignForm } from "@/components/campaigns/CampaignForm";
import { CampaignLaunch } from "@/components/campaigns/CampaignLaunch";
import { CHANNEL_LABEL, campaignState } from "@/components/campaigns/labels";
import { Messages } from "@/components/campaigns/Messages";
import { StepsEditor } from "@/components/campaigns/StepsEditor";
import { ImportPanel } from "@/components/leads/ImportPanel";
import { LeadsTable } from "@/components/leads/LeadsTable";
import { api, errorMessage, type Campaign } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

type Tab = "sequence" | "leads" | "messages" | "settings";

function sentence(c: Campaign): string {
  const n = c.counts;
  if (n.leads === 0) return "No buyers yet. Import an Apollo export to start.";
  const parts = [`${n.leads} ${n.leads === 1 ? "buyer" : "buyers"}`];
  if (n.ready) parts.push(`${n.ready} ready to send`);
  if (n.pushed) parts.push(`${n.pushed} in the sequence`);
  if (n.replied) parts.push(`${n.replied} replied`);
  if (n.bookings) parts.push(`${n.bookings} ${n.bookings === 1 ? "call" : "calls"} booked`);
  return parts.join(", ") + ".";
}

export default function CampaignPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { can, toast } = useWorkspace();
  const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("leads");
  const [refresh, setRefresh] = useState(0);

  const load = useCallback(async () => {
    try {
      setCampaign(await api<Campaign>(`/v1/campaigns/${id}`));
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  const changed = useCallback(() => {
    setRefresh((n) => n + 1);
    void load();
  }, [load]);

  if (error) {
    return (
      <div className="page">
        <p className="notice notice-error">{error}</p>
        <Link href="/campaigns" className="btn" style={{ marginTop: 16 }}>
          All campaigns
        </Link>
      </div>
    );
  }
  if (!campaign) return <div className="page muted">Loading…</div>;

  return (
    <div className="page page-wide">
      <header className="page-head">
        <div>
          <p className="muted small">
            <Link href="/campaigns" className="crumb">
              Campaigns
            </Link>
          </p>
          <h1 className="title">{campaign.name}</h1>
          <p className="lede">
            {CHANNEL_LABEL[campaign.channel]}, {campaignState(campaign).toLowerCase()}. {sentence(campaign)}
          </p>
        </div>
      </header>

      <CampaignLaunch campaign={campaign} onChanged={changed} />

      <div className="tabs" role="tablist" aria-label="Campaign">
        {(
          [
            ["leads", "Buyers"],
            ["messages", "Messages"],
            ["sequence", "Sequence"],
            ["settings", "Settings"],
          ] as [Tab, string][]
        ).map(([key, label]) => (
          <button key={key} type="button" role="tab" className="tab" aria-selected={tab === key} onClick={() => setTab(key)}>
            {label}
            {key === "leads" && <span className="tab-count num">{campaign.counts.leads}</span>}
          </button>
        ))}
      </div>

      {tab === "leads" && (
        <>
          <ImportPanel campaignId={campaign.id} onImported={changed} />
          <section className="section">
            <div className="section-head">
              <h2 className="section-title">Buyers in this campaign</h2>
            </div>
            <LeadsTable campaignId={campaign.id} refreshKey={refresh} />
          </section>
        </>
      )}
      {tab === "messages" && <Messages campaign={campaign} refreshKey={refresh} />}
      {tab === "sequence" && <StepsEditor campaign={campaign} />}
      {tab === "settings" && (
        <div style={{ maxWidth: 640 }}>
          <CampaignForm
            initial={campaign}
            submitLabel="Save changes"
            onSubmit={async (values) => {
              setCampaign(await api<Campaign>(`/v1/campaigns/${campaign.id}`, { method: "PATCH", body: JSON.stringify(values) }));
              toast("Campaign saved");
            }}
          />
          {can("admin") && campaign.launch_status === "not_launched" && (
            <section className="section">
              <h2 className="section-title">Delete campaign</h2>
              <p className="row-desc">Deletes the campaign and its buyers. This can&apos;t be undone.</p>
              <button
                className="btn btn-danger"
                style={{ marginTop: 10 }}
                onClick={async () => {
                  if (!window.confirm(`Delete ${campaign.name} and its ${campaign.counts.leads} buyers?`)) return;
                  try {
                    await api(`/v1/campaigns/${campaign.id}`, { method: "DELETE" });
                    router.push("/campaigns");
                  } catch (e) {
                    toast(errorMessage(e));
                  }
                }}
              >
                Delete campaign
              </button>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
