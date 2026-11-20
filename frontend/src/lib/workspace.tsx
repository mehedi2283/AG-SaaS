"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import type { Session } from "@supabase/supabase-js";

import { api, errorMessage, ROLE_RANK, type Me, type Membership, type Role } from "./api";
import { supabase } from "./supabase";

type WorkspaceState = {
  me: Me;
  active: Membership | null;
  can: (role: Role) => boolean;
  switchTo: (tenantId: string) => Promise<void>;
  create: (name: string, slug: string) => Promise<void>;
  signOut: () => Promise<void>;
  toast: (message: string) => void;
};

const WorkspaceContext = createContext<WorkspaceState | null>(null);

export function useWorkspace(): WorkspaceState {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("useWorkspace must be used inside <WorkspaceProvider>");
  return value;
}

type Phase =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "no-workspace"; me: Me }
  | { kind: "ready"; me: Me };

/**
 * Signs the user in, loads /v1/me and makes sure a workspace is active. The active workspace lives in the
 * access token (added by the database hook), so switching asks the API to record the choice and then
 * refreshes the session to get a token that carries it.
 */
export function WorkspaceProvider({
  children,
  welcome,
}: {
  children: ReactNode;
  welcome: (create: WorkspaceState["create"], signOut: () => Promise<void>) => ReactNode;
}) {
  const router = useRouter();
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const [toastMessage, setToastMessage] = useState<string | null>(null);

  const toast = useCallback((message: string) => {
    setToastMessage(message);
    window.setTimeout(() => setToastMessage((current) => (current === message ? null : current)), 2600);
  }, []);

  const activate = useCallback(async (tenantId: string) => {
    await api("/v1/session/active-tenant", { method: "POST", body: JSON.stringify({ tenant_id: tenantId }) });
    const { error } = await supabase.auth.refreshSession();
    if (error) throw error;
  }, []);

  const load = useCallback(async () => {
    try {
      let me = await api<Me>("/v1/me");
      if (!me.active_tenant_id) {
        const first = me.workspaces.find((w) => w.membership_status === "active");
        if (!first) return setPhase({ kind: "no-workspace", me });
        await activate(first.tenant_id);
        me = await api<Me>("/v1/me");
      }
      setPhase({ kind: "ready", me });
    } catch (error) {
      setPhase({ kind: "error", message: errorMessage(error) });
    }
  }, [activate]);

  useEffect(() => {
    let cancelled = false;
    supabase.auth.getSession().then(({ data }: { data: { session: Session | null } }) => {
      if (cancelled) return;
      if (!data.session) router.replace("/sign-in");
      else void load();
    });
    const { data: sub } = supabase.auth.onAuthStateChange((event) => {
      if (event === "SIGNED_OUT") router.replace("/sign-in");
    });
    return () => {
      cancelled = true;
      sub.subscription.unsubscribe();
    };
  }, [load, router]);

  const switchTo = useCallback(
    async (tenantId: string) => {
      setPhase({ kind: "loading" });
      try {
        await activate(tenantId);
      } catch (error) {
        setPhase({ kind: "error", message: errorMessage(error) });
        return;
      }
      await load();
    },
    [activate, load],
  );

  const create = useCallback(
    async (name: string, slug: string) => {
      const workspace = await api<Membership>("/v1/tenants", {
        method: "POST",
        body: JSON.stringify({ name, slug }),
      });
      await switchTo(workspace.tenant_id);
    },
    [switchTo],
  );

  const signOut = useCallback(async () => {
    await supabase.auth.signOut();
  }, []);

  const value = useMemo<WorkspaceState | null>(() => {
    if (phase.kind !== "ready") return null;
    const active = phase.me.workspaces.find((w) => w.tenant_id === phase.me.active_tenant_id) ?? null;
    const role = phase.me.active_role;
    return {
      me: phase.me,
      active,
      can: (min: Role) => (role ? ROLE_RANK[role] >= ROLE_RANK[min] : false),
      switchTo,
      create,
      signOut,
      toast,
    };
  }, [phase, switchTo, create, signOut, toast]);

  if (phase.kind === "loading") {
    return (
      <div className="center" aria-busy="true">
        <p className="muted">Opening your workspace…</p>
      </div>
    );
  }
  if (phase.kind === "error") {
    return (
      <div className="center">
        <div className="welcome">
          <h1 className="title">Gazi couldn&apos;t load your workspace</h1>
          <p className="lede">{phase.message}</p>
          <div style={{ display: "flex", gap: 10, marginTop: 24 }}>
            <button className="btn btn-primary" onClick={() => void load()}>
              Try again
            </button>
            <button className="btn btn-quiet" onClick={() => void signOut()}>
              Sign out
            </button>
          </div>
        </div>
      </div>
    );
  }
  if (phase.kind === "no-workspace") return <>{welcome(create, signOut)}</>;

  return (
    <WorkspaceContext.Provider value={value}>
      {children}
      {toastMessage && (
        <div className="toast" role="status">
          {toastMessage}
        </div>
      )}
    </WorkspaceContext.Provider>
  );
}
