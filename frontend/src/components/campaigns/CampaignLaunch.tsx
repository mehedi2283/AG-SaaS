"use client";

import { useEffect, useState } from "react";

import { api, errorMessage, type Campaign } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

/** The next thing to do with a campaign: write messages, launch, or pause. Polls while work is running. */
export function CampaignLaunch({ campaign, onChanged }: { campaign: Campaign; onChanged: () => void }) {
  const { can, toast } = useWorkspace();
  const [busy, setBusy] = useState(false);
  const n = campaign.counts;
  const provider = campaign.channel === "email" ? "Instantly" : "HeyReach";
  const working = n.generating > 0 || campaign.launch_status === "launching";

  useEffect(() => {
    if (!working) return;
    const timer = window.setInterval(onChanged, 2500);
    return () => window.clearInterval(timer);
  }, [working, onChanged]);

  async function run(path: string, message: string) {
    setBusy(true);
    try {
      await api(path, { method: "POST" });
      toast(message);
      onChanged();
    } catch (e) {
      toast(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  if (n.leads === 0) return null;

  const launched = campaign.launch_status === "launched";
  return (
    <section className="next-step" aria-live="polite">
      <div>
        {n.generating > 0 ? (
          <>
            <p className="row-name">Writing messages…</p>
            <p className="row-desc">
              {n.ready + n.pushed} of {n.leads} buyers done. Each one gets their own sequence; you can leave this page.
            </p>
          </>
        ) : campaign.launch_status === "launching" ? (
          <>
            <p className="row-name">Launching in {provider}…</p>
            <p className="row-desc">Creating the campaign and adding {n.ready} buyers.</p>
          </>
        ) : campaign.launch_status === "failed" ? (
          <>
            <p className="row-name status-off">The launch didn&apos;t work</p>
            <p className="row-desc">{campaign.launch_error ?? "Try again."}</p>
          </>
        ) : n.new > 0 ? (
          <>
            <p className="row-name">
              {n.new} {n.new === 1 ? "buyer needs" : "buyers need"} messages
            </p>
            <p className="row-desc">
              Gazi writes every step for each buyer from your offer. Check a few in Messages before launching.
              {n.failed_messages > 0 && ` ${n.failed_messages} messages need a look.`}
            </p>
          </>
        ) : n.ready > 0 ? (
          <>
            <p className="row-name">
              {n.ready} {n.ready === 1 ? "buyer is" : "buyers are"} ready {launched ? "to add" : "to launch"}
            </p>
            <p className="row-desc">
              {launched
                ? `They'll be added to the running ${provider} campaign.`
                : `Gazi creates the campaign in ${provider} and adds them. ${provider} sends every step on your schedule.`}
            </p>
          </>
        ) : launched ? (
          <>
            <p className="row-name">{campaign.status === "paused" ? "Paused" : `Sending through ${provider}`}</p>
            <p className="row-desc">Replies land in Replies. Import more buyers any time.</p>
          </>
        ) : (
          <p className="row-desc">Import buyers to begin.</p>
        )}
      </div>
      <div className="row-actions">
        {!working && n.new > 0 && can("member") && (
          <button
            className="btn btn-primary"
            disabled={busy}
            onClick={() => void run(`/v1/campaigns/${campaign.id}/generate`, "Writing started")}
          >
            Write messages
          </button>
        )}
        {!working && n.new === 0 && n.ready > 0 && can("admin") && (
          <button
            className="btn btn-primary"
            disabled={busy}
            onClick={() => {
              if (!launched && !window.confirm(`Launch in ${provider}? Emails start going out on your schedule.`)) return;
              void run(`/v1/campaigns/${campaign.id}/launch`, launched ? "Adding buyers" : "Launching");
            }}
          >
            {launched ? `Add ${n.ready} to ${provider}` : `Launch in ${provider}`}
          </button>
        )}
        {launched && !working && can("member") && campaign.status !== "paused" && (
          <button className="btn" disabled={busy} onClick={() => void run(`/v1/campaigns/${campaign.id}/pause`, "Pausing")}>
            Pause
          </button>
        )}
        {launched && !working && can("admin") && campaign.status === "paused" && (
          <button className="btn" disabled={busy} onClick={() => void run(`/v1/campaigns/${campaign.id}/resume`, "Resuming")}>
            Resume
          </button>
        )}
      </div>
    </section>
  );
}
