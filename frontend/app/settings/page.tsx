"use client";

import { Suspense, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Bell,
  Check,
  Copy,
  KeyRound,
  Loader2,
  Lock,
  Mail,
  Paintbrush,
  Plus,
  RefreshCw,
  Settings as SettingsIcon,
  Trash2,
  User as UserIcon,
  Users as UsersIcon,
} from "lucide-react";
import { api, type Invite, type PersonalAccessTokenCreated } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";
import { TabBar, useUrlTab } from "@/components/ui/tabs";

/**
 * /settings — four-tab control panel for the signed-in operator.
 *
 *  1. Profile      — name + email + sign-out.
 *  2. Appearance   — theme toggle (persists via ThemeProvider).
 *  3. Notifications — per-channel toggles stored in user.preferences.
 *  4. API tokens   — opaque PATs for scripts/CI. Cleartext shown once.
 *
 * Tabs are deliberately stateful-in-URL (?tab=...) so the sidebar link
 * always lands on a deterministic section and copy-pasting a URL
 * lands the colleague on the right place.
 */

type TabId = "profile" | "team" | "appearance" | "notifications" | "tokens";

const TABS: { id: TabId; label: string; icon: typeof UserIcon }[] = [
  { id: "profile", label: "Profile", icon: UserIcon },
  { id: "team", label: "Team", icon: UsersIcon },
  { id: "appearance", label: "Appearance", icon: Paintbrush },
  { id: "notifications", label: "Notifications", icon: Bell },
  { id: "tokens", label: "API tokens", icon: KeyRound },
];

const TAB_IDS = TABS.map((t) => t.id);

export default function SettingsPage() {
  // useUrlTab reads ?tab=, which needs a Suspense boundary.
  return (
    <Suspense fallback={null}>
      <SettingsPageInner />
    </Suspense>
  );
}

function SettingsPageInner() {
  const [tab, switchTab] = useUrlTab<TabId>(TAB_IDS, "profile");

  return (
    <div className="space-y-6 max-w-[1100px]">
      <div>
        <h1 className="text-2xl font-semibold flex items-center gap-2">
          <SettingsIcon className="h-5 w-5 text-accent-cyan" /> Settings
        </h1>
        <p className="text-muted text-sm mt-1">
          Account, appearance, notifications and API tokens.
        </p>
      </div>

      <TabBar
        tabs={TABS.map(({ id, label, icon }) => ({ id, label, icon }))}
        active={tab}
        onChange={switchTab}
        ariaLabel="Settings"
      />

      <div className="pt-2">
        {tab === "profile" && <ProfileTab />}
        {tab === "team" && <TeamTab />}
        {tab === "appearance" && <AppearanceTab />}
        {tab === "notifications" && <NotificationsTab />}
        {tab === "tokens" && <TokensTab />}
      </div>
    </div>
  );
}

// ── Profile ─────────────────────────────────────────────────────────────

