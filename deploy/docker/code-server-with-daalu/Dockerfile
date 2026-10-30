# Custom code-server image with the Daalu Assistant extension
# pre-installed. Used by the workspace-controller as the image for
# every per-tenant workspace pod.
#
# Build (from repo root):
#   docker build -f deploy/docker/code-server-with-daalu/Dockerfile \
#     --build-arg CODE_SERVER_VERSION=4.121.0 \
#     -t ghcr.io/kiwueke1/code-server-with-daalu:latest .
#
# CI: .github/workflows/build-code-server-image.yml.

# Global ARGs declared *before* the first FROM so all stages can
# reference them. (BuildKit scopes ARGs per-stage otherwise.)
ARG CODE_SERVER_VERSION=4.121.0

# ─── Builder stage: compile the extension and package it ────────────
FROM node:20-alpine AS extbuilder
WORKDIR /ext
COPY extensions/daalu-assistant/package.json extensions/daalu-assistant/tsconfig.json ./
RUN npm install --no-audit --no-fund
COPY extensions/daalu-assistant/src ./src
COPY extensions/daalu-assistant/webview ./webview
COPY extensions/daalu-assistant/resources ./resources
COPY extensions/daalu-assistant/README.md ./README.md
RUN npm run build && npm run package
# /ext/daalu-assistant.vsix is the artefact.

# ─── Runtime stage: code-server with the extension pre-installed ────
# Re-declare the global ARG inside this stage so the FROM line below
# can substitute it. (Per Docker docs: global ARGs are visible to
# FROM lines but not to subsequent instructions until redeclared.)
ARG CODE_SERVER_VERSION
FROM codercom/code-server:${CODE_SERVER_VERSION}

USER root

# Install OS tooling the assistant + the workspace user need.
# Tightly scoped — keep this image small.
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates git jq python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Install kubectl, helm — the assistant doesn't apply changes
# directly (everything goes through change-proposals), but having
# these in the terminal is core to the operator UX.
RUN curl -fsSL -o /usr/local/bin/kubectl \
      https://storage.googleapis.com/kubernetes-release/release/v1.30.0/bin/linux/amd64/kubectl \
    && chmod +x /usr/local/bin/kubectl
RUN curl -fsSL https://get.helm.sh/helm-v3.15.0-linux-amd64.tar.gz \
      | tar xz -C /tmp \
    && mv /tmp/linux-amd64/helm /usr/local/bin/helm \
    && rm -rf /tmp/linux-amd64

# Remove the bundled GitHub Copilot Chat built-in. Code 1.121.0 ships it
# as the `copilot` system extension; it renders the "Build with Agent"
# CHAT panel + "Sign In" flow, which is upstream/unrelated to our stack
# and would lure users away from the Daalu Assistant (our own activity-bar
# view, which routes to the local-GPU coder vLLM). With no chat provider
# registered the core Chat view disappears entirely. The `test -d` makes
# the build fail loudly if a future base image moves the extensions dir,
# rather than silently no-op'ing the suppression.
RUN set -eux; \
    ext_dir=/usr/lib/code-server/lib/vscode/extensions; \
    test -d "$ext_dir"; \
    rm -rf "$ext_dir/copilot"; \
    test ! -e "$ext_dir/copilot"

# Install the daalu CLI so the assistant can open change proposals
# from inside the workspace. Same backend image as the rest of the
# platform, but only the CLI bits are needed. TODO: trim to a
# single-binary distribution rather than the full backend image.
# For MVP we copy the entrypoint script and let it shell into the
# real CLI image at runtime via a docker socket — left as a follow-up.

USER coder

# Copy the built .vsix into the image and install it. code-server
# stores extensions under /home/coder/.local/share/code-server/extensions.
# We --chown to coder so the `code-server --install-extension` step
# (which runs as the coder user) can read the file. We deliberately
# don't `rm` the .vsix afterwards — it's 30 KB, harmless, and trying
# to remove a root-owned file as coder fails the build.
COPY --from=extbuilder --chown=coder:coder /ext/daalu-assistant.vsix /tmp/daalu-assistant.vsix
RUN code-server --install-extension /tmp/daalu-assistant.vsix

# Drop a settings.json so the extension picks up the gateway URL
# from the env we inject in workspace_controller/k8s.py. The env
# values win over this file, but the fallback ensures the extension
# works in dev (e.g. running code-server outside the pod).
#
# Also disable the upstream chat/AI surface as a belt-and-suspenders to
# removing the copilot built-in above: `chat.disableAIFeatures` is the
# officially-supported kill switch (hides the Chat view + agent UI) and
# `chat.commandCenter.enabled:false` drops the title-bar chat button.
# Together they ensure the Daalu Assistant is the only AI surface.
RUN mkdir -p /home/coder/.local/share/code-server/User \
    && printf '%s\n' '{' \
       '  "daalu.tokenFile": "/var/run/daalu/tokens/service.jwt",' \
       '  "chat.disableAIFeatures": true,' \
       '  "chat.commandCenter.enabled": false' \
       '}' \
       > /home/coder/.local/share/code-server/User/settings.json

EXPOSE 8080
ENTRYPOINT ["/usr/bin/entrypoint.sh"]
CMD ["--bind-addr", "0.0.0.0:8080", "--auth", "none", "--disable-telemetry", "/workspace"]
