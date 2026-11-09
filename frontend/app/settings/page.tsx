"use client";

import { useUnsavedGuard } from "@/components/ui/use-unsaved-guard";
import { Suspense, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Bell,
  Check,
  Copy,
  Eye,
  EyeOff,
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
import { Button } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { useToast } from "@/components/ui/toast";
import { errorText } from "@/lib/errors";

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
  const [confirmPw, setConfirmPw] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [pwError, setPwError] = useState<string | null>(null);
  const toast = useToast();

  const nameDirty = !!user && fullName !== (user.full_name ?? "");
  const pwDirty = !!(currentPw || newPw || confirmPw);
  useUnsavedGuard(nameDirty || pwDirty);

  const saveProfile = useMutation({
    mutationFn: () => api.auth.updateMe({ full_name: fullName.trim() || null }),
    onSuccess: () => {
      refresh();
      toast.success("Profile saved");
    },
    onError: (err) => toast.error(errorText(err)),
  });
  const savePassword = useMutation({
    mutationFn: () => api.auth.changePassword(currentPw, newPw),
    onSuccess: () => {
      setCurrentPw("");
      setNewPw("");
      setConfirmPw("");
      setPwError(null);
      toast.success("Password changed. Use the new one next time you sign in.");
    },
    onError: (err) => {
      setPwError(errorText(err));
      toast.error(errorText(err));
    },
  });

  if (!user) return <SectionEmpty />;

  const pwTooShort = newPw.length > 0 && newPw.length < 8;
  const pwMismatch = confirmPw.length > 0 && confirmPw !== newPw;
  const pwReason = !currentPw
    ? "Enter your current password"
    : newPw.length < 8
      ? "New password needs at least 8 characters"
      : confirmPw !== newPw
        ? "The two new passwords must match"
        : undefined;

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
              placeholder="Your name"
              autoComplete="name"
              className={inputClass}
            />
          </Field>
        </div>
        <div className="flex justify-end gap-2 pt-3">
          <Button
            variant="primary"
            onClick={() => saveProfile.mutate()}
            pending={saveProfile.isPending}
            disabled={!nameDirty}
            disabledReason={saveProfile.isPending ? "Saving…" : "Change a field to save"}
          >
            Save profile
          </Button>
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
          <div className="hidden md:block" aria-hidden />
          <Field label="New password">
            <input
              type={showPw ? "text" : "password"}
              value={newPw}
              onChange={(e) => setNewPw(e.target.value)}
              autoComplete="new-password"
              aria-invalid={pwTooShort || undefined}
              aria-describedby="new-pw-hint"
              className={inputClass}
            />
            <div
              id="new-pw-hint"
              className={cn("text-xs mt-1", pwTooShort ? "text-signal-critical" : "text-muted")}
            >
              {pwTooShort
                ? `${8 - newPw.length} more character${8 - newPw.length === 1 ? "" : "s"} needed.`
                : "At least 8 characters."}
            </div>
          </Field>
          <Field label="Confirm new password">
            <input
              type={showPw ? "text" : "password"}
              value={confirmPw}
              onChange={(e) => setConfirmPw(e.target.value)}
              autoComplete="new-password"
              aria-invalid={pwMismatch || undefined}
              aria-describedby="confirm-pw-hint"
              className={inputClass}
            />
            <div
              id="confirm-pw-hint"
              aria-live="polite"
              className={cn(
                "text-xs mt-1",
                pwMismatch ? "text-signal-critical" : confirmPw && !pwMismatch ? "text-accent" : "text-muted"
              )}
            >
              {pwMismatch
                ? "Passwords don't match yet."
                : confirmPw
                  ? "Passwords match."
                  : "Type the new password again."}
            </div>
          </Field>
        </div>
        <div className="pt-2">
          <Button
            variant="ghost"
            size="sm"
            icon={showPw ? <EyeOff className="h-3.5 w-3.5" aria-hidden /> : <Eye className="h-3.5 w-3.5" aria-hidden />}
            onClick={() => setShowPw((v) => !v)}
            aria-pressed={showPw}
            // Ghost padding would indent the label; line it up with the fields.
            className="-ml-3"
          >
            {showPw ? "Hide passwords" : "Show passwords"}
          </Button>
        </div>
        {pwError && <BannerError message={pwError} />}
        <div className="flex justify-end pt-3">
          <Button
            variant="primary"
            onClick={() => savePassword.mutate()}
            pending={savePassword.isPending}
            disabled={!!pwReason}
            disabledReason={savePassword.isPending ? "Changing password…" : pwReason}
          >
            Change password
          </Button>
        </div>
      </Section>

      <Section
        title="Session"
        description="Sign out of this browser. Other sessions (including API tokens) are unaffected."
      >
        <div className="flex justify-end">
          <Button variant="danger" onClick={() => logout()}>
            Sign out
          </Button>
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
  const toast = useToast();

  const saveDensity = useMutation({
    mutationFn: (next: "compact" | "comfortable") =>
      api.auth.updateMe({ preferences: { density: next } }),
    onSuccess: (_d, next) => {
      refresh();
      toast.success(`Density set to ${next}`);
    },
    onError: (err) => toast.error(errorText(err)),
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
  const toast = useToast();

  const save = useMutation({
    mutationFn: (next: Record<string, boolean>) =>
      api.auth.updateMe({ preferences: { notifications: next } }),
    onSuccess: () => {
      refresh();
      toast.success("Notification settings saved");
    },
    onError: (err) => toast.error(errorText(err)),
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
  const toast = useToast();

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
      toast.success(`Token "${created.name}" created. Copy it now.`);
    },
    onError: (err) => setCreateError(errorText(err)),
  });

  const revoke = useMutation({
    mutationFn: (id: string) => api.auth.tokens.revoke(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["auth", "tokens"] });
      toast.success("Token revoked");
    },
    onError: (err) => toast.error(errorText(err)),
  });

  async function copyToken() {
    if (!justCreated) return;
    try {
      await navigator.clipboard.writeText(justCreated.token);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      toast.error("Couldn't copy. Select the token and copy it by hand.");
    }
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
          <Button
            variant="primary"
            onClick={() => create.mutate()}
            pending={create.isPending}
            disabled={!name.trim()}
            disabledReason={create.isPending ? "Creating…" : "Name the token to create it"}
            icon={<Plus className="h-3.5 w-3.5" aria-hidden />}
          >
            Create token
          </Button>
        </div>

        {justCreated && (
          <div className="mt-4 rounded-lg border border-accent-blue/60 bg-accent-blue/10 p-4 motion-enter">
            <div className="flex items-center justify-between gap-2 mb-2">
              <div className="text-sm font-medium">
                Token created — copy it now
              </div>
              <Button variant="ghost" size="sm" onClick={() => setJustCreated(null)}>
                Done
              </Button>
            </div>
            <div className="font-mono text-xs break-all bg-[color:var(--bg-elevated)] p-3 rounded border border-line">
              {justCreated.token}
            </div>
            <Button
              size="sm"
              onClick={copyToken}
              className="mt-3"
              icon={
                copied ? (
                  <Check className="h-3.5 w-3.5 motion-success" aria-hidden />
                ) : (
                  <Copy className="h-3.5 w-3.5" aria-hidden />
                )
              }
            >
              {copied ? "Copied" : "Copy to clipboard"}
            </Button>
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
          <div role="status" className="text-sm text-muted flex items-center gap-1.5">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> Loading tokens…
          </div>
        )}
        {tokens.isError && <BannerError message={errorText(tokens.error)} />}
        {tokens.data && tokens.data.length === 0 && (
          <div className="text-sm text-muted">
            No tokens yet. Create one above for a script or CI job.
          </div>
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
                    <span className="font-mono text-xs text-muted">
                      {t.prefix}…
                    </span>
                    {revoked && (
                      <span className="text-[11px] uppercase tracking-wider text-signal-critical">
                        revoked
                      </span>
                    )}
                    {!revoked && expired && (
                      <span className="text-[11px] uppercase tracking-wider text-muted">
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
                  <ConfirmButton
                    tone="danger"
                    label="Revoke"
                    confirmLabel="Revoke token"
                    consequence="Anything using this token stops working immediately."
                    icon={<Trash2 className="h-3.5 w-3.5" aria-hidden />}
                    pending={revoke.isPending && revoke.variables === t.id}
                    onConfirm={() => revoke.mutate(t.id)}
                  />
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

  const toast = useToast();
  const revokeMut = useMutation({
    mutationFn: (id: string) => api.invites.revoke(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["invites"] });
      toast.success("Invite revoked");
    },
    onError: (err) => toast.error(errorText(err)),
  });

  const resendMut = useMutation({
    mutationFn: (id: string) => api.invites.resend(id),
    onSuccess: (inv) => {
      setLastCreated(inv);
      qc.invalidateQueries({ queryKey: ["invites"] });
      toast.success(inv.delivered ? `Invite re-sent to ${inv.email}` : `New invite link ready for ${inv.email}`);
    },
    onError: (err) => toast.error(errorText(err)),
  });

  if (!user) return <SectionEmpty />;

  // Non-admins still see the section, but with a read-only message
  // explaining their tenant admin is the one who can invite.
  if (!user.is_admin) {
    return (
      <Section title="Team" description="Who else has access to this organisation.">
        <p className="text-sm text-muted">
          Only organisation admins can invite people. Ask your admin to add
          them from Settings → Team.
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
        description="Send a single-use link that expires after 7 days. They join this organisation."
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
          <Button
            variant="primary"
            onClick={() => setCreating(true)}
            icon={<Plus className="h-4 w-4" aria-hidden />}
          >
            Invite a teammate
          </Button>
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
        {query.isError && <BannerError message={errorText(query.error)} />}
        {query.data && query.data.length === 0 && (
          <p className="text-sm text-muted">
            {showAll ? "No invites yet." : "No pending invites. Invite a teammate above."}
          </p>
        )}
        {query.data && query.data.length > 0 && (
          <ul className="space-y-2">
            {query.data.map((inv) => (
              <InviteRow
                key={inv.id}
                invite={inv}
                onRevoke={() => revokeMut.mutate(inv.id)}
                onResend={() => resendMut.mutate(inv.id)}
                busy={
                  (revokeMut.isPending && revokeMut.variables === inv.id) ||
                  (resendMut.isPending && resendMut.variables === inv.id)
                }
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
              aria-label="Invite link"
              value={invite.invite_url}
              className="flex-1 min-w-0 rounded-md border border-line bg-[color:var(--bg-elevated)] px-2 py-1 text-xs font-mono"
              onFocus={(e) => e.currentTarget.select()}
            />
            <Button
              size="sm"
              onClick={() => {
                navigator.clipboard.writeText(invite.invite_url!).then(() => {
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1500);
                });
              }}
              icon={
                copied ? (
                  <Check className="h-3.5 w-3.5 motion-success" aria-hidden />
                ) : (
                  <Copy className="h-3.5 w-3.5" aria-hidden />
                )
              }
            >
              {copied ? "Copied" : "Copy link"}
            </Button>
          </div>
        </div>
        <Button variant="ghost" size="sm" onClick={onDismiss}>
          Dismiss
        </Button>
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
    onError: (e) => setError(errorText(e)),
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
          autoComplete="off"
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
        <p role="alert" className="text-sm flex items-center gap-1.5 text-signal-critical">
          <AlertTriangle className="h-3.5 w-3.5" aria-hidden />
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <Button
          type="submit"
          variant="primary"
          pending={createMut.isPending}
          disabled={!email.trim()}
          disabledReason={createMut.isPending ? "Sending…" : "Enter their email to send"}
        >
          Send invite
        </Button>
        <Button onClick={onCancel}>Cancel</Button>
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
          <Mail className="h-3.5 w-3.5 text-muted" aria-hidden />
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
          <Button
            size="sm"
            onClick={onResend}
            disabled={busy}
            title="Issue a new link and email it again"
            icon={<RefreshCw className="h-3 w-3" aria-hidden />}
          >
            Resend
          </Button>
          <ConfirmButton
            tone="danger"
            label="Revoke"
            confirmLabel="Revoke invite"
            consequence="The link stops working. This can't be undone."
            disabled={busy}
            icon={<Trash2 className="h-3 w-3" aria-hidden />}
            onConfirm={onRevoke}
          />
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
    accepted: "bg-accent/15 text-accent",
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
    <div role="status" className="rounded-xl border border-line p-6 text-muted text-sm flex items-center gap-2">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Loading…
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
      <div className="text-xs font-medium text-muted mb-1.5">
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
        "shrink-0 relative h-6 w-11 rounded-full transition-colors",
        checked ? "bg-accent" : "bg-fg/15"
      )}
    >
      <span
        className={cn(
          "absolute top-0.5 h-5 w-5 rounded-full bg-bg-card shadow transition-transform",
          checked ? "translate-x-[22px]" : "translate-x-0.5"
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
    <div role="alert" className="mt-3 text-sm text-signal-critical flex items-start gap-1.5">
      <AlertTriangle className="h-3.5 w-3.5 mt-0.5 shrink-0" aria-hidden /> {message}
    </div>
  );
}

const inputClass =
  "w-full h-9 px-3 rounded-lg border border-line bg-[color:var(--bg-elevated)] text-sm focus:outline-none focus:border-accent transition-colors";

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

