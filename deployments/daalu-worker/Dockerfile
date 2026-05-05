# syntax=docker/dockerfile:1
#
# daalu-worker — long-running Temporal worker that exposes the daalu CLI as
# orchestrated workflows. Image ships:
#   - Python 3.11 + the daalu package (with all its deps)
#   - kubectl, helm, openssh-client, sshpass — needed because activities
#     shell out to `daalu deploy` which itself uses these tools
#   - the `daalu-worker` and `daalu-registry` console entry points
#
# At runtime, mount:
#   /workspace                  ← the daalu repo (cluster-defs, helm-charts, etc.)
#   /root/.kube/daalu-mgmt-config ← mgmt cluster kubeconfig
#   /root/.ssh/                 ← ssh keys for SSH into bare-metal nodes
#
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# System deps the daalu CLI shells out to.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg lsb-release \
        openssh-client sshpass git jq \
        procps iproute2 \
    && rm -rf /var/lib/apt/lists/*

# kubectl
RUN curl -fsSLo /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/v1.30.0/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl

# helm
RUN curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 \
    | bash

# clusterctl (used by the cluster-api stage)
RUN curl -fsSLo /usr/local/bin/clusterctl \
      "https://github.com/kubernetes-sigs/cluster-api/releases/download/v1.12.0/clusterctl-linux-amd64" \
    && chmod +x /usr/local/bin/clusterctl

# cilium CLI (used by the tinkerbell deploy flow)
RUN curl -fsSLo /tmp/cilium.tar.gz \
      "https://github.com/cilium/cilium-cli/releases/download/v0.16.10/cilium-linux-amd64.tar.gz" \
    && tar -C /usr/local/bin -xzf /tmp/cilium.tar.gz cilium \
    && rm /tmp/cilium.tar.gz

WORKDIR /app

# Install daalu (editable so a workspace-mount can override it during dev).
COPY pyproject.toml /app/
RUN mkdir -p /app/src && touch /app/src/__placeholder__
COPY src /app/src
RUN pip install --upgrade pip \
    && pip install typer>=0.12 paramiko pydantic>=2 pydantic-settings>=2 \
        PyYAML jinja2 \
    && pip install -e '/app[worker]'

WORKDIR /workspace

# The worker reads TEMPORAL_ADDRESS, TEMPORAL_NAMESPACE, DAALU_TASK_QUEUE
# from the env (see src/daalu/temporal/settings.py).
CMD ["daalu-worker"]
