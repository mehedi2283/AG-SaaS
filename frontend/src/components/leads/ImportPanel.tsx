"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { formatDate } from "@/components/billing/Billing";
import { ApolloSearch } from "@/components/leads/ApolloSearch";
import { DuplicateReview } from "@/components/leads/DuplicateReview";
import { api, errorMessage, uploadFile, type LeadImport } from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";

const STATUS_LABEL: Record<LeadImport["status"], string> = {
  uploaded: "Waiting",
  processing: "Importing…",
  completed: "Done",
  completed_with_errors: "Done, some rows skipped",
  failed: "Failed",
};
const ERROR_LABEL: Record<string, string> = {
  no_email_column: "The file has no email column.",
  empty_file: "The file is empty.",
  file_missing: "The file was deleted before it could be imported. Upload it again.",
  apollo_not_connected: "Apollo isn't connected. Add its key in Settings.",
  provider_rejected: "Apollo refused the search. Check the key and your credits.",
};

/** Upload an Apollo CSV into a campaign, follow the import, and review duplicates. */
export function ImportPanel({ campaignId, onImported }: { campaignId: string; onImported: () => void }) {
  const { can, toast } = useWorkspace();
  const [imports, setImports] = useState<LeadImport[] | null>(null);
  const [uploading, setUploading] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    const list = await api<LeadImport[]>(`/v1/lead-imports?campaign_id=${campaignId}`);
    setImports(list);
    return list;
  }, [campaignId]);

  useEffect(() => {
    void load().catch((e) => toast(errorMessage(e)));
  }, [load, toast]);

  // Follow running imports until they finish.
  const running = imports?.some((i) => i.status === "uploaded" || i.status === "processing");
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(async () => {
      const list = await load().catch(() => null);
      if (list && !list.some((i) => i.status === "uploaded" || i.status === "processing")) onImported();
    }, 1500);
    return () => window.clearInterval(timer);
  }, [running, load, onImported]);

  async function upload(file: File) {
    setUploading(true);
    try {
      const params = new URLSearchParams({ campaign_id: campaignId, filename: file.name });
      await uploadFile<LeadImport>(`/v1/lead-imports?${params}`, file);
      toast(`Importing ${file.name}`);
      await load();
    } catch (e) {
      toast(errorMessage(e));
    } finally {
      setUploading(false);
      if (input.current) input.current.value = "";
    }
  }

  return (
    <section className="section">
      <div className="section-head">
        <h2 className="section-title">Import buyers</h2>
        <p className="muted small">
          Upload a CSV exported from Apollo (up to 50 MB). People already in your workspace, or listed twice, are held
          for you to review; the rest are imported. Gazi keeps the file for 7 days.
        </p>
      </div>
      {can("member") && (
        <div className="inline-form">
          <input
            ref={input}
            type="file"
            accept=".csv,text/csv"
            className="sr-only"
            id="csv-file"
            onChange={(e) => e.target.files?.[0] && void upload(e.target.files[0])}
          />
          <label htmlFor="csv-file" className={`btn btn-primary${uploading ? " is-busy" : ""}`} aria-disabled={uploading}>
            {uploading ? "Uploading…" : "Choose CSV file"}
          </label>
          <ApolloSearch campaignId={campaignId} onStarted={() => void load()} />
        </div>
      )}

      {imports && imports.length > 0 && (
        <table className="table" style={{ marginTop: 20 }}>
          <thead>
            <tr>
              <th>File</th>
              <th>Status</th>
              <th className="right">Imported</th>
              <th className="right">Duplicates</th>
              <th className="right">Skipped rows</th>
            </tr>
          </thead>
          <tbody>
            {imports.map((imp) => (
              <ImportRow
                key={imp.id}
                imp={imp}
                open={open === imp.id}
                onToggle={() => setOpen(open === imp.id ? null : imp.id)}
                onResolved={() => {
                  void load();
                  onImported();
                }}
              />
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function ImportRow({
  imp,
  open,
  onToggle,
  onResolved,
}: {
  imp: LeadImport;
  open: boolean;
  onToggle: () => void;
  onResolved: () => void;
}) {
  const reviewable = imp.duplicate_rows > 0 || imp.errors.length > 0;
  return (
    <>
      <tr className={open ? "is-open" : undefined}>
        <td>
          {reviewable ? (
            <button type="button" className="cell-button" aria-expanded={open} onClick={onToggle}>
              <span className="row-name">{imp.original_filename}</span>
              <span className="row-desc">{formatDate(imp.created_at)}</span>
            </button>
          ) : (
            <>
              <span className="row-name">{imp.original_filename}</span>
              <span className="row-desc">{formatDate(imp.created_at)}</span>
            </>
          )}
        </td>
        <td>
          {imp.status === "failed" ? (
            <span className="status-off">{ERROR_LABEL[imp.error_code ?? ""] ?? "Failed"}</span>
          ) : (
            STATUS_LABEL[imp.status]
          )}
        </td>
        <td className="right num">{imp.imported_rows}</td>
        <td className="right num">
          {imp.duplicate_rows}
          {imp.pending_duplicates > 0 && <span className="row-desc">{imp.pending_duplicates} to review</span>}
        </td>
        <td className="right num">{imp.failed_rows}</td>
      </tr>
      {open && (
        <tr className="detail">
          <td colSpan={5}>
            <div className="detail-body detail-single">
              {imp.errors.length > 0 && (
                <div>
                  <h3 className="row-name">Rows that weren&apos;t imported</h3>
                  <ul className="plain-list small">
                    {imp.errors.slice(0, 20).map((e) => (
                      <li key={e.row}>
                        Row {e.row}: {e.error}
                      </li>
                    ))}
                    {imp.failed_rows > 20 && <li className="muted">and {imp.failed_rows - 20} more</li>}
                  </ul>
                </div>
              )}
              {imp.duplicate_rows > 0 && <DuplicateReview importId={imp.id} onResolved={onResolved} />}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
