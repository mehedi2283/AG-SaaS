"use client";

import { useCallback, useEffect, useState } from "react";

import { api, errorMessage, type Duplicate } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

type Resolution = "skip" | "update_existing" | "add_anyway";
type Result = { resolved: number; added: number; updated: number; skipped: number; not_possible: number };

const RESOLVED_LABEL: Record<string, string> = {
  skip: "Skipped",
  update_existing: "Updated the existing lead",
  add_anyway: "Added to this campaign",
};

/** People who were already in the workspace or appeared twice: skip, update the existing lead, or add anyway. */
export function DuplicateReview({ importId, onResolved }: { importId: string; onResolved: () => void }) {
  const { can, toast } = useWorkspace();
  const [items, setItems] = useState<Duplicate[] | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setItems(await api<Duplicate[]>(`/v1/lead-imports/${importId}/duplicates`));
  }, [importId]);

  useEffect(() => {
    void load().catch((e) => toast(errorMessage(e)));
  }, [load, toast]);

  async function resolve(resolution: Resolution, ids?: string[]) {
    setBusy(true);
    try {
      const result = await api<Result>(`/v1/lead-imports/${importId}/duplicates/resolve`, {
        method: "POST",
        body: JSON.stringify(ids ? { resolution, ids } : { resolution, all_pending: true }),
      });
      const parts = [
        result.added && `${result.added} added`,
        result.updated && `${result.updated} updated`,
        result.skipped && `${result.skipped} skipped`,
        result.not_possible && `${result.not_possible} already in this campaign`,
      ].filter(Boolean);
      toast(parts.join(", ") || "Done");
      await load();
      onResolved();
    } catch (e) {
      toast(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  if (!items) return <p className="muted small">Loading duplicates…</p>;
  const pending = items.filter((d) => d.resolution === "pending");
  const editable = can("member");

  return (
    <div>
      <h3 className="row-name">Duplicates</h3>
      <p className="row-desc">
        {pending.length > 0
          ? `${pending.length} waiting for you. Until you decide, Gazi won't write to them or send them anywhere.`
          : "All reviewed."}
      </p>
      {editable && pending.length > 1 && (
        <div className="inline-form">
          <button className="btn" disabled={busy} onClick={() => void resolve("skip")}>
            Skip all
          </button>
          <button className="btn" disabled={busy} onClick={() => void resolve("update_existing")}>
            Update all existing
          </button>
          <button className="btn" disabled={busy} onClick={() => void resolve("add_anyway")}>
            Add all anyway
          </button>
        </div>
      )}
      <ul className="plain-list" style={{ marginTop: 14 }}>
        {items.map((d) => (
          <li key={d.id} className="dup">
            <div>
              <span className="row-name">{d.incoming_email}</span>
              <span className="row-desc">
                {d.reason === "repeated_in_file"
                  ? "Listed more than once in this file"
                  : `Already in ${d.existing_campaign ?? "another campaign"}${d.existing_name ? ` as ${d.existing_name}` : ""}`}
              </span>
            </div>
            {d.resolution === "pending" ? (
              editable && (
                <div className="row-actions">
                  <button className="btn btn-quiet" disabled={busy} onClick={() => void resolve("skip", [d.id])}>
                    Skip
                  </button>
                  {d.reason === "exists_in_tenant" && (
                    <button className="btn btn-quiet" disabled={busy} onClick={() => void resolve("update_existing", [d.id])}>
                      Update existing
                    </button>
                  )}
                  <button className="btn btn-quiet" disabled={busy} onClick={() => void resolve("add_anyway", [d.id])}>
                    Add anyway
                  </button>
                </div>
              )
            ) : (
              <span className="muted small">{RESOLVED_LABEL[d.resolution]}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
