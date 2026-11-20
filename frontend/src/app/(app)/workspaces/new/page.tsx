"use client";

import { WorkspaceForm } from "@/components/WorkspaceForm";
import { useWorkspace } from "@/lib/workspace";

export default function NewWorkspacePage() {
  const { create } = useWorkspace();
  return (
    <div className="page" style={{ maxWidth: 560 }}>
      <header className="page-head" style={{ marginBottom: 8 }}>
        <div>
          <h1 className="title">New workspace</h1>
          <p className="lede">Use a separate workspace for each company you run outreach for.</p>
        </div>
      </header>
      <WorkspaceForm onCreate={create} />
    </div>
  );
}
