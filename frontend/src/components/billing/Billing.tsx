"use client";

import { useEffect, useState } from "react";

import { api, errorMessage, type Billing, type Redirect } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

export function formatPrice(plan: Billing["plan"]): string {
  if (!plan || plan.unit_amount == null) return "";
  const amount = new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: plan.currency.toUpperCase(),
    maximumFractionDigits: plan.unit_amount % 100 === 0 ? 0 : 2,
  }).format(plan.unit_amount / 100);
  return plan.interval ? `${amount} a ${plan.interval}` : amount;
}

export function formatDate(iso: string | null): string {
  return iso ? new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" }) : "";
}

/** Sends the browser to a Stripe-hosted page (checkout or the billing portal). */
export async function goToStripe(path: "/v1/billing/checkout" | "/v1/billing/portal"): Promise<void> {
  const { url } = await api<Redirect>(path, { method: "POST" });
  window.location.assign(url);
}

function useReturnFromCheckout(): "success" | "canceled" | null {
  const [value, setValue] = useState<"success" | "canceled" | null>(null);
  useEffect(() => {
    const param = new URLSearchParams(window.location.search).get("billing");
    if (param === "success" || param === "canceled") setValue(param);
  }, []);
  return value;
}

function clearReturnParam() {
  const url = new URL(window.location.href);
  url.searchParams.delete("billing");
  window.history.replaceState(null, "", url.toString());
}

/** Shown instead of the app while the workspace has never subscribed. */
export function ChoosePlan() {
  const { billing, can, active, refreshBilling } = useWorkspace();
  const returned = useReturnFromCheckout();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Back from Stripe: the webhook usually lands within seconds. Poll until it does.
  useEffect(() => {
    if (returned !== "success") return;
    let cancelled = false;
    setConfirming(true);
    void (async () => {
      for (let i = 0; i < 15 && !cancelled; i++) {
        const next = await refreshBilling();
        if (next && next.access !== "none") {
          clearReturnParam();
          return;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 2000));
      }
      if (!cancelled) {
        setConfirming(false);
        setError("Stripe hasn't confirmed the subscription yet. Refresh this page in a minute.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [returned, refreshBilling]);

  if (!billing?.plan) return null;
  const { plan } = billing;
  const trial = billing.trial_available ? plan.trial_days : 0;

  async function start() {
    setBusy(true);
    setError(null);
    try {
      await goToStripe("/v1/billing/checkout");
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  if (confirming) {
    return (
      <div className="page" style={{ maxWidth: 560 }} aria-busy="true">
        <h1 className="title">Confirming your subscription…</h1>
        <p className="lede">Stripe is letting Gazi know. This takes a few seconds.</p>
      </div>
    );
  }

  return (
    <div className="page" style={{ maxWidth: 560 }}>
      <header className="page-head" style={{ marginBottom: 0 }}>
        <div>
          <h1 className="title">{trial ? "Start your free trial" : "Choose your plan"}</h1>
          <p className="lede">
            {trial
              ? `Try Gazi free for ${trial} days. You won't be charged until the trial ends, and you can cancel any time before then.`
              : `${active?.name ?? "This workspace"} needs an active subscription to launch campaigns and draft replies.`}
          </p>
        </div>
      </header>

      <div className="plan">
        <div className="plan-head">
          <span className="section-title">Gazi</span>
          <span className="plan-price">{formatPrice(plan)}</span>
        </div>
        <p className="muted small">
          Per workspace. Campaigns, leads, AI-written sequences and reply drafts, and weekly reports.
        </p>
      </div>

      {returned === "canceled" && !error && (
        <p className="notice notice-plain" style={{ marginTop: 20 }}>
          Checkout was canceled. Nothing was charged.
        </p>
      )}
      {error && (
        <p className="notice notice-error" role="alert" style={{ marginTop: 20 }}>
          {error}
        </p>
      )}

      {can("admin") ? (
        <div style={{ marginTop: 24 }}>
          <button className="btn btn-primary" onClick={() => void start()} disabled={busy}>
            {busy ? "Opening Stripe…" : trial ? `Start ${trial}-day free trial` : "Subscribe"}
          </button>
          <p className="muted small" style={{ marginTop: 10 }}>
            You&apos;ll add a card on Stripe&apos;s secure checkout page.
          </p>
        </div>
      ) : (
        <p className="notice notice-plain" style={{ marginTop: 24 }}>
          Ask an owner or admin of {active?.name ?? "this workspace"} to start the subscription.
        </p>
      )}
    </div>
  );
}

/** A strip above the page while payment has lapsed (grace) or the workspace is paused. */
export function BillingBanner() {
  const { billing, can, active, toast } = useWorkspace();
  const [busy, setBusy] = useState(false);
  if (!billing?.enabled || (billing.access !== "grace" && billing.access !== "paused")) return null;

  const paused = billing.access === "paused";
  const message = paused
    ? `${active?.name ?? "This workspace"} is paused. New launches and AI drafting are off until billing is fixed. Your data is safe.`
    : `Payment didn't go through. Update billing by ${formatDate(billing.grace_ends_at)} to keep launching campaigns and using AI.`;

  return (
    <div className={`banner ${paused ? "banner-danger" : "banner-warn"}`} role="status">
      <span>{message}</span>
      {can("admin") ? (
        <button
          className="btn"
          disabled={busy}
          onClick={() => {
            setBusy(true);
            const path = billing.status === "canceled" ? "/v1/billing/checkout" : "/v1/billing/portal";
            void goToStripe(path).catch((e) => {
              setBusy(false);
              toast(errorMessage(e));
            });
          }}
        >
          {billing.status === "canceled" ? "Resubscribe" : "Update billing"}
        </button>
      ) : (
        <span className="small">Ask an admin to update billing.</span>
      )}
    </div>
  );
}
