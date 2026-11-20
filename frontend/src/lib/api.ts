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