function ProfileTab() {
  const { user, refresh, logout } = useAuth();
  const [fullName, setFullName] = useState(user?.full_name ?? "");
  const [currentPw, setCurrentPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [pwError, setPwError] = useState<string | null>(null);
  const [pwOk, setPwOk] = useState(false);

  const saveProfile = useMutation({
    mutationFn: () => api.auth.updateMe({ full_name: fullName.trim() || null }),
    onSuccess: () => refresh(),
  });
  const savePassword = useMutation({
    mutationFn: () => api.auth.changePassword(currentPw, newPw),
    onSuccess: () => {
      setCurrentPw("");
      setNewPw("");
      setPwError(null);
      setPwOk(true);
      setTimeout(() => setPwOk(false), 4000);
    },
    onError: (err: Error) => {
      setPwOk(false);
      setPwError(extractDetail(err) ?? "could not change password");
    },
  });

  if (!user) return <SectionEmpty />;

  return (
    <div className="space-y-6">
      <Section
        title="Identity"
        description="How you appear inside the platform. Your email is your sign-in and can only be changed by an admin."
      >
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Field label="Email">
            <ReadOnlyField icon={Mail} value={user.email} />
          </Field>
          <Field label="Display name">
            <input
              type="text"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="—"
              className={inputClass}
            />
          </Field>
        </div>
        <div className="flex justify-end gap-2 pt-3">
          <button
            type="button"
            onClick={() => saveProfile.mutate()}
            disabled={saveProfile.isPending || fullName === (user.full_name ?? "")}
            className={primaryBtn}
          >
            {saveProfile.isPending ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> Saving…
              </>
            ) : saveProfile.isSuccess ? (
              <>
                <Check className="h-3.5 w-3.5" /> Saved
              </>
            ) : (
              "Save profile"
            )}
          </button>
        </div>
      </Section>

      <Section
        title="Password"
        description="Updates require your current password. New password must be at least 8 characters."
      >
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Field label="Current password">
            <input
              type="password"
              value={currentPw}
              onChange={(e) => setCurrentPw(e.target.value)}
              autoComplete="current-password"
              className={inputClass}
            />
          </Field>
          <Field label="New password">
            <input
              type="password"
              value={newPw}
              onChange={(e) => setNewPw(e.target.value)}
              autoComplete="new-password"
              className={inputClass}
            />
          </Field>
        </div>
        {pwError && <BannerError message={pwError} />}
        {pwOk && <BannerOk message="Password updated." />}
        <div className="flex justify-end pt-3">
          <button
            type="button"
            onClick={() => savePassword.mutate()}
            disabled={
              savePassword.isPending || !currentPw || newPw.length < 8
            }
            className={primaryBtn}
          >
            {savePassword.isPending ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> Updating…
              </>
            ) : (
              "Change password"
            )}
          </button>
        </div>
      </Section>

      <Section
        title="Session"
        description="Sign out of this browser. Other sessions (including API tokens) are unaffected."
      >
        <div className="flex justify-end">
          <button type="button" onClick={() => logout()} className={dangerBtn}>
            Sign out
          </button>
        </div>
      </Section>
    </div>
  );
}

// ── Appearance ──────────────────────────────────────────────────────────

function AppearanceTab() {
  const { theme, setTheme } = useTheme();
  const { user, refresh } = useAuth();
  const prefs = (user?.preferences ?? {}) as Record<string, unknown>;
  const density = (prefs.density as "compact" | "comfortable") ?? "comfortable";

  const saveDensity = useMutation({
    mutationFn: (next: "compact" | "comfortable") =>
      api.auth.updateMe({ preferences: { density: next } }),
    onSuccess: () => refresh(),
  });

  return (
    <div className="space-y-6">
      <Section
        title="Theme"
        description="Dark is the default operational theme. Light is calibrated for daylight reviewing — same accent, lifted surfaces."
      >
        <div className="grid grid-cols-2 gap-3 max-w-[480px]">
          <Choice
            active={theme === "dark"}
            onClick={() => setTheme("dark")}
            label="Dark"
            hint="Default. Lower glare in dim rooms."
          />
          <Choice
            active={theme === "light"}
            onClick={() => setTheme("light")}
            label="Light"
            hint="High legibility in daylight."
          />
        </div>
      </Section>

      <Section
        title="Density"
        description="Controls how much padding sits between rows in lists and forms."
      >
        <div className="grid grid-cols-2 gap-3 max-w-[480px]">
          <Choice
            active={density === "comfortable"}
            onClick={() => saveDensity.mutate("comfortable")}
            label="Comfortable"
            hint="Default. Generous spacing."
          />
          <Choice
            active={density === "compact"}
            onClick={() => saveDensity.mutate("compact")}
            label="Compact"
            hint="More information per screen."
          />
        </div>
        {saveDensity.isPending && (
          <div className="text-xs text-muted mt-2 flex items-center gap-1.5">
            <Loader2 className="h-3 w-3 animate-spin" /> Saving…
          </div>
        )}
      </Section>
    </div>
  );
}

// ── Notifications ───────────────────────────────────────────────────────

