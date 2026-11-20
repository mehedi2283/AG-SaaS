"use client";

import { useEffect, useState } from "react";

import { formatDate, formatPrice, goToStripe } from "@/components/billing/Billing";
import { errorMessage } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

const STATUS_LABEL: Record<string, string> = {
  trialing: "Free trial",
  active: "Active",
  past_due: "Payment failed",
  unpaid: "Unpaid",
  canceled: "Canceled",
  paused: "Paused",
  incomplete: "Waiting for payment",
  incomplete_expired: "Checkout expired",
  none: "Not subscribed",
};

export function BillingSection() {
  const { billing, can, toast } = useWorkspace();
  const [busy, setBusy] = useState(false);

  // Coming back from checkout with the subscription already confirmed.
  useEffect(() => {
    const url = new URL(window.location.href);
    if (url.searchParams.get("billing") !== "success") return;
    toast("Your subscription is set up");
    url.searchParams.delete("billing");
    window.history.replaceState(null, "", url.toString());
  }, [toast]);

  if (!billing?.enabled) return null;

  let renewal: string | null = null;
  if (billing.status === "trialing" && billing.trial_end) {
    renewal = `Trial ends ${formatDate(billing.trial_end)}${billing.cancel_at_period_end ? ". It won't renew." : ", then billing starts."}`;
  } else if (billing.cancel_at_period_end && billing.current_period_end) {
    renewal = `Ends ${formatDate(billing.current_period_end)}`;
  } else if (billing.status === "active" && billing.current_period_end) {
    renewal = `Renews ${formatDate(billing.current_period_end)}`;
  }

  return (
    <section className="section">
      <div className="section-head">
        <h2 className="section-title">Billing</h2>
        <p className="muted small">Card, invoices and cancelling are handled on Stripe&apos;s secure billing page.</p>
      </div>
      <dl className="facts">
        <dt>Plan</dt>
        <dd>Gazi, {formatPrice(billing.plan)}</dd>
        <dt>Status</dt>
        <dd>{STATUS_LABEL[billing.status] ?? billing.status}</dd>
        {renewal && (
          <>
            <dt>Next</dt>
            <dd>{renewal}</dd>
          </>
        )}
      </dl>
      {can("admin") && billing.status !== "none" && (
        <button
          className="btn"
          disabled={busy}
          onClick={() => {
            setBusy(true);
            void goToStripe("/v1/billing/portal").catch((e) => {
              setBusy(false);
              toast(errorMessage(e));
            });
          }}
        >
          {busy ? "Opening Stripe…" : "Manage billing"}
        </button>
      )}
    </section>
  );
}
