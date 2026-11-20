"use client";

import { useEffect, useState } from "react";

import { formatDate, formatPrice, formatTokens, goToStripe } from "@/components/billing/Billing";
import { api, errorMessage, type Billing } from "@/lib/api";
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
  const { billing, can, toast, refreshBilling } = useWorkspace();
  const [busy, setBusy] = useState(false);

  // Coming back from checkout with the subscription already confirmed.
  useEffect(() => {
    const url = new URL(window.location.href);
    if (url.searchParams.get("billing") !== "success") return;
    toast("Your subscription is set up");
    url.searchParams.delete("billing");
    window.history.replaceState(null, "", url.toString());
  }, [toast]);

  if (!billing) return null;
  if (!billing.enabled) return <Allowance billing={billing} />;

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
              toast(errorMessage(e), "error");
            });
          }}
        >
          {busy ? "Opening Stripe…" : "Manage billing"}
        </button>
      )}
      <Allowance billing={billing} onChanged={refreshBilling} />
    </section>
  );
}

/** This month's AI tokens, and the monthly packs an admin can add on top of the plan. */
function Allowance({ billing, onChanged }: { billing: Billing; onChanged?: () => Promise<Billing | null> }) {
  const { can, toast } = useWorkspace();
  const [pending, setPending] = useState<string | null>(null);
  const { tokens } = billing;
  const share = tokens.cap > 0 ? Math.min(1, tokens.used / tokens.cap) : 1;
  const canBuy = billing.enabled && can("admin") && (billing.status === "active" || billing.status === "trialing");

  async function change(priceId: string, add: boolean, label: string) {
    if (add && billing.status === "trialing") {
      if (!window.confirm(`Adding ${label} ends your free trial today and starts billing for the plan and the pack.`)) return;
    }
    setPending(priceId);
    try {
      await api(add ? "/v1/billing/token-packs" : `/v1/billing/token-packs/${priceId}`, {
        method: add ? "POST" : "DELETE",
        ...(add ? { body: JSON.stringify({ price_id: priceId }) } : {}),
      });
      // Stripe confirms through a webhook; wait for the new allowance to show.
      const before = tokens.cap;
      for (let i = 0; i < 10; i++) {
        const next = await onChanged?.();
        if (!next || next.tokens.cap !== before) break;
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
      }
      toast(add ? `Added ${label}` : `Removed ${label}`);
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setPending(null);
    }
  }

  return (
    <div id="ai-allowance" style={{ marginTop: 32 }}>
      <h3 className="row-name">AI tokens this month</h3>
      <p className="row-desc">
        <span className="num">{formatTokens(tokens.used)}</span> of <span className="num">{formatTokens(tokens.cap)}</span> used.
        Resets {formatDate(tokens.resets_at)}. When they run out, AI writing and reply drafting pause; everything else keeps
        working.
      </p>
      <div className="meter" role="meter" aria-valuemin={0} aria-valuemax={tokens.cap} aria-valuenow={tokens.used} aria-label="AI tokens used">
        <span style={{ width: `${share * 100}%` }} className={share >= 1 ? "meter-full" : share >= 0.8 ? "meter-high" : undefined} />
      </div>
      {billing.packs.length > 0 && (
        <div className="rows" style={{ marginTop: 20 }}>
          {billing.packs.map((pack) => {
            const label = `+${formatTokens(pack.tokens)} tokens`;
            return (
              <div className="row" key={pack.price_id}>
                <div className="row-main">
                  <div>
                    <div className="row-name">{label} a month</div>
                    <div className="row-desc">
                      {formatPrice({ price_id: pack.price_id, unit_amount: pack.unit_amount, currency: pack.currency, interval: "month", trial_days: 0 })}
                      {pack.quantity > 0 && <>, you have {pack.quantity}</>}
                    </div>
                  </div>
                  {canBuy && (
                    <div className="row-actions">
                      {pack.quantity > 0 && (
                        <button className="btn btn-quiet" disabled={pending !== null} onClick={() => void change(pack.price_id, false, label)}>
                          Remove one
                        </button>
                      )}
                      <button className="btn" disabled={pending !== null} onClick={() => void change(pack.price_id, true, label)}>
                        {pending === pack.price_id ? "Adding…" : "Add"}
                      </button>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
      {canBuy && billing.packs.length > 0 && (
        <p className="muted small" style={{ marginTop: 10 }}>
          Packs renew monthly with your plan. Adding one charges the card on file now, for the rest of this billing period.
        </p>
      )}
    </div>
  );
}