const NOTIF_CHANNELS = [
  {
    id: "email_critical_alerts",
    label: "Email me on critical alerts",
    hint: "Sends an email when an open alert lands at severity=critical.",
  },
  {
    id: "email_daily_briefing",
    label: "Email the daily briefing",
    hint: "Morning recap of yesterday's infrastructure signals.",
  },
  {
    id: "inapp_proposal_ready",
    label: "In-app: a change proposal is ready for review",
    hint: "Subtle indicator in the right rail when a proposal needs your call.",
  },
  {
    id: "inapp_workflow_failed",
    label: "In-app: workflow failed",
    hint: "Surface failures in the activity feed.",
  },
];

function NotificationsTab() {
  const { user, refresh } = useAuth();
  const prefs = (user?.preferences ?? {}) as Record<string, unknown>;
  const notif = (prefs.notifications as Record<string, boolean>) ?? {};

  const save = useMutation({
    mutationFn: (next: Record<string, boolean>) =>
      api.auth.updateMe({ preferences: { notifications: next } }),
    onSuccess: () => refresh(),
  });

  function toggle(id: string) {
    const next = { ...notif, [id]: !notif[id] };
    save.mutate(next);
  }

  return (
    <Section
      title="Channels"
      description="What gets sent where. Defaults stay quiet — turn on what you want."
    >
      <div className="space-y-2">
        {NOTIF_CHANNELS.map((ch) => {
          const on = !!notif[ch.id];
          return (
            <label
              key={ch.id}
              className="flex items-start justify-between gap-4 p-3 rounded-lg border border-line bg-bg-elevated/30 cursor-pointer hover:border-line-strong transition-colors"
            >
              <div className="min-w-0">
                <div className="text-sm">{ch.label}</div>
                <div className="text-xs text-muted mt-0.5">{ch.hint}</div>
              </div>
              <Switch checked={on} onChange={() => toggle(ch.id)} />
            </label>
          );
        })}
      </div>
      {save.isPending && (
        <div className="text-xs text-muted mt-3 flex items-center gap-1.5">
          <Loader2 className="h-3 w-3 animate-spin" /> Saving…
        </div>
      )}
    </Section>
  );
}

// ── API tokens ──────────────────────────────────────────────────────────

