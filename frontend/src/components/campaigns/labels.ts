import type { Campaign } from "@/lib/api";

export const CHANNEL_LABEL = { email: "Email", linkedin: "LinkedIn" } as const;

export const DAY_LABEL = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" } as const;

export function campaignState(c: Campaign): string {
  if (c.launch_status === "launched") return c.status === "paused" ? "Paused" : "Sending";
  if (c.launch_status === "launching") return "Launching";
  if (c.launch_status === "failed") return "Launch failed";
  return "Draft";
}

export const LEAD_STATUS_LABEL: Record<string, string> = {
  new: "New",
  generating: "Writing",
  ready: "Ready",
  pushed: "In sequence",
  replied: "Replied",
  unsubscribed: "Unsubscribed",
  bounced: "Bounced",
  do_not_contact: "Do not contact",
};
