"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { BillingBanner, ChoosePlan } from "@/components/billing/Billing";
import { useWorkspace } from "@/lib/workspace";

const NAV = [
  { href: "/replies", label: "Replies" },
  { href: "/campaigns", label: "Campaigns" },
  { href: "/leads", label: "Leads" },
  { href: "/settings", label: "Settings" },
];

const ROLE_LABEL = { owner: "Owner", admin: "Admin", member: "Member", viewer: "Viewer" } as const;

export function Shell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { me, billing } = useWorkspace();
  // A workspace that has never subscribed sees the plan (with the free trial) instead of the app.
  const needsPlan = billing?.enabled && billing.access === "none";

  return (
    <div className="shell">
      <nav className="rail" aria-label="Main">
        <Link href="/replies" className="wordmark">
          Gazi
        </Link>
        <WorkspaceSwitcher />
        <div className="rail-nav">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="rail-link"
              aria-current={pathname.startsWith(item.href) ? "page" : undefined}
            >
              {item.label}
            </Link>
          ))}
        </div>
        <div className="rail-foot muted small" style={{ padding: "0 10px" }}>
          {me.email}
        </div>
      </nav>
      <main className="main">
        <BillingBanner />
        {needsPlan ? <ChoosePlan /> : children}
      </main>
    </div>
  );
}

function WorkspaceSwitcher() {
  const { me, active, switchTo, signOut } = useWorkspace();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent ? event.key === "Escape" : !ref.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);

  return (
    <div className="switcher" ref={ref}>
      <button
        type="button"
        className="switcher-button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span>
          <span className="switcher-name">{active?.name ?? "No workspace"}</span>
          <span className="switcher-role">{active ? ROLE_LABEL[active.role] : ""}</span>
        </span>
        <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
          <path d="M3 4.5 6 7.5 9 4.5" fill="none" stroke="currentColor" strokeWidth="1.4" />
        </svg>
      </button>
      {open && (
        <div className="menu" role="menu">
          {me.workspaces
            .filter((w) => w.membership_status === "active")
            .map((w) => (
              <button
                key={w.tenant_id}
                type="button"
                role="menuitem"
                className="menu-item"
                aria-current={w.tenant_id === me.active_tenant_id}
                onClick={() => {
                  setOpen(false);
                  if (w.tenant_id !== me.active_tenant_id) void switchTo(w.tenant_id);
                }}
              >
                <span>{w.name}</span>
                <span className="muted small">{ROLE_LABEL[w.role]}</span>
              </button>
            ))}
          <div className="menu-rule" />
          <Link href="/workspaces/new" role="menuitem" className="menu-item" style={{ textDecoration: "none" }} onClick={() => setOpen(false)}>
            New workspace
          </Link>
          <button type="button" role="menuitem" className="menu-item" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
