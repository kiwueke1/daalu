# daalu — Daalu coding agent for your terminal

Chat with Daalu's sovereign coding model from any project on your machine, in
the terminal of your IDE. Like Claude Code / GitHub CLI, but pointed at your
operator's private inference — your prompts and code never touch a public LLM.

## Install

```sh
# Linux / macOS
curl -fsSL https://get.daalu.io/install.sh | sh

# macOS / Linuxbrew
brew install daalu-io/tap/daalu

# anywhere with Python 3.9+
pipx install daalu-cli
```

Windows (PowerShell):

```powershell
irm https://get.daalu.io/install.ps1 | iex
```

## Log in

```sh
daalu login                  # opens your browser to approve, then you're in
daalu login --host https://ops.your-company.com   # a specific instance
daalu login --code dvc_live_…                      # express: paste the command
```

Your operator's workspace page can hand you a ready-made `daalu login --code …`
command — paste it and you're authenticated, no browser step.

## Use it

```sh
cd ~/src/my-project
daalu                        # start the agent in this directory
```

Inside the REPL the agent can **read** files, propose **writes** (shown as a
diff you approve), and **run** commands (it asks first). Type `/help` for
commands, `/quit` to exit.

```sh
daalu status                 # who am I, which host + model
daalu models                 # list available coding models
daalu logout                 # remove stored credentials
```

## Where credentials live

`~/.daalu/credentials.json` (mode 0600). For CI, set `DAALU_HOST` and
`DAALU_TOKEN` (a `dpat_` personal-access-token) instead of running `daalu login`.

## How it talks to Daalu

The CLI calls the public coding gateway on your operator instance
(`POST /api/v1/coding/v1/chat/completions`) with your personal-access-token.
The backend enforces `purpose=coding`, so the request routes only to sovereign
/ Daalu-hosted inference — never an external provider. See the engineering
handbook, "The Daalu CLI", for the full architecture.
