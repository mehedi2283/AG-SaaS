"use client";

import { useCallback, useEffect, useState } from "react";

import { LEAD_STATUS_LABEL } from "@/components/campaigns/labels";
import { api, errorMessage, type Campaign, type Lead, type LeadPage } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

const PAGE = 50;

/** A searchable, paged lead list, for one campaign or the whole workspace. */
export function LeadsTable({
  campaignId,
  campaigns,
  refreshKey = 0,
}: {
  campaignId?: string;
  campaigns?: Campaign[];
  refreshKey?: number;
}) {
  const { can, toast } = useWorkspace();
  const [page, setPage] = useState<LeadPage | null>(null);
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [campaign, setCampaign] = useState(campaignId ?? "");
  const [offset, setOffset] = useState(0);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    if (campaign) params.set("campaign_id", campaign);
    if (status) params.set("status", status);
    if (query) params.set("q", query);
    try {
      setPage(await api<LeadPage>(`/v1/leads?${params}`));
    } catch (e) {
      toast(errorMessage(e));
    }
  }, [campaign, status, query, offset, toast]);

  useEffect(() => {
    void load();
  }, [load, refreshKey]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setOffset(0);
      setQuery(q.trim());
    }, 300);
    return () => window.clearTimeout(timer);
  }, [q]);

  async function block(lead: Lead, blocked: boolean) {
    try {
      await api(`/v1/leads/${lead.id}`, {
        method: "PATCH",
        body: JSON.stringify({ status: blocked ? "do_not_contact" : "new" }),
      });
      toast(blocked ? `${lead.email} won't be contacted` : `${lead.email} can be contacted again`);
      await load();
    } catch (e) {
      toast(errorMessage(e));
    }
  }

  const names = new Map((campaigns ?? []).map((c) => [c.id, c.name]));

  return (
    <div>
      <div className="filters">
        <input
          className="input"
          type="search"
          placeholder="Search name, email or company"
          aria-label="Search leads"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        {!campaignId && campaigns && (
          <select
            className="input select"
            aria-label="Campaign"
            value={campaign}
            onChange={(e) => {
              setOffset(0);
              setCampaign(e.target.value);
            }}
          >
            <option value="">All campaigns</option>
            {campaigns.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        )}
        <select
          className="input select"
          aria-label="Status"
          value={status}
          onChange={(e) => {
            setOffset(0);
            setStatus(e.target.value);
          }}
        >
          <option value="">Any status</option>
          {Object.entries(LEAD_STATUS_LABEL).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </div>

      {page && page.items.length === 0 && <p className="muted" style={{ marginTop: 20 }}>No leads match.</p>}
      {page && page.items.length > 0 && (
        <>
          <table className="table" style={{ marginTop: 16 }}>
            <thead>
              <tr>
                <th>Buyer</th>
                <th>Company</th>
                {!campaignId && <th>Campaign</th>}
                <th>Status</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {page.items.map((lead) => {
                const name = [lead.first_name, lead.last_name].filter(Boolean).join(" ");
                return (
                  <tr key={lead.id}>
                    <td>
                      <span className="row-name">{name || lead.email}</span>
                      <span className="row-desc">
                        {name ? lead.email : ""}
                        {lead.title ? `${name ? ", " : ""}${lead.title}` : ""}
                      </span>
                    </td>
                    <td>
                      {lead.company ?? <span className="muted">Unknown</span>}
                      {lead.company_size != null && <span className="row-desc num">{lead.company_size} people</span>}
                    </td>
                    {!campaignId && <td>{names.get(lead.campaign_id) ?? ""}</td>}
                    <td>{LEAD_STATUS_LABEL[lead.status] ?? lead.status}</td>
                    <td className="right">
                      {can("member") &&
                        (lead.status === "do_not_contact" ? (
                          <button className="btn btn-quiet" onClick={() => void block(lead, false)}>
                            Allow contact
                          </button>
                        ) : (
                          ["new", "ready"].includes(lead.status) && (
                            <button className="btn btn-quiet btn-danger" onClick={() => void block(lead, true)}>
                              Do not contact
                            </button>
                          )
                        ))}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="pager">
            <span className="muted small num">
              {offset + 1}–{Math.min(offset + PAGE, page.total)} of {page.total}
            </span>
            <button className="btn btn-quiet" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>
              Previous
            </button>
            <button className="btn btn-quiet" disabled={offset + PAGE >= page.total} onClick={() => setOffset(offset + PAGE)}>
              Next
            </button>
          </div>
        </>
      )}
      {!page && <p className="muted">Loading…</p>}
    </div>
  );
}
