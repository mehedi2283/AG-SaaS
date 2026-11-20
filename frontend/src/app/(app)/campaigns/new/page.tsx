"use client";

import { useRouter } from "next/navigation";

import { CampaignForm } from "@/components/campaigns/CampaignForm";
import { api, type Campaign } from "@/lib/api";

export default function NewCampaignPage() {
  const router = useRouter();
  return (
    <div className="page" style={{ maxWidth: 640 }}>
      <header className="page-head" style={{ marginBottom: 8 }}>
        <div>
          <h1 className="title">New campaign</h1>
          <p className="lede">
            What you tell Gazi here is what it writes from. Be specific: who you sell to, what makes you different, and
            what you&apos;d never claim.
          </p>
        </div>
      </header>
      <CampaignForm
        submitLabel="Create campaign"
        onSubmit={async (values) => {
          const created = await api<Campaign>("/v1/campaigns", { method: "POST", body: JSON.stringify(values) });
          router.push(`/campaigns/${created.id}`);
        }}
      />
    </div>
  );
}
