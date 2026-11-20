"use client";

import type { ReactNode } from "react";

import { Shell } from "@/components/Shell";
import { WorkspaceForm } from "@/components/WorkspaceForm";
import { WorkspaceProvider } from "@/lib/workspace";

export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <WorkspaceProvider
      welcome={(create, signOut) => (
        <main className="center">
          <div className="welcome">
            <div className="wordmark" style={{ padding: 0 }}>
              Gazi
            </div>
            <h1 className="title" style={{ marginTop: 40 }}>
              Set up your workspace
            </h1>
            <p className="lede">
              A workspace holds your campaigns, leads and connected accounts. You can invite your team later.
            </p>
            <WorkspaceForm onCreate={create} />
            <button className="btn btn-quiet" style={{ marginTop: 16, paddingLeft: 0 }} onClick={() => void signOut()}>
              Sign out
            </button>
          </div>
        </main>
      )}
    >
      <Shell>{children}</Shell>
    </WorkspaceProvider>
  );
}
