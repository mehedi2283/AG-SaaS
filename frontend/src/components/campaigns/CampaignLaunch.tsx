"use client";

import type { Campaign } from "@/lib/api";

/** Writing and launching (filled in with the generation and launch slices). */
export function CampaignLaunch({ campaign }: { campaign: Campaign; onChanged: () => void }) {
  if (campaign.counts.leads === 0) return null;
  return null;
}