function TokensTab() {
  const qc = useQueryClient();
  const tokens = useQuery({
    queryKey: ["auth", "tokens"],
    queryFn: api.auth.tokens.list,
  });
  const [name, setName] = useState("");
  const [expiresDays, setExpiresDays] = useState<string>("");
  const [justCreated, setJustCreated] =
    useState<PersonalAccessTokenCreated | null>(null);
  const [copied, setCopied] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () =>
      api.auth.tokens.create(
        name.trim(),
        expiresDays ? Math.max(1, Number(expiresDays)) : null
      ),
    onSuccess: (created) => {
      setJustCreated(created);
      setCreateError(null);
      setName("");
      setExpiresDays("");
      qc.invalidateQueries({ queryKey: ["auth", "tokens"] });
    },
    onError: (err: Error) =>
      setCreateError(extractDetail(err) ?? "could not create token"),
  });

  const revoke = useMutation({
    mutationFn: (id: string) => api.auth.tokens.revoke(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["auth", "tokens"] }),
  });

  async function copyToken() {
    if (!justCreated) return;
    await navigator.clipboard.writeText(justCreated.token);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="space-y-6">
      <Section
        title="Mint a new token"
        description="Tokens carry your identity. The cleartext value is shown exactly once — store it immediately."
      >
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Field label="Name">
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. ci-runner, my-laptop"
              className={inputClass}
            />
          </Field>
          <Field label="Expires in (days)">
            <input
              type="number"
              min={1}
              max={365 * 5}
              value={expiresDays}
              onChange={(e) => setExpiresDays(e.target.value)}
              placeholder="never"
              className={inputClass}
            />
          </Field>
        </div>
        {createError && <BannerError message={createError} />}
        <div className="flex justify-end pt-3">
          <button
            type="button"
            onClick={() => create.mutate()}
            disabled={create.isPending || !name.trim()}
            className={primaryBtn}
          >
            {create.isPending ? (
              <>
                <Loader2 className="h-3.5 w-3.5 animate-spin" /> Minting…
              </>
            ) : (
              <>
                <Plus className="h-3.5 w-3.5" /> Create token
              </>
            )}
          </button>
        </div>

        {justCreated && (
          <div className="mt-4 rounded-lg border border-accent-blue/60 bg-accent-blue/10 p-4">
            <div className="flex items-center justify-between gap-2 mb-2">
              <div className="text-sm font-medium">
                Token created — copy it now
              </div>
              <button
                onClick={() => setJustCreated(null)}
                className="text-xs text-muted hover:text-[color:var(--text)]"
              >
                Done
              </button>
            </div>
            <div className="font-mono text-xs break-all bg-[color:var(--bg-elevated)] p-3 rounded border border-line">
              {justCreated.token}
            </div>
            <button
              onClick={copyToken}
              className="mt-3 text-xs flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-line hover:border-line-strong"
            >
              {copied ? (
                <>
                  <Check className="h-3.5 w-3.5" /> Copied
                </>
              ) : (
                <>
                  <Copy className="h-3.5 w-3.5" /> Copy to clipboard
                </>
              )}
            </button>
            <div className="text-xs text-muted mt-3 flex items-start gap-1.5">
              <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
              We do not keep the cleartext. If you lose it, revoke this token
              and create a new one.
            </div>
          </div>
        )}
      </Section>

      <Section
        title="Existing tokens"
        description="Use Authorization: Bearer dpat_… on any API call. Revoking is immediate."
      >
        {tokens.isLoading && (
          <div className="text-xs text-muted">Loading…</div>
        )}
        {tokens.data && tokens.data.length === 0 && (
          <div className="text-xs text-muted">No tokens yet.</div>
        )}
        <div className="space-y-2">
          {(tokens.data ?? []).map((t) => {
            const revoked = !!t.revoked_at;
            const expired =
              t.expires_at && new Date(t.expires_at) < new Date();
            return (
              <div
                key={t.id}
                className={cn(
                  "flex items-center justify-between gap-4 p-3 rounded-lg border bg-bg-elevated/30",
                  revoked || expired
                    ? "border-line opacity-50"
                    : "border-line"
                )}
              >
                <div className="min-w-0 flex-1">
                  <div className="text-sm flex items-center gap-2">
                    <span className="truncate">{t.name}</span>
                    <span className="font-mono text-[11px] text-muted">
                      {t.prefix}…
                    </span>
                    {revoked && (
                      <span className="text-[10px] uppercase tracking-wider text-[color:var(--critical)]">
                        revoked
                      </span>
                    )}
                    {!revoked && expired && (
                      <span className="text-[10px] uppercase tracking-wider text-muted">
                        expired
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-muted mt-0.5">
                    Created {formatDate(t.created_at)}
                    {t.last_used_at && (
                      <> · last used {formatDate(t.last_used_at)}</>
                    )}
                    {t.expires_at && <> · expires {formatDate(t.expires_at)}</>}
                  </div>
                </div>
                {!revoked && (
                  <button
                    onClick={() => revoke.mutate(t.id)}
                    disabled={
                      revoke.isPending && revoke.variables === t.id
                    }
                    className="text-xs flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-line hover:border-[color:var(--critical)] hover:text-[color:var(--critical)]"
                  >
                    {revoke.isPending && revoke.variables === t.id ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <Trash2 className="h-3.5 w-3.5" />
                    )}
                    Revoke
                  </button>
                )}
              </div>
            );
          })}
        </div>
      </Section>
    </div>
  );
}

// ── Team ────────────────────────────────────────────────────────────────

