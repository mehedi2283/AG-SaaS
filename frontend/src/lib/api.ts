import { supabase } from "./supabase";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
  ) {
    super(message);
  }
}

type Problem = { detail?: string; error_code?: string; request_id?: string };

/** Calls the Gazi API through the /api proxy with the signed-in user's token. */
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      headers: {
        "content-type": "application/json",
        ...(token ? { authorization: `Bearer ${token}` } : {}),
        ...init.headers,
      },
    });
  } catch {
    throw new ApiError(0, "network_error", "Gazi's API didn't respond. Check that it's running, then try again.");
  }
  if (response.status === 204) return undefined as T;
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const problem = (body ?? {}) as Problem;
    throw new ApiError(
      response.status,
      problem.error_code ?? "unknown_error",
      problem.detail ?? `The request failed (${response.status}).`,
      problem.request_id,
    );
  }
  return body as T;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong. Try again.";
}

// ---- API shapes (backend/app/domain/*/schemas.py) ----

export type Role = "owner" | "admin" | "member" | "viewer";

export type Membership = {
  tenant_id: string;
  name: string;
  slug: string;
  role: Role;
  membership_status: "invited" | "active";
};

export type Me = {
  user_id: string;
  email: string | null;
  active_tenant_id: string | null;
  active_role: Role | null;
  workspaces: Membership[];
  is_platform_admin: boolean;
};

export type Provider = "instantly" | "heyreach" | "apollo" | "calendly";

export type Integration = {
  provider: Provider;
  key_last4: string | null;
  status: "unverified" | "active" | "invalid";
  last_verified_at: string | null;
  last_error: string | null;
  updated_at: string;
};

export type WebhookProvider = "instantly" | "heyreach" | "calendly";

export type WebhookEndpoint = {
  id: string;
  provider: WebhookProvider;
  token_prefix: string;
  has_secret: boolean;
  created_at: string;
  revoked_at: string | null;
  last_used_at: string | null;
};

export type WebhookEndpointCreated = WebhookEndpoint & { url_path: string; secret: string | null };

export type BillingAccess = "active" | "grace" | "paused" | "none";

export type Billing = {
  enabled: boolean;
  access: BillingAccess;
  status: string;
  trial_end: string | null;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  grace_ends_at: string | null;
  trial_available: boolean;
  plan: { price_id: string; unit_amount: number | null; currency: string; interval: string | null; trial_days: number } | null;
  /** This calendar month's AI allowance (UTC). Over the cap, AI writing and drafting pause. */
  tokens: { used: number; cap: number; included: number; addon: number; resets_at: string };
  packs: { price_id: string; tokens: number; unit_amount: number | null; currency: string; quantity: number }[];
};

export type AiSettings = {
  auto_reply: boolean;
  writing_model: string;
  classify_model: string;
  available_models: string[];
};

export type Redirect = { url: string };

export const ROLE_RANK: Record<Role, number> = { viewer: 1, member: 2, admin: 3, owner: 4 };

// ---- Superuser (backend/app/domain/admin/schemas.py) ----

export type AdminTenant = {
  id: string;
  name: string;
  slug: string;
  status: "active" | "suspended" | "deleted";
  created_at: string;
  members: number;
  owner_email: string | null;
  subscription_status: string;
  trial_end: string | null;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  access: BillingAccess;
  comp_until: string | null;
  addon_tokens: number;
  tokens_this_month: number;
  token_cap: number;
  stripe_customer_id: string | null;
  stripe_subscription_id: string | null;
};

export type AdminUser = {
  id: string;
  email: string;
  created_at: string;
  last_sign_in_at: string | null;
  banned_until: string | null;
  workspaces: number;
  is_platform_admin: boolean;
  login_enabled: boolean;
};

export type AdminMember = { user_id: string; email: string; role: Role; status: string; created_at: string };

export type AdminActivity = {
  source: "platform" | "workspace";
  created_at: string;
  tenant_id: string | null;
  tenant_name: string | null;
  actor_email: string | null;
  action: string;
  target_user_email: string | null;
  diff: Record<string, unknown>;
};

// ---- Campaigns, senders, leads (backend/app/domain/{campaigns,senders,leads}/schemas.py) ----

export type Channel = "email" | "linkedin";
export type Day = "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun";

export type Campaign = {
  id: string;
  name: string;
  channel: Channel;
  status: string;
  launch_status: "not_launched" | "launching" | "launched" | "failed";
  launch_error: string | null;
  launched_at: string | null;
  offer_summary: string | null;
  offer_details: string | null;
  attachment_url: string | null;
  report_emails: string[];
  timezone: string;
  sending_days: Day[];
  send_from: string; // "09:00:00"
  send_to: string;
  daily_limit: number;
  sender_identity_id: string | null;
  sequence_count: number;
  counts: {
    leads: number;
    new: number;
    generating: number;
    ready: number;
    failed_messages: number;
    pushed: number;
    replied: number;
    pending_duplicates: number;
    bookings: number;
  };
  created_at: string;
  updated_at: string;
};

export type Step = { step: number; delay_days: number; angle: string };

export type Sender = {
  id: string;
  display_name: string;
  title: string | null;
  company: string | null;
  has_signature: boolean;
  signature_image_url: string | null;
  instantly_eaccount: string | null;
  heyreach_account_id: string | null;
  is_default: boolean;
  updated_at: string;
};

export type LeadImport = {
  id: string;
  campaign_id: string;
  original_filename: string;
  status: "uploaded" | "processing" | "completed" | "completed_with_errors" | "failed";
  total_rows: number;
  imported_rows: number;
  failed_rows: number;
  duplicate_rows: number;
  pending_duplicates: number;
  errors: { row: number; error: string }[];
  error_code: string | null;
  file_kept_until: string | null;
  created_at: string;
  finished_at: string | null;
};

export type Duplicate = {
  id: string;
  incoming_email: string;
  incoming: Record<string, unknown>;
  reason: "exists_in_tenant" | "repeated_in_file";
  resolution: "pending" | "skip" | "update_existing" | "add_anyway";
  existing_lead_id: string | null;
  existing_campaign: string | null;
  existing_name: string | null;
  resolved_at: string | null;
};

export type Lead = {
  id: string;
  campaign_id: string;
  email: string;
  first_name: string | null;
  last_name: string | null;
  title: string | null;
  company: string | null;
  linkedin_url: string | null;
  website: string | null;
  industry: string | null;
  city: string | null;
  country: string | null;
  company_size: number | null;
  status: string;
  source: string;
  buyer_score: number | null;
  created_at: string;
};

export type LeadPage = { items: Lead[]; total: number };

/** Upload a file as the raw request body (the API streams it to disk). */
export async function uploadFile<T>(path: string, file: File): Promise<T> {
  return api<T>(path, { method: "POST", body: file, headers: { "content-type": file.type || "text/csv" } });
}

export type Message = {
  id: string;
  lead_id: string;
  lead_email: string;
  lead_name: string | null;
  lead_company: string | null;
  step: number;
  channel: Channel;
  subject: string | null;
  body: string | null;
  status: "pending" | "ok" | "failed" | "approved" | "pushed";
  validation_errors: string[];
  error_code: string | null;
  model: string;
  prompt_version: number;
  updated_at: string;
};

export type MessagePage = { items: Message[]; total: number };
