# 61. End-to-end verification

This chapter explains how we think about end-to-end testing
of a deployed system: the post-deploy checklist tells you
*what* to verify, this chapter tells you *why*.

---

## What e2e covers that integration tests don't

Integration tests prove the code is correct in isolation.
They run against an in-process app with mocked externals.

E2E tests prove the *deployed system* works end-to-end:

- Real ingress (Traefik) terminates TLS.
- Real DNS resolves.
- Real cert-manager has issued a cert.
- Real Postgres has the right schema and is reachable.
- Real federation tunnel is up.
- Real Prometheus is scraping.
- Real Loki is receiving.

These aren't things you can mock; they're properties of the
deployment.

---

## The checklist's structure

A post-deploy checklist works best organized by subsystem,
section by section. Each section has:

- **Where to click** in the UI.
- **What you should see** as a pass/fail signal.
- **Behind the scenes** — what API / DB / metric backs the
  panel.
- **Troubleshoot** — the most likely root cause if it's red.

When something breaks, the checklist is the doc to open first.

---

## The smoke script

The golden path fits in a single-command smoke script that
verifies six things:

1. Tunnel handshake to the workload cluster is fresh.
2. vLLM responds via the tunnel.
3. A classifier call routes locally.
4. The cluster row shows connected in Daalu.
5. Prometheus targets are all UP.
6. Loki is receiving recent logs.

If all six lines print, the platform is functioning end-to-
end. We run it after every deploy and on a schedule.

---

## When to run e2e

- **After every deploy of daalu-automation.** Manual now;
  on the roadmap to wire as a CI gate after each helm
  upgrade.
- **After every infrastructure change.** A monitoring chart
  update, a new ServiceMonitor, a CNI upgrade.
- **On a schedule.** A nightly e2e run catches drift no
  one would otherwise notice.

---

## The hosted demo lab (`demo/`)

`demo/` provides a one-command, disposable end-to-end environment
against a running hub (e.g. `ops.example.com`): `demo/up.sh`
stands up a **kind** cluster with monitoring + a dummy app, onboards it to
the hub (`POST /clusters` → daalu-edge helm install over WireGuard), waits
for the tunnel, and registers the `prometheus`/`loki`/`kubernetes`
integrations **bound to the cluster's tunnel** (in-cluster Service URLs via
the edge HTTP proxy; kubeconfig via the apiserver-proxy at
`<tunnelIp>:6443`). Auth is a `dpat_` PAT. `demo/down.sh` tears it down and
detaches it from the hub; `demo/break.sh` injects a break-fix scenario for
demos. `demo/README.md` documents the topology.

Beyond demos, this is the closest thing we have to an automated
**federation-join e2e**: it exercises invite → edge bootstrap → handshake →
tunnel-bound integrations against the real hub. One config caveat baked into
`demo/up.sh`: it registers the **Prometheus server**
(`prometheus-operated:9090`), not Alertmanager — the server URL backs both
the metric console and alert ingest (chapter 78).

---

## What we don't currently test e2e

To be honest:

- **Federation join flow.** Covered semi-automatically by the demo lab
  above; a fresh *customer-shaped* cluster (non-kind, with the customer's
  CNI quirks) is still a manual exercise.
- **GPU node setup.** Same — manual on demand.
- **Multi-tenant simultaneous activity.** We have integration
  tests for isolation but no load test.
- **Failure injection.** No chaos engineering. On the
  roadmap.

These are gaps. We treat them with care during deploys (run
the smoke; watch for surprises).

---

## How to add an e2e check

If you add a new feature that should be verified post-deploy:

1. Add a section to the post-deploy checklist with
   the four sub-sections (where to click, what you should see,
   behind the scenes, troubleshoot).
2. Add a line to the smoke script if it's part of the
   golden path.
3. Add a Prometheus alert that fires if the metric the check
   relies on disappears (so you know if the check itself
   broke).

---

## What's next

Part XII covers contributing — how to add new domains,
integrations, agents, and how to cut a release.
