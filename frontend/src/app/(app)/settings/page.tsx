"use client";

import { BillingSection } from "@/components/settings/BillingSection";
import { ConnectedAccounts } from "@/components/settings/ConnectedAccounts";
import { WebhookAddresses } from "@/components/settings/WebhookAddresses";
import { useWorkspace } from "@/lib/workspace";

const ROLE_LABEL = { owner: "Owner", admin: "Admin", member: "Member", viewer: "Viewer" } as const;

export default function SettingsPage() {
  const { active, can } = useWorkspace();
  const isAdmin = can("admin");

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1 className="title">Settings</h1>
          <p className="lede">How {active?.name ?? "this workspace"} connects to the tools that send for you.</p>
        </div>
      </header>

      {isAdmin ? (
        <>
          <ConnectedAccounts />
          <WebhookAddresses />
        </>
      ) : (
        <p className="notice notice-plain">Only admins can see and change connected accounts.</p>
      )}

      <BillingSection />

      <section className="section">
        <div className="section-head">
          <h2 className="section-title">Workspace</h2>
        </div>
        <dl className="facts">
          <dt>Name</dt>
          <dd>{active?.name}</dd>
          <dt>Address</dt>
          <dd>{active?.slug}</dd>
          <dt>Your role</dt>
          <dd>{active ? ROLE_LABEL[active.role] : ""}</dd>
        </dl>
      </section>
    </div>
  );
}
