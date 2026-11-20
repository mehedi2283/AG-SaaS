"use client";

import { useCallback, useEffect, useState } from "react";

import { formatDate, formatTokens } from "@/components/billing/Billing";
import {
  api,
  errorMessage,
  type AdminActivity,
  type AdminMember,
  type AdminTenant,
  type AdminUser,
  type Role,
} from "@/lib/api";
import { useWorkspace } from "@/lib/workspace";
import { SkeletonRows } from "@/components/Skeleton";

type Tab = "workspaces" | "users" | "activity";

const ROLES: Role[] = ["owner", "admin", "member", "viewer"];
const ROLE_LABEL: Record<Role, string> = { owner: "Owner", admin: "Admin", member: "Member", viewer: "Viewer" };
const SUB_LABEL: Record<string, string> = {
  none: "No plan yet",
  trialing: "Free trial",
  active: "Paying",
  past_due: "Payment failed",
  unpaid: "Unpaid",
  canceled: "Canceled",
  paused: "Paused",
  incomplete: "Waiting for payment",
  incomplete_expired: "Checkout expired",
};

function when(iso: string | null): string {
  if (!iso) return "Never";
  return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
}

/** Superuser dashboard: every workspace and person on Gazi. Account details only, never workspace content. */
export default function AdminPage() {
  const { me } = useWorkspace();
  const [tab, setTab] = useState<Tab>("workspaces");
  const [tenants, setTenants] = useState<AdminTenant[] | null>(null);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [activity, setActivity] = useState<AdminActivity[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [t, u, a] = await Promise.all([
        api<AdminTenant[]>("/v1/admin/tenants"),
        api<AdminUser[]>("/v1/admin/users"),
        api<AdminActivity[]>("/v1/admin/activity?limit=200"),
      ]);
      setTenants(t);
      setUsers(u);
      setActivity(a);
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);

  useEffect(() => {
    if (me.is_platform_admin) void load();
  }, [me.is_platform_admin, load]);

  if (!me.is_platform_admin) {
    return (
      <div className="page">
        <h1 className="title">Superuser</h1>
        <p className="lede">This page is only for Gazi superusers.</p>
      </div>
    );
  }

  const tabs: { key: Tab; label: string; count?: number }[] = [
    { key: "workspaces", label: "Workspaces", count: tenants?.length },
    { key: "users", label: "People", count: users?.length },
    { key: "activity", label: "Activity" },
  ];

  return (
    <div className="page page-wide">
      <header className="page-head">
        <div>
          <h1 className="title">Superuser</h1>
          <p className="lede">
            Every workspace and person on Gazi. You see accounts, plans and usage. Leads, emails and replies stay
            private to each workspace.
          </p>
        </div>
      </header>

      <div className="tabs" role="tablist" aria-label="Superuser views">
        {tabs.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            className="tab"
            aria-selected={tab === t.key}
            onClick={() => setTab(t.key)}
          >
            {t.label}
            {t.count !== undefined && <span className="tab-count num">{t.count}</span>}
          </button>
        ))}
      </div>

      {error && <p className="notice notice-error">{error}</p>}
      {tab === "workspaces" && tenants && <Workspaces tenants={tenants} onChanged={load} />}
      {tab === "users" && users && <People users={users} onChanged={load} />}
      {tab === "activity" && activity && <Activity rows={activity} />}
      {!error && !tenants && <SkeletonRows />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Workspaces
// ---------------------------------------------------------------------------

function planLine(t: AdminTenant): string {
  if (t.comp_until && new Date(t.comp_until) > new Date()) return `Free access until ${formatDate(t.comp_until)}`;
  const label = SUB_LABEL[t.subscription_status] ?? t.subscription_status;
  if (t.subscription_status === "trialing" && t.trial_end) return `${label}, ends ${formatDate(t.trial_end)}`;
  if (t.cancel_at_period_end && t.current_period_end) return `${label}, ends ${formatDate(t.current_period_end)}`;
  if (t.access === "grace") return `${label}, in grace period`;
  if (t.access === "paused") return `${label}, AI and launches paused`;
  return label;
}

function Workspaces({ tenants, onChanged }: { tenants: AdminTenant[]; onChanged: () => Promise<void> }) {
  const [open, setOpen] = useState<string | null>(null);
  if (tenants.length === 0) return <p className="muted">No workspaces yet.</p>;
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Workspace</th>
          <th>Plan</th>
          <th className="right">People</th>
          <th className="right">AI tokens this month</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {tenants.map((t) => (
          <WorkspaceRow
            key={t.id}
            tenant={t}
            open={open === t.id}
            onToggle={() => setOpen(open === t.id ? null : t.id)}
            onChanged={onChanged}
          />
        ))}
      </tbody>
    </table>
  );
}

