# 76. Private-repo authentication

A workspace can be seeded from a **private** Git repository — GitHub,
GitLab, or a self-hosted server — by supplying a **personal-access-token
(PAT)** when you create it. This chapter is the end-to-end walkthrough of
that path: where the token is entered, how it is stored, how it reaches the
`git clone`, and the security properties that fall out of the design.

Before this feature the bootstrap `git clone` ran with a bare URL and no
credentials, so only *public* repos could seed a workspace; a private URL
clone-failed silently and the user landed in an empty editor. Now the same
form takes an optional token and private repos clone cleanly.

> **Why PAT and not OAuth/SSH.** PAT-over-HTTPS is the one mechanism that
> works within the workspace pod's network posture: egress is locked to
> TCP/443 (no port 22, so SSH deploy keys can't reach the host), and a PAT
> needs no callback/redirect plumbing. A GitHub-App/OAuth integration is a
> reasonable *future* upgrade (short-lived installation tokens, per-repo
> scoping, easy revocation); this chapter documents the shipped PAT path.

## The pieces and where they live

| Piece | Path |
|---|---|
| Token field in the Create-Workspace form | `frontend/app/workspace/page.tsx` (`CreateWorkspaceForm`) |
| API spec (`git_token`, write-only) | `src/daalu_automation/api/routers/workspace.py` (`WorkspaceSpec`) |
| Controller spec + encrypt-at-rest | `src/daalu_automation/workspace_controller/app.py` |
| At-rest encryption (Fernet) | `src/daalu_automation/core/crypto.py` |
| DB column `git_token_encrypted` | `src/daalu_automation/models/workspace.py` + migration `0026_workspace_git_token` |
| Git Secret + credential-helper wiring | `src/daalu_automation/workspace_controller/k8s.py` |
| Egress allowance for HTTPS clone | `deploy/k8s/workspace-controller/namespace.yaml` |

## The flow, end to end

1. **Browser.** When the user types a repo URL, the form reveals an
   *Access token (private repos)* password field (`page.tsx`). On submit it
   sends `git_token` in the `POST /api/v1/ide/sessions` body — but only when
   both a URL and a token are present.
2. **daalu-api.** `WorkspaceSpec.git_token` (`workspace.py`) is **write-only
   and length-capped**: it is forwarded verbatim to the workspace-controller
   and is *never* a field on any response model. The browser only ever gets
   back a boolean `git_authenticated`.
3. **Controller — encrypt at rest.** `_get_or_create_workspace` (`app.py`)
   encrypts the token with `core.crypto.encrypt_secret` (Fernet, key derived
   from `settings.secret_key`) and stores the ciphertext in
   `workspaces.git_token_encrypted`. The plaintext is never persisted.
   Token semantics on update:
   * `git_token = None` → leave the stored token unchanged (the normal
     case — the browser only sends a token on first creation);
   * `git_token = ""` → clear the stored token (drops the git Secret);
   * any other value → re-encrypt, store, and force a re-provision so the
     pod picks up the new credential.
4. **Reconciler — decrypt just-in-time.** `_reconcile_one` (`app.py`)
   decrypts the ciphertext only for the duration of one `apply_workspace`
   call and passes the plaintext down. A ciphertext that won't decrypt
   (e.g. `secret_key` was rotated) is logged and treated as *no token* — the
   loop never wedges; the clone just falls back to unauthenticated and the
   init container surfaces the failure (below).
5. **K8s — Secret + credential helper.** `apply_workspace` (`k8s.py`)
   reconciles a **per-workspace Secret** `ws-<short>-git` whose single
   `credentials` key is a git credential-helper line, then builds the
   Deployment with that Secret mounted and git pointed at it.

## How the token reaches `git clone` without leaking

Two design choices keep the PAT out of every place it could leak:

**1. A credential-helper *file*, not a URL.** `_git_credentials_line`
(`k8s.py`) builds a single line in git's `store`-helper format:

```
https://<user>:<TOKEN>@<host>[:port]
```

The username is provider-specific — `x-access-token` for GitHub, `oauth2`
for GitLab, `git` for anything else — because that is what each server
expects alongside a PAT-as-password. This line is the **only** place the
token appears; it goes into the Secret via `stringData` (K8s base64s it),
mounted read-only and `0400` at `/var/run/daalu/git/credentials` on an
in-memory tmpfs-style secret volume.

The clone command itself stays **credential-free**:

```sh
git clone --branch <branch> https://github.com/acme/private.git /workspace
```

Because the remote URL carries no token, the PAT never lands in
`/workspace/.git/config` — so `git remote -v` in the user's terminal can't
reveal it, and it isn't committed by accident.

**2. `GIT_CONFIG_*` env, not a `.gitconfig`.** `_git_cred_env` (`k8s.py`)
injects:

```
GIT_CONFIG_COUNT=1
GIT_CONFIG_KEY_0=credential.helper
GIT_CONFIG_VALUE_0=store --file=/var/run/daalu/git/credentials
```

git's env-config mechanism means the helper is active in **both** the
throwaway `alpine/git` init container *and* the long-lived `code-server`
container without either sharing a HOME. The token value is never in the
environment — only the *path* to the file — so `printenv` in the user's
terminal shows nothing sensitive. Mounting into the code-server container
too is what makes terminal `git pull`/`git push` keep working after the
initial clone.

Net effect: a holder of `kubectl get deploy -o yaml` in `daalu-workspaces`
sees a Secret *reference*, never the token; the DB holds only ciphertext;
and the cloned repo's config is clean.

## Lifecycle of the credential Secret

`_apply_git_secret` (`k8s.py`) is create-or-patch-or-delete:

* token present **and** a repo URL set → create/patch `ws-<short>-git`;
* otherwise → delete it (so clearing a token, or switching to a public
  repo, removes stale credentials).

`destroy_workspace` (`k8s.py`) deletes the Secret on teardown. Pause scales
the Deployment to 0 rather than destroying, so the Secret survives a pause;
a resume re-runs `apply_workspace`, which re-derives it from the still-
encrypted DB row.

## When a private clone fails

`_bootstrap_script` (`k8s.py`) does **not** `exit 1` on a failed clone —
that would CrashLoop the pod and wedge the workspace in `provisioning`
forever. Instead it writes a sentinel the user can see in the editor:

```
/workspace/.daalu-clone-failed
  → "CLONE FAILED — if this is a private repo, check the access token
     (it may be missing, wrong, or expired) and recreate the workspace"
```

So the failure mode is a visible breadcrumb in the file tree, not a
mysteriously empty workspace. The usual causes are a missing/expired token
or a token without `repo` (GitHub) / `read_repository` (GitLab) scope.

## Network posture interaction

The egress policy already permits the clone. `namespace.yaml`'s
`allow-workspace-egress-to-gateway-and-dns` opens **TCP/443 to the public
internet, minus RFC-1918 + link-local**. That is exactly enough for
`github.com`/`gitlab.com` and not an inch more. Consequences (also covered
in [75](75-workspace-platform-internals.md#network-posture)):

* **Self-hosted / internal git** on a `10./172.16./192.168.` address is
  blocked by the RFC-1918 exclusion. To support it, an operator adds a
  targeted `ipBlock` egress rule for that host (model it on the existing
  `10.200.0.2/32` gateway rule).
* **SSH remotes** need port 22, which isn't allowed. PAT-over-HTTPS is the
  supported path.

## What to scope a token to

| Provider | Token type | Minimum scope |
|---|---|---|
| GitHub | Personal access token (classic) or fine-grained | `repo` (classic) / *Contents: read* (fine-grained) |
| GitLab | Personal/project access token | `read_repository` |
| Self-hosted (Gitea/Bitbucket/…) | Repo/app token | repository read |

Read-only is enough to *seed* a workspace. If the user intends to `git
push` from the terminal, the token needs write scope too — the same
credential helper serves push.

## Testing

`tests/test_workspace_controller.py` pins the contract:

* `_git_credentials_line` picks the right username per provider and carries
  host+port;
* `build_git_secret_spec` puts the line in `stringData`;
* `_bootstrap_script` never embeds the token;
* `build_deployment_spec(git_authenticated=True)` mounts the Secret and sets
  the credential-helper env on **both** containers, while keeping the
  code-server container's gateway/model env;
* `git_authenticated=False` adds none of it.

## See also

* [75. The workspace platform internals](75-workspace-platform-internals.md)
* [36. Coding-assistant — operator runbook](36-coding-assistant-operator.md)
* At-rest encryption: `src/daalu_automation/core/crypto.py`
