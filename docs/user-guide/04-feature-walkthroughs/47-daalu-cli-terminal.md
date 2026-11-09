# 47. Using Daalu from your terminal

[Chapter 26](26-coding-assistant.md) covered the browser Coding Assistant — VS
Code in a tab, the assistant in the side panel. This chapter covers the other
way to use the same agent: the **`daalu` command-line tool**, which you install
on your own machine and run inside the terminal of your favourite editor, over
the code that's already on your disk.

If you've used Claude Code or the GitHub CLI, this will feel familiar: install
it once, log in from the terminal, and run `daalu` in any project folder.

It is the *same* agent and the *same* privacy guarantee as the browser
Workspace — your prompts and code go only to your own GPU or the Daalu-hosted
pool, never to a public LLM. The difference is purely *where* you work: the CLI
edits the files on your laptop, across as many repositories as you like, with no
need to push code anywhere first.

---

## When to use the CLI instead of the browser IDE

| Use the **browser Workspace** when… | Use the **`daalu` CLI** when… |
|---|---|
| You want a ready-made editor with nothing to install | You already have your editor + tools set up locally |
| You're working from a machine you don't control | You're on your own dev machine |
| You want to seed from one Git repo and stay in it | You jump between several local repos / uncommitted work |

You can use both — they share your account and your inference tier.

---

## Install

Pick the line that matches your machine. Re-run it any time to upgrade.

**Linux / macOS**

```sh
curl -fsSL https://get.daalu.io/install.sh | sh
```

**macOS (Homebrew)**

```sh
brew install daalu-io/tap/daalu
```

**Any machine with Python 3.9+**

```sh
pipx install daalu-cli
```

**Windows (PowerShell)**

```powershell
irm https://get.daalu.io/install.ps1 | iex
```

Check it landed:

```sh
daalu --version
```

> If `daalu` isn't found after installing, add the install directory to your
> `PATH` — the installer prints the exact line to add.

---

## Log in

You have two ways to sign in. Both end with your machine holding a token that
acts as you until you revoke it.

### Option A — `daalu login` (opens your browser)

```sh
daalu login
```

This prints a short code and opens your browser to an **Authorize this
terminal?** page. Check that the code on the page matches the one in your
terminal, click **Authorize this terminal**, return to your terminal, and
you're signed in. (If you didn't just run `daalu login`, click **Deny — don't
sign in** instead; no token is issued.) If
your company runs Daalu at its own address, point at it:

```sh
daalu login --host https://ops.your-company.com
```

### Option B — paste a one-time command (no browser step)

1. In the Daalu web app, open **Workspace** in the left sidebar (under
   *Infrastructure*).
2. Find the **Use Daalu from your terminal** card and click **Generate one-time
   login command**.
3. Copy the `daalu login --code …` command it shows and paste it into your
   terminal.

The command carries a one-time, 10-minute login code tied to your account, so it
logs you in immediately — handy when the machine you're on can't easily open a
browser.

Either way, confirm you're in:

```sh
daalu status
```

---

## Use it

Change into any project and start the agent:

```sh
cd ~/src/my-project
daalu
```

You'll get a prompt. Ask it to explain code, make a change, or run something.
The agent can:

- **Read** files in the project (no confirmation — it's your code).
- **Propose a change** — it shows you a diff and asks **y/N** before writing.
- **Run a command** — it shows the command and asks **y/N** before running it.

Nothing is written or executed without your yes. And the agent always
finishes with an answer: on a big task ("explain this codebase") it may
spend many turns reading files, but when it reaches its per-turn tool
budget it stops, summarises what it found, and tells you if it ran out of
room — it never silently drops you back at the prompt mid-thought.

Useful in-session commands:

| In the `daalu` prompt | |
|---|---|
| `/help` | list commands |
| `/reset` | clear the conversation and start fresh |
| `/quit` | leave |

Other things you can do from the shell:

```sh
daalu models     # list the coding models available to you
daalu status     # which instance + model you're using
daalu logout     # remove the stored login from this machine
```

---

## Your privacy, on your laptop too

The CLI sends your prompts to your operator's Daalu instance, which routes them
**only** to your own GPU or the Daalu-hosted pool — the same rule the browser
Workspace follows. Coding prompts are never sent to an external AI provider.
The agent only ever touches files **inside the project folder you started it
in** — it refuses paths that escape that folder.

Your login is stored in `~/.daalu/credentials.json` on your machine (readable
only by you). It's an ordinary API token: you can see it listed under **Settings
→ API tokens** in the web app, named after your machine, and **revoke** it there
at any time to instantly cut off that machine.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Not logged in` | Run `daalu login`. |
| `session expired` while chatting | Your token was revoked or expired — `daalu login` again. |
| `coding is not enabled for your tenant` | Your tenant has no inference tier for coding — same requirement as the browser Workspace (see chapter 26). Ask your operator. |
| `daalu` not found after install | Add the install directory to your `PATH` (the installer prints it). |
| Browser didn't open on `daalu login` | Copy the printed URL manually, or use the paste-a-command option. |

For the browser-based version of the same assistant, see
[chapter 26](26-coding-assistant.md).