function TeamTab() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [lastCreated, setLastCreated] = useState<Invite | null>(null);

  const query = useQuery({
    queryKey: ["invites", showAll],
    queryFn: () => api.invites.list(showAll),
  });

  const revokeMut = useMutation({
    mutationFn: (id: string) => api.invites.revoke(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["invites"] }),
  });

  const resendMut = useMutation({
    mutationFn: (id: string) => api.invites.resend(id),
    onSuccess: (inv) => {
      setLastCreated(inv);
      qc.invalidateQueries({ queryKey: ["invites"] });
    },
  });

  if (!user) return <SectionEmpty />;

  // Non-admins still see the section, but with a read-only message
  // explaining their tenant admin is the one who can invite.
  if (!user.is_admin) {
    return (
      <Section title="Team" description="Who else has access to this tenant.">
        <p className="text-sm text-muted">
          Only tenant admins can invite users. Ask your admin to add the
          person from Settings → Team.
        </p>
      </Section>
    );
  }

  return (
    <div className="space-y-6">
      {lastCreated?.invite_url && (
        <InviteCreatedBanner
          invite={lastCreated}
          onDismiss={() => setLastCreated(null)}
        />
      )}

      <Section
        title="Invite a teammate"
        description="Send a single-use link that expires after 7 days. They sign up under this tenant."
      >
        {creating ? (
          <InviteForm
            onCancel={() => setCreating(false)}
            onCreated={(inv) => {
              setCreating(false);
              setLastCreated(inv);
              qc.invalidateQueries({ queryKey: ["invites"] });
            }}
          />
        ) : (
          <button
            onClick={() => setCreating(true)}
            className="inline-flex items-center gap-2 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-bg-base hover:opacity-90"
          >
            <Plus className="h-4 w-4" /> Invite user
          </button>
        )}
      </Section>

      <Section
        title={showAll ? "All invites" : "Active invites"}
        description={
          showAll
            ? "Including accepted and revoked rows for audit."
            : "Pending and expired only. Toggle to include accepted / revoked."
        }
      >
        <div className="flex justify-end mb-3">
          <label className="text-xs text-muted flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={showAll}
              onChange={(e) => setShowAll(e.target.checked)}
            />
            Show all (including terminal)
          </label>
        </div>
        {query.isLoading && <SectionEmpty />}
        {query.data && query.data.length === 0 && (
          <p className="text-sm text-muted">No invites yet.</p>
        )}
        {query.data && query.data.length > 0 && (
          <ul className="space-y-2">
            {query.data.map((inv) => (
              <InviteRow
                key={inv.id}
                invite={inv}
                onRevoke={() => revokeMut.mutate(inv.id)}
                onResend={() => resendMut.mutate(inv.id)}
                busy={revokeMut.isPending || resendMut.isPending}
              />
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}

function InviteCreatedBanner({
  invite,
  onDismiss,
}: {
  invite: Invite;
  onDismiss: () => void;
}) {
  const [copied, setCopied] = useState(false);
  if (!invite.invite_url) return null;
  const delivered = invite.delivered === true;
  return (
    <div className="rounded-xl border border-accent/40 bg-accent/5 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1">
          <p className="text-sm font-medium">
            {delivered
              ? `Invite emailed to ${invite.email}`
              : `Invite link ready for ${invite.email}`}
          </p>
          <p className="text-xs text-muted mt-1">
            {delivered ? (
              <>
                They should receive it within a minute. If it doesn&apos;t
                arrive (or they need the link sooner), copy it from below.
                Shown <em>once</em>; closing this banner discards it.
              </>
            ) : (
              <>
                No SMTP integration is configured for this tenant, so we
                couldn&apos;t email them automatically. Copy the link below and
                send it yourself — or set up{" "}
                <a
                  href="/integrations"
                  className="underline text-accent-cyan"
                >
                  Integrations → Email
                </a>{" "}
                to get automatic delivery. Shown <em>once</em>; closing this
                banner discards it.
              </>
            )}
          </p>
          <div className="mt-3 flex gap-2">
            <input
              readOnly
              value={invite.invite_url}
              className="flex-1 rounded-md border border-line bg-[color:var(--bg-elevated)] px-2 py-1 text-xs font-mono"
              onFocus={(e) => e.currentTarget.select()}
            />
            <button
              onClick={() => {
                navigator.clipboard.writeText(invite.invite_url!);
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              }}
              className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs"
            >
              {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
        </div>
        <button
          onClick={onDismiss}
          className="text-xs text-muted hover:text-[color:var(--text)]"
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}

function InviteForm({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: (inv: Invite) => void;
}) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"admin" | "user">("user");
  const [message, setMessage] = useState("");
  const [error, setError] = useState<string | null>(null);

  const createMut = useMutation({
    mutationFn: () =>
      api.invites.create({
        email: email.trim().toLowerCase(),
        role,
        message: message.trim() || null,
      }),
    onSuccess: (inv) => onCreated(inv),
    onError: (e: Error) => setError(extractDetail(e) ?? e.message),
  });

  return (
    <form
      className="space-y-3 max-w-[480px]"
      onSubmit={(e) => {
        e.preventDefault();
        setError(null);
        createMut.mutate();
      }}
    >
      <Field label="Email">
        <input
          type="email"
          required
          autoFocus
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="teammate@yourcompany.com"
          className="w-full rounded-md border border-line bg-[color:var(--bg-elevated)] px-2 py-1.5 text-sm"
        />
      </Field>
      <Field label="Role">
        <select
          value={role}
          onChange={(e) => setRole(e.target.value as "admin" | "user")}
          className="w-full rounded-md border border-line bg-[color:var(--bg-elevated)] px-2 py-1.5 text-sm"
        >
          <option value="user">User — can investigate and approve proposals</option>
          <option value="admin">Admin — also invites, integrations, tokens</option>
        </select>
      </Field>
      <Field label="Optional message">
        <textarea
          rows={2}
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          placeholder={`"hey, here's the Daalu account I mentioned"`}
          className="w-full rounded-md border border-line bg-[color:var(--bg-elevated)] px-2 py-1.5 text-sm"
        />
      </Field>
      {error && (
        <p className="text-sm flex items-center gap-1.5 text-[color:var(--critical)]">
          <AlertTriangle className="h-3.5 w-3.5" />
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={createMut.isPending}
          className="inline-flex items-center gap-2 rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-bg-base hover:opacity-90 disabled:opacity-50"
        >
          {createMut.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
          Send invite
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-line px-3 py-1.5 text-sm"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

function InviteRow({
  invite,
  onRevoke,
  onResend,
  busy,
}: {
  invite: Invite;
  onRevoke: () => void;
  onResend: () => void;
  busy: boolean;
}) {
  return (
    <li className="flex items-center justify-between gap-3 rounded-md border border-line bg-bg-elevated/40 px-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-sm">
          <Mail className="h-3.5 w-3.5 text-muted" />
          <span className="truncate">{invite.email}</span>
          <span className="rounded bg-line/60 px-1.5 py-px text-xs uppercase tracking-wider text-muted">
            {invite.role}
          </span>
          <InviteStatusPill status={invite.status} />
        </div>
        <div className="text-xs text-muted mt-1">
          {invite.inviter_name ? `invited by ${invite.inviter_name} · ` : ""}
          expires {new Date(invite.expires_at).toLocaleDateString()}
        </div>
      </div>
      {invite.status === "pending" || invite.status === "expired" ? (
        <div className="flex gap-1.5">
          <button
            onClick={onResend}
            disabled={busy}
            title="Re-issue the token + re-send the email"
            className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs hover:bg-line/40 disabled:opacity-50"
          >
            <RefreshCw className="h-3 w-3" /> Resend
          </button>
          <button
            onClick={onRevoke}
            disabled={busy}
            title="Revoke this invite. Cannot be undone."
            className="inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-[color:var(--critical)] hover:bg-line/40 disabled:opacity-50"
          >
            <Trash2 className="h-3 w-3" /> Revoke
          </button>
        </div>
      ) : null}
    </li>
  );
}

function InviteStatusPill({ status }: { status: Invite["status"] }) {
  const styles: Record<Invite["status"], string> = {
    pending: "bg-accent/15 text-accent-cyan",
    expired: "bg-line/60 text-muted",
    revoked: "bg-line/60 text-muted line-through",
    accepted: "bg-emerald-500/20 text-emerald-400",
  };
  return (
    <span className={cn("rounded px-1.5 py-px text-xs uppercase tracking-wider", styles[status])}>
      {status}
    </span>
  );
}

// ── Shared building blocks ──────────────────────────────────────────────

function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-line bg-bg-elevated/40 p-5">
      <h2 className="text-sm font-medium">{title}</h2>
      {description && (
        <p className="text-xs text-muted mt-1 mb-4 max-w-[640px]">
          {description}
        </p>
      )}
      {children}
    </section>
  );
}

function SectionEmpty() {
  return (
    <div className="rounded-xl border border-line p-6 text-muted text-sm">
      Loading…
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <div className="text-xs uppercase tracking-[0.16em] text-muted mb-1.5">
        {label}
      </div>
      {children}
    </label>
  );
}

function ReadOnlyField({
  icon: Icon,
  value,
}: {
  icon: typeof Mail;
  value: string;
}) {
  return (
    <div
      className={cn(
        inputClass,
        "flex items-center gap-2 cursor-default bg-transparent border-dashed text-muted"
      )}
      aria-readonly="true"
    >
      <Icon className="h-3.5 w-3.5 shrink-0" />
      <span className="min-w-0 truncate">{value}</span>
      <Lock className="ml-auto h-3 w-3 shrink-0" aria-label="Read-only" />
    </div>
  );
}

function Switch({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: () => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={onChange}
      className={cn(
        "shrink-0 relative h-5 w-9 rounded-full transition-colors",
        checked ? "bg-accent" : "bg-fg/15"
      )}
    >
      <span
        className={cn(
          "absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform",
          checked ? "translate-x-4" : "translate-x-0.5"
        )}
      />
    </button>
  );
}

function Choice({
  active,
  onClick,
  label,
  hint,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  hint: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "text-left p-3 rounded-lg border transition-all",
        active
          ? "border-accent-blue/60 bg-accent-blue/10"
          : "border-line bg-bg-elevated/30 hover:border-line-strong"
      )}
    >
      <div className="text-sm font-medium flex items-center justify-between">
        {label}
        {active && <Check className="h-3.5 w-3.5 text-accent-blue" />}
      </div>
      <div className="text-xs text-muted mt-1">{hint}</div>
    </button>
  );
}

function BannerError({ message }: { message: string }) {
  return (
    <div className="mt-3 text-xs text-[color:var(--critical)] flex items-start gap-1.5">
      <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" /> {message}
    </div>
  );
}

function BannerOk({ message }: { message: string }) {
  return (
    <div className="mt-3 text-xs text-accent-blue flex items-start gap-1.5">
      <Check className="h-3.5 w-3.5 mt-0.5 shrink-0" /> {message}
    </div>
  );
}

const inputClass =
  "w-full h-9 px-3 rounded-lg border border-line bg-[color:var(--bg-elevated)] text-sm focus:outline-none focus:border-accent transition-colors";

const primaryBtn =
  "text-xs h-9 px-3 rounded-lg border border-accent-blue/60 bg-accent-blue/10 hover:bg-accent-blue/20 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1.5";

const dangerBtn =
  "text-xs h-9 px-3 rounded-lg border border-line hover:border-[color:var(--critical)] hover:text-[color:var(--critical)]";

function formatDate(s: string): string {
  try {
    const d = new Date(s);
    return d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return s;
  }
}

function extractDetail(err: Error): string | null {
  // Backend errors arrive as "<status> <statusText>: <body>". Pull the
  // body if it's a JSON object with a {detail} key, which is what
  // FastAPI HTTPException renders.
  const m = err.message.match(/\d+\s[^:]+:\s(.+)$/);
  if (!m) return null;
  try {
    const body = JSON.parse(m[1]) as { detail?: string };
    return body.detail ?? null;
  } catch {
    return m[1];
  }
}

