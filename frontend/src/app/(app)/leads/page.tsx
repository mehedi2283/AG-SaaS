"use client";

export default function LeadsPage() {
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1 className="title">Leads</h1>
          <p className="lede">The buyers you reach, imported from Apollo or found by searching it.</p>
        </div>
      </header>
      <section className="empty">
        <p className="letter">Imported buyers will appear here.</p>
        <p className="muted">
          Importing an Apollo export arrives with the next update. Gazi keeps the file for 7 days so you can
          re-run an import, and asks you about anyone who&apos;s already in your workspace.
        </p>
      </section>
    </div>
  );
}
