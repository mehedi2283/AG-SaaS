"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import type { Session } from "@supabase/supabase-js";

import { api, errorMessage, ROLE_RANK, type Billing, type Me, type Membership, type Role } from "./api";
import { supabase } from "./supabase";
import { SkeletonShell } from "@/components/Skeleton";

type WorkspaceState = {
  me: Me;
  active: Membership | null;
  /** Null if billing couldn't be loaded; the API still enforces it, so the app just doesn't gate. */
  billing: Billing | null;
  refreshBilling: () => Promise<Billing | null>;
  can: (role: Role) => boolean;
  switchTo: (tenantId: string) => Promise<void>;
  create: (name: string, slug: string) => Promise<void>;
  signOut: () => Promise<void>;
  /** "error" stays longer, can be dismissed and is announced at once. */
  toast: (message: string, tone?: ToastTone) => void;
};

type ToastTone = "done" | "error";
type Toast = { id: number; message: string; tone: ToastTone; leaving: boolean };

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
  | { kind: "ready"; me: Me; billing: Billing | null };

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
  const [toastState, setToastState] = useState<Toast | null>(null);
  const toastTimers = useRef<number[]>([]);

  // Hide fades out first; only the toast that set the timer may clear it.
  const hideToast = useCallback((id: number) => {
    setToastState((current) => (current?.id === id ? { ...current, leaving: true } : current));
    toastTimers.current.push(
      window.setTimeout(() => setToastState((current) => (current?.id === id ? null : current)), 160),
    );
  }, []);

  const toast = useCallback(
    (message: string, tone: ToastTone = "done") => {
      const id = Date.now();
      setToastState({ id, message, tone, leaving: false });
      toastTimers.current.push(window.setTimeout(() => hideToast(id), tone === "error" ? 7000 : 2800));
    },
    [hideToast],
  );

  useEffect(() => () => toastTimers.current.forEach(window.clearTimeout), []);

  const activate = useCallback(async (tenantId: string) => {
    await api("/v1/session/active-tenant", { method: "POST", body: JSON.stringify({ tenant_id: tenantId }) });
    const { error } = await supabase.auth.refreshSession();
    if (error) throw error;
  }, []);

  const fetchBilling = useCallback(async (): Promise<Billing | null> => {
    try {
      return await api<Billing>("/v1/billing");
    } catch {
      return null;
    }
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
      setPhase({ kind: "ready", me, billing: await fetchBilling() });
    } catch (error) {
      setPhase({ kind: "error", message: errorMessage(error) });
    }
  }, [activate, fetchBilling]);

  const refreshBilling = useCallback(async () => {
    const billing = await fetchBilling();
    setPhase((current) => (current.kind === "ready" ? { ...current, billing } : current));
    return billing;
  }, [fetchBilling]);

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
      billing: phase.billing,
      refreshBilling,
      can: (min: Role) => (role ? ROLE_RANK[role] >= ROLE_RANK[min] : false),
      switchTo,
      create,
      signOut,
      toast,
    };
  }, [phase, refreshBilling, switchTo, create, signOut, toast]);

  if (phase.kind === "loading") return <SkeletonShell />;
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
      {toastState && (
        <div
          key={toastState.id}
          className={`toast toast-${toastState.tone}${toastState.leaving ? " is-leaving" : ""}`}
          role={toastState.tone === "error" ? "alert" : "status"}
        >
          <span className="toast-dot" aria-hidden="true" />
          <span>{toastState.message}</span>
          {toastState.tone === "error" && (
            <button className="toast-close" aria-label="Dismiss" onClick={() => hideToast(toastState.id)}>
              ×
            </button>
          )}
        </div>
      )}
    </WorkspaceContext.Provider>
  );
}