function WorkspaceRow({
  tenant: t,
  open,
  onToggle,
  onChanged,
}: {
  tenant: AdminTenant;
  open: boolean;
  onToggle: () => void;
  onChanged: () => Promise<void>;
}) {
  return (
    <>
      <tr className={open ? "is-open" : undefined}>
        <td>
          <button type="button" className="cell-button" aria-expanded={open} onClick={onToggle}>
            <span className="row-name">{t.name}</span>
            <span className="row-desc">{t.owner_email ?? t.slug}</span>
          </button>
        </td>
        <td>{planLine(t)}</td>
        <td className="right num">{t.members}</td>
        <td className="right num">
          {formatTokens(t.tokens_this_month)} <span className="muted">of {formatTokens(t.token_cap)}</span>
        </td>
        <td>
          {t.status === "suspended" ? <span className="status-off">Suspended</span> : <span className="muted">Active</span>}
        </td>
      </tr>
      {open && (
        <tr className="detail">
          <td colSpan={5}>
            <WorkspaceDetail tenant={t} onChanged={onChanged} />
          </td>
        </tr>
      )}
    </>
  );
}

function WorkspaceDetail({ tenant: t, onChanged }: { tenant: AdminTenant; onChanged: () => Promise<void> }) {
  const { toast } = useWorkspace();
  const [members, setMembers] = useState<AdminMember[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [until, setUntil] = useState("");

  const loadMembers = useCallback(async () => {
    try {
      setMembers(await api<AdminMember[]>(`/v1/admin/tenants/${t.id}/members`));
    } catch (e) {
      toast(errorMessage(e), "error");
    }
  }, [t.id, toast]);

  useEffect(() => {
    void loadMembers();
  }, [loadMembers]);

  async function run(action: () => Promise<unknown>, message: string) {
    setBusy(true);
    try {
      await action();
      toast(message);
      await Promise.all([onChanged(), loadMembers()]);
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setBusy(false);
    }
  }

  const suspended = t.status === "suspended";
  const hasFreeAccess = !!t.comp_until && new Date(t.comp_until) > new Date();

  return (
    <div className="detail-body">
      <div className="detail-col">
        <h3 className="row-name">People</h3>
        {!members ? (
          <SkeletonRows rows={2} />
        ) : (
          <ul className="plain-list">
            {members.map((m) => (
              <li key={m.user_id} className="member">
                <span className="member-email">{m.email}</span>
                <select
                  className="input select select-small"
                  aria-label={`Role for ${m.email}`}
                  value={m.role}
                  disabled={busy}
                  onChange={(e) =>
                    void run(
                      () =>
                        api(`/v1/admin/tenants/${t.id}/members/${m.user_id}`, {
                          method: "PATCH",
                          body: JSON.stringify({ role: e.target.value }),
                        }),
                      `${m.email} is now ${ROLE_LABEL[e.target.value as Role]}`,
                    )
                  }
                >
                  {ROLES.map((r) => (
                    <option key={r} value={r}>
                      {ROLE_LABEL[r]}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="btn btn-quiet btn-danger"
                  disabled={busy}
                  onClick={() => {
                    if (!window.confirm(`Remove ${m.email} from ${t.name}?`)) return;
                    void run(
                      () => api(`/v1/admin/tenants/${t.id}/members/${m.user_id}`, { method: "DELETE" }),
                      `Removed ${m.email}`,
                    );
                  }}
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="detail-col">
        <h3 className="row-name">Free access</h3>
        <p className="row-desc">
          {hasFreeAccess
            ? `Free until ${formatDate(t.comp_until)}.`
            : "Lets the workspace use Gazi without a paid plan until a date."}{" "}
          A workspace on its free trial also gets the trial moved, so Stripe won&apos;t charge before then. A paying
          subscription keeps being billed: refund or cancel it in Stripe.
        </p>
        <div className="inline-form">
          <input
            type="date"
            className="input input-date"
            aria-label="Free access until"
            value={until}
            min={new Date(Date.now() + 86_400_000).toISOString().slice(0, 10)}
            onChange={(e) => setUntil(e.target.value)}
          />
          <button
            type="button"
            className="btn"
            disabled={busy || !until}
            onClick={() =>
              void run(
                () =>
                  api(`/v1/admin/tenants/${t.id}/free-access`, {
                    method: "POST",
                    body: JSON.stringify({ until: new Date(`${until}T23:59:59Z`).toISOString() }),
                  }),
                `${t.name} has free access until ${formatDate(`${until}T23:59:59Z`)}`,
              )
            }
          >
            {hasFreeAccess ? "Change date" : "Give free access"}
          </button>
          {hasFreeAccess && (
            <button
              type="button"
              className="btn btn-quiet"
              disabled={busy}
              onClick={() =>
                void run(
                  () =>
                    api(`/v1/admin/tenants/${t.id}/free-access`, { method: "POST", body: JSON.stringify({ until: null }) }),
                  "Free access ended",
                )
              }
            >
              End free access
            </button>
          )}
        </div>

        <h3 className="row-name" style={{ marginTop: 24 }}>
          {suspended ? "Suspended" : "Suspend"}
        </h3>
        <p className="row-desc">
          {suspended
            ? "Nobody in this workspace can open it. Its data is kept."
            : "Locks everyone out of this workspace straight away. Its data is kept, and you can reactivate it."}
        </p>
        <button
          type="button"
          className={suspended ? "btn" : "btn btn-danger"}
          style={{ marginTop: 10 }}
          disabled={busy}
          onClick={() => {
            if (!suspended && !window.confirm(`Suspend ${t.name}? Everyone in it loses access now.`)) return;
            void run(
              () =>
                api(`/v1/admin/tenants/${t.id}/status`, {
                  method: "POST",
                  body: JSON.stringify({ status: suspended ? "active" : "suspended" }),
                }),
              suspended ? `${t.name} is active again` : `${t.name} is suspended`,
            );
          }}
        >
          {suspended ? "Reactivate workspace" : "Suspend workspace"}
        </button>

        <dl className="facts facts-small">
          <dt>Address</dt>
          <dd>{t.slug}</dd>
          <dt>Created</dt>
          <dd>{formatDate(t.created_at)}</dd>
          {t.stripe_customer_id && (
            <>
              <dt>Stripe customer</dt>
              <dd className="num">{t.stripe_customer_id}</dd>
            </>
          )}
        </dl>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// People
// ---------------------------------------------------------------------------

function People({ users, onChanged }: { users: AdminUser[]; onChanged: () => Promise<void> }) {
  const { me, toast } = useWorkspace();
  const [busy, setBusy] = useState<string | null>(null);

  async function setLogin(u: AdminUser, enabled: boolean) {
    if (!enabled && !window.confirm(`Disable ${u.email}? They won't be able to sign in to any workspace.`)) return;
    setBusy(u.id);
    try {
      await api(`/v1/admin/users/${u.id}/login`, { method: "POST", body: JSON.stringify({ enabled }) });
      toast(enabled ? `${u.email} can sign in again` : `${u.email} can no longer sign in`);
      await onChanged();
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setBusy(null);
    }
  }

  return (
    <>
      <table className="table">
        <thead>
          <tr>
            <th>Person</th>
            <th>Last signed in</th>
            <th className="right">Workspaces</th>
            <th>Login</th>
            <th aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td>
                <span className="row-name">{u.email}</span>
                <span className="row-desc">
                  {u.is_platform_admin ? "Superuser" : `Joined ${formatDate(u.created_at)}`}
                  {u.id === me.user_id ? " (you)" : ""}
                </span>
              </td>
              <td className="num">{when(u.last_sign_in_at)}</td>
              <td className="right num">{u.workspaces}</td>
              <td>{u.login_enabled ? <span className="muted">Enabled</span> : <span className="status-off">Disabled</span>}</td>
              <td className="right">
                {!u.is_platform_admin && (
                  <button
                    type="button"
                    className={u.login_enabled ? "btn btn-quiet btn-danger" : "btn"}
                    disabled={busy !== null}
                    onClick={() => void setLogin(u, !u.login_enabled)}
                  >
                    {busy === u.id ? "Saving…" : u.login_enabled ? "Disable login" : "Enable login"}
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small" style={{ marginTop: 12 }}>
        Disabling stops new sign-ins at once. Someone already signed in keeps access for up to an hour, until their
        session expires.
      </p>
    </>
  );
}

// ---------------------------------------------------------------------------
// Activity
// ---------------------------------------------------------------------------

const ACTION_LABEL: Record<string, string> = {
  "workspace.status_changed": "Changed workspace status",
  "workspace.free_access_changed": "Changed free access",
  "workspace.trial_extended": "Extended the Stripe trial",
  "workspace.ai_settings_changed": "Changed AI settings",
  "member.role_changed": "Changed a role",
  "member.removed": "Removed a member",
  "user.login_disabled": "Disabled a login",
  "user.login_enabled": "Enabled a login",
  "integration.saved": "Connected an account",
  "integration.deleted": "Disconnected an account",
  "billing.checkout_started": "Opened checkout",
  "billing.portal_opened": "Opened billing",
  "billing.token_pack_added": "Added a token pack",
  "billing.token_pack_removed": "Removed a token pack",
};

function detail(a: AdminActivity): string {
  const d = a.diff;
  if ("from" in d && "to" in d) return `${String(d.from ?? "none")} to ${String(d.to ?? "none")}`;
  if ("provider" in d) return String(d.provider);
  if ("tokens" in d) return `${formatTokens(Number(d.tokens))} tokens`;
  return "";
}

function Activity({ rows }: { rows: AdminActivity[] }) {
  if (rows.length === 0) return <p className="muted">Nothing has happened yet.</p>;
  return (
    <table className="table">
      <thead>
        <tr>
          <th>When</th>
          <th>Who</th>
          <th>What</th>
          <th>Workspace</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((a, i) => (
          <tr key={`${a.created_at}-${i}`}>
            <td className="num nowrap">{when(a.created_at)}</td>
            <td>
              {a.actor_email ?? <span className="muted">System</span>}
              {a.source === "platform" && <span className="row-desc">Superuser</span>}
            </td>
            <td>
              {ACTION_LABEL[a.action] ?? a.action}
              {(a.target_user_email || detail(a)) && (
                <span className="row-desc">{[a.target_user_email, detail(a)].filter(Boolean).join(", ")}</span>
              )}
            </td>
            <td>{a.tenant_name ?? <span className="muted">None</span>}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
