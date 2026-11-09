# Fault injection: break the lab on purpose, then put it back

Every episode that shows Daalu finding or fixing something needs a real
incident, and the same incident again for the next take. `daalu fault` gives
you that: a catalog of faults for Kubernetes workloads, nodes, an OpenStack on
Kubernetes install and a network fabric. Every fault can be planned before it
runs, is recorded in a local journal, and reverts to the known-good state with
one command.

```bash
daalu fault                                        # guided: environment → fault → target → confirm
daalu fault inject openstack/mariadb-down --env openstack-lab --ttl 30m --watch
daalu fault revert f-1a2b3c
```

The same tool works on any cluster or fabric you list in its inventory, with a
pinned API server so it can't wander into production.

---

## Why it isn't a button in the Daalu UI

It would be easy to add a "Break it" button to the cluster page. It would also
be a mistake:

1. **Daalu holds write access to every connected cluster.** A fault-injection
   API inside the product is a "damage any customer cluster" endpoint. Any
   tenant admin, any leaked token, and the AI agent itself (through its tools or
   MCP) could reach it. A prompt injection in a log line should never be one tool
   call away from a fault catalog.
2. **It would spoil the test.** If the platform that injects the fault is the
   platform being graded, the AI can learn the answer from it (a database row, an
   audit event, a "what changed" entry written by Daalu itself).
3. **It would spoil the metrics.** Incidents that Daalu caused and Daalu then
   resolved would flatter escalation rate and time to resolve. That's the
   opposite of what the AI Ops metrics are for.

So `daalu fault` lives in the server CLI next to `daalu eval`. It runs on your
machine, with **your** kubeconfig, SSH key or docker socket, and it never calls
the Daalu API. To Daalu, an injected fault looks like what it imitates: someone
made a bad change, or something broke.

---

## Install

`daalu fault` ships with this repo's server package:

```bash
pip install -e .                    # from the repo root
daalu fault --help
python -m daalu_automation fault --help   # same thing, if the laptop coding CLI owns `daalu` on your PATH
```

It needs `kubectl` for clusters, `docker` (and `containerlab`, `sudo` for cable
swaps) for containerlab fabrics, and `ssh` for real devices.

---

## 1. Tell it which environments it may touch

Nothing happens without an **inventory** (`~/.daalu/faults/inventory.yaml`).
Let `init` discover an environment for you:

```bash
# A cluster: pins its API server, lists workloads, Services and nodes as targets
daalu fault init cluster openstack-lab --kubeconfig ~/.kube/openstack-lab \
  -n openstack -n kube-system

# A containerlab fabric: devices from the running containers, targets from the topology's links
daalu fault init clab spectrum-x --lab daalu-spectrum-x
```

A cluster entry looks like this (full example with SSH fabrics:
[`faults/inventory.example.yaml`](../faults/inventory.example.yaml)):

```yaml
environments:
  openstack-lab:
    kind: cluster
    kubeconfig: ~/.kube/openstack-lab
    server: https://192.0.2.50:6443      # the pin: refuse if the context points anywhere else
    node_access: helper-pod              # or ssh
    protect_namespaces: [daalu-edge, daalu-system]
    targets:
      keystone-api:   {kind: deployment,  name: keystone-api,   namespace: openstack, container: keystone-api}
      mariadb-server: {kind: statefulset, name: mariadb-server, namespace: openstack, container: mariadb}
      node-lab01:     {node: lab01}
```

**Targets** are named shortcuts for a fault's parameters. They make it agnostic:
the catalog says "scale `${kind}/${name}` to zero" and your inventory says what
`mariadb-server` is on *this* cluster. Pointing the tool at another cluster
means another inventory entry, not another fault.

---

## 2. Pick, plan, break, watch, revert

```bash
daalu fault list --env openstack-lab             # recipes and faults that fit this environment
daalu fault show openstack/rabbitmq-down         # params, steps, answer key
daalu fault plan openstack/rabbitmq-down --env openstack-lab   # exactly what will run; runs nothing
daalu fault inject openstack/rabbitmq-down --env openstack-lab --ttl 30m
daalu fault watch f-1a2b3c                       # polls the health check; prints time to recover
daalu fault status                               # every active fault, broken or healthy again
daalu fault revert f-1a2b3c                      # or: revert --all
```

- **Recipes** (`openstack/…`, `fabric/…`, `lab-llm/…`, `demo/…`) are
  ready-made incidents: a fault plus a target plus a story and an answer key.
  Episodes use recipes.
- **Faults** (`kubernetes/…`, `node/…`, `network/…`) are the generic building
  blocks. Give them a target and/or `--set key=value`:
  `daalu fault inject kubernetes/oom-limit --env openstack-lab --target nova-api-osapi --set limit=32Mi`.
- Run `daalu fault` with no arguments for the guided version. It asks for the
  environment, then the fault (recipes first), then a target, shows the plan,
  and asks before it touches anything.

**Recording an episode** is the same five steps every time:

1. `daalu fault status`: nothing active (a leftover fault ruins the take).
2. `daalu fault inject <recipe> --env <env> --ttl 30m`, on camera or just before.
3. Hands off. Let the alert fire and Daalu investigate; approve (or not) in the UI.
4. `daalu fault watch <id>` in a side terminal shows the moment it recovers, and
   how long that took, measured by the tool rather than by Daalu.
5. `daalu fault revert <id>`. This is safe even after Daalu fixed it: revert
   restores the known-good spec, so it's a no-op if the fix already did that.

`--ttl` is a deadline. `watch` reverts when it's reached, and `daalu fault reap`
reverts every expired fault, so a cron line (`*/5 * * * * daalu fault reap`)
guarantees nothing stays broken overnight by accident.

---

## 3. Game days: many incidents in a row

The metrics episodes need many real incidents, not one. A **drill** runs a list
of recipes back to back. Each one is injected, left for the responder (Daalu,
with whatever autonomy policy you're measuring) up to a timeout, then reverted,
followed by a quiet gap:

```bash
daalu fault drill openstack/gameday --env openstack-lab --dry-run
daalu fault drill openstack/gameday --env openstack-lab
```

It ends with a table of time to recover per incident, saved as JSON under
`~/.daalu/faults/drills/`. Put that next to Daalu's **Insights → Maturity** for the
same window. The drill's numbers are ground truth that Daalu didn't produce, so
they're how you check the dashboard (S3-09) and how you compare models fairly
(S1-11, S4-04).

| Drill | What it is | Incidents | Wait per incident / gap |
|---|---|---|---|
| `fabric/gameday` | Fabric game day (six network incidents) | 6 | 15m / 2m |
| `openstack/gameday` | OpenStack game day (eight incidents, easy to hard) | 8 | 20m / 3m |
| `openstack/overnight` | A noisy night on the OpenStack cluster (for the S1-09 morning briefing) | 3 | 40m / 60m |

Write your own in `faults/drills/<group>/<id>.yaml`, or pass a file path.

---

## 4. Safety: what stops it from hurting the wrong thing

| Guard | What it does |
|---|---|
| Inventory only | Only environments you listed can be targeted. There is no "use my current context". |
| API server pin | Before every inject, the context's API server must equal `server:`. A context renamed or repointed at production is refused. |
| Protected namespaces | Faults can't target `daalu-edge` / `daalu-system` (configurable): breaking Daalu's own tunnel tests nothing. |
| Plan first | `plan`, and the confirmation prompt on `inject`, show every command that will run, with values filled in. |
| Typed params | Every value is validated (names, sizes, interfaces, CIDRs…), so a `--set` can't smuggle `; rm -rf` into a node shell. |
| Revert recorded first | Revert commands, with everything captured before the fault (the object's spec, the old MTU, the original cabling), are written to the journal (`~/.daalu/faults/journal.json`, mode 0600) as part of inject. `revert` needs nothing else. |
| Partial failure | If any inject step fails, everything done so far is reverted at once. |
| Blast radius | Each fault declares one (pod → workload → service → namespace → node → cluster; interface → link → device). Node-wide faults warn that Daalu's edge agent may be hit. |
| Node safety timers | `node/kubelet-stop` schedules its own restart on the node (`systemd-run`), because nothing can reach a node whose kubelet is down. It refuses single-node clusters. |
| Capped fills | `kubernetes/volume-fill` writes at most `max_mb`, because on `local-path` volumes `df` reports the node's own disk. |

Two things it deliberately doesn't do. It doesn't label or annotate what it
breaks, because that would hand the AI the answer. And it can't run arbitrary
commands: only the steps in a catalog file run, after you've seen them in the plan.

---

## 5. The catalog

37 faults, 32 recipes, 3 drills. All in [`faults/`](../faults), one YAML file each.

#### Kubernetes workloads

| Fault | Blast | What breaks | Daalu can fix? |
|---|---|---|---|
| `kubernetes/bad-env` | workload | Roll out a wrong environment variable | yes |
| `kubernetes/bad-image` | workload | Roll out an image tag that doesn't exist | yes |
| `kubernetes/configmap-corrupt` | workload | Change one ConfigMap key, then restart the workload that reads it (a rollback doesn't fix it) | yes |
| `kubernetes/cpu-throttle` | workload | CPU limit so low the app starves (slow, probes time out) | yes |
| `kubernetes/crashloop` | workload | Roll out a command that exits on start (CrashLoopBackOff) | yes |
| `kubernetes/daemonset-unschedulable` | service | DaemonSet nodeSelector matches no node (its pods disappear) | yes |
| `kubernetes/network-policy-deny` | service | A NetworkPolicy that blocks all ingress to some pods | no: escalate |
| `kubernetes/oom-limit` | workload | Memory limit too low (OOMKilled) | yes |
| `kubernetes/pod-kill` | workload | Kill every pod of a workload at once (self-heals; the right action is none) | no action needed |
| `kubernetes/readiness-probe-broken` | workload | Readiness probe pointed at a port nothing listens on | yes |
| `kubernetes/resource-quota-block` | namespace | A ResourceQuota that allows no new pods, then the workload's pods are recycled | no: escalate |
| `kubernetes/scale-to-zero` | workload | Scaled to zero replicas (a rollback is the wrong fix) | yes |
| `kubernetes/secret-corrupt` | workload | Change one Secret key, then restart the workload that reads it | yes |
| `kubernetes/service-selector-mismatch` | service | Service selector no longer matches any pod | yes |
| `kubernetes/service-wrong-port` | service | Service targetPort points at a port the app doesn't listen on | yes |
| `kubernetes/volume-fill` | workload | Fill a pod's volume until writes fail | no: escalate |

#### Nodes

| Fault | Blast | What breaks | Daalu can fix? |
|---|---|---|---|
| `node/disk-pressure` | node | Fill a node's disk until the kubelet reports DiskPressure and evicts pods | no: escalate |
| `node/drain` | node | Cordon and drain a node (forgotten maintenance) | no: escalate |
| `node/gpu-persistence-off` | node | Turn GPU persistence mode off | no: escalate |
| `node/kubelet-stop` | node | Stop the kubelet on one node (node goes NotReady) | no: escalate |
| `node/noexecute-taint` | node | Taint a node NoExecute (evicts everything that doesn't tolerate it) | no: escalate |

Node faults run as a short-lived privileged pod on that node (`nsenter` into
the host), so they need no SSH. Set `node_access: ssh` in the inventory to use SSH
instead.

#### Network fabric

| Fault | NOS | Blast | What breaks | Daalu can fix? |
|---|---|---|---|---|
| `network/bgp-neighbor-shutdown` | cumulus, sonic, frr | link | Shut one BGP session (FRR runtime, not saved) | no: escalate |
| `network/blackhole-route` | linux | device | Blackhole a prefix on one switch | no: escalate |
| `network/cable-pull` | linux | link | Pull a cable (carrier lost on both ends, config untouched) | no: escalate |
| `network/cable-swap` | linux | link | Cross two cables on a server (both links stay up, wrong neighbours) | no: escalate |
| `network/interface-shutdown` | cumulus | interface | Shut a switch port in the config (NVUE link state down) | no: escalate |
| `network/latency` | linux | link | Add latency (and jitter) on a link | no: escalate |
| `network/link-flap` | linux | link | Flap a link (down and up on a timer) | no: escalate |
| `network/mtu-mismatch` | linux | interface | Drop one interface to MTU 1500 on a jumbo-frame fabric (kernel only) | no: escalate |
| `network/nvue-mtu-change` | cumulus | interface | Commit an MTU change to one switch port's config (NVUE) | no: escalate |
| `network/packet-corruption` | linux | link | Corrupt a percentage of packets on a link | no: escalate |
| `network/packet-loss` | linux | link | Drop a percentage of packets on a link | no: escalate |
| `network/rogue-ssh-key` | linux | device | Leave an extra SSH key in a switch user's authorized_keys (drift) | yes (drift proposal, human approves) |
| `network/routing-daemon-stop` | cumulus, sonic, frr | device | Stop the routing daemon (every BGP session on the switch drops) | no: escalate |
| `network/switch-isolated` | linux | device | Isolate a switch (every front-panel port down, mgmt still up) | no: escalate |
| `network/sysctl-drift` | linux | device | Change a kernel sysctl on a switch or server by hand (drift) | yes (drift proposal, human approves) |
| `network/traffic-drop` | linux | device | Drop routed traffic to a prefix with a firewall rule (a bad ACL) | no: escalate |

`linux` means any Linux-based NOS (Cumulus, SONiC, FRR boxes, servers): those
faults use `ip`, `tc`, `iptables` and `sysctl`. Containerlab fabrics are reached
with `docker exec`, real devices with `ssh` (commands wrapped in `sudo -n`).
Mixed fabrics set `device_nos:` per device (`init clab` does it for you), so a
Cumulus-only fault is refused on a Linux server.

"Daalu can fix?" is the honest answer key. Most network faults, and every node
fault, have no Kubernetes write tool that fixes them. For those, the right
behaviour is a correct root cause and an escalation, and that is what the
episodes should show (S2-10's whole point).

#### Recipes (episode-ready)

| Recipe | What happens | Fault | Episodes |
|---|---|---|---|
| `demo/dummy-bad-image` | dummy-app: image tag that doesn't exist | `kubernetes/bad-image` | S0-02, S3-10 |
| `demo/dummy-crashloop` | dummy-app: container crashes on startup | `kubernetes/crashloop` | S0-03 |
| `demo/dummy-scaled-to-zero` | dummy-app: scaled to zero | `kubernetes/scale-to-zero` | - |
| `demo/metrics-error-mode` | metrics-app: error rate climbs, pods stay healthy | `kubernetes/bad-env` | - |
| `fabric/cable-pulled` | A cable pulled between a leaf and a GPU server | `network/cable-pull` | S2-01 |
| `fabric/crossed-cables` | gpu2's rail-2 and rail-3 cables swapped | `network/cable-swap` | S2-07 |
| `fabric/flapping-link` | A leaf-to-GPU link flapping every 20 s | `network/link-flap` | - |
| `fabric/jumbo-frames-broken` | One leaf port drops to MTU 1500 on a 9216 fabric | `network/mtu-mismatch` | S2-10 |
| `fabric/leaf-port-shutdown` | A leaf port facing a GPU server is shut in the config | `network/interface-shutdown` | S2-06 |
| `fabric/lossy-optic` | 2% packet loss on one leaf-to-GPU link | `network/packet-loss` | S2-10 |
| `fabric/mtu-change-in-config` | A committed MTU change on one leaf port: the line in the config diff | `network/nvue-mtu-change` | S2-05 |
| `fabric/rogue-ssh-key` | A contractor's SSH key left on leaf1 | `network/rogue-ssh-key` | S2-04 |
| `fabric/spine-routing-down` | FRR stopped on a spine | `network/routing-daemon-stop` | S2-09 |
| `lab-llm/coder-model-down` | The local coder model is scaled to zero (the router should fail over) | `kubernetes/scale-to-zero` | S1-13 |
| `lab-llm/gpu-memory-overcommit` | chat-llm asks for 90% of a GPU that's already shared (the S1-01 break) | `kubernetes/bad-env` | S1-01, S1-12, S3-01, S3-02, S3-03, S3-04, S3-10 |
| `lab-llm/gpu-persistence-off` | GPU persistence mode turned off on the GPU node | `node/gpu-persistence-off` | S1-16 |
| `openstack/cluster-dns-outage` | CoreDNS scaled to zero: every service-to-service call fails | `kubernetes/scale-to-zero` | - |
| `openstack/compute-node-disk-full` | The compute node's disk fills past the eviction threshold | `node/disk-pressure` | S0-04 |
| `openstack/database-disk-full` | MariaDB data volume full | `kubernetes/volume-fill` | - |
| `openstack/database-isolated` | A default-deny NetworkPolicy cuts every service off from MariaDB | `kubernetes/network-policy-deny` | S3-07 |
| `openstack/glance-never-ready` | Glance readiness probe broken: image API has no endpoints | `kubernetes/readiness-probe-broken` | S4-02 |
| `openstack/keystone-bad-rollout` | Keystone rolled out with an image that doesn't exist | `kubernetes/bad-image` | S0-01 |
| `openstack/keystone-service-orphaned` | Keystone Service selector selects nothing | `kubernetes/service-selector-mismatch` | - |
| `openstack/keystone-service-wrong-port` | Keystone Service targetPort changed | `kubernetes/service-wrong-port` | S1-14 |
| `openstack/mariadb-down` | The OpenStack database is scaled to zero | `kubernetes/scale-to-zero` | S1-02, S3-06 |
| `openstack/memcached-down` | Memcached gone: Keystone token validation crawls | `kubernetes/scale-to-zero` | S1-06 |
| `openstack/namespace-quota-zero` | A ResourceQuota blocks every new pod in the openstack namespace | `kubernetes/resource-quota-block` | - |
| `openstack/neutron-server-crashloop` | neutron-server rolled out with a command that exits | `kubernetes/crashloop` | S1-07 |
| `openstack/nova-api-oom` | nova-api memory limit cut to 64Mi (OOMKilled) | `kubernetes/oom-limit` | S4-03 |
| `openstack/nova-compute-vanished` | nova-compute DaemonSet no longer schedules on any node | `kubernetes/daemonset-unschedulable` | - |
| `openstack/ovs-agent-vanished` | Neutron OVS agent DaemonSet no longer schedules | `kubernetes/daemonset-unschedulable` | - |
| `openstack/rabbitmq-down` | RabbitMQ scaled to zero: compute and network agents go dark | `kubernetes/scale-to-zero` | S1-11 |

The OpenStack recipes use **openstack-helm** resource names (`keystone-api`,
`mariadb-server`, `rabbitmq-rabbitmq`, `nova-compute-default`, …), which are
the target names `daalu fault init cluster … -n openstack` writes. On another
deployment (Kolla-on-k8s, Atmosphere, a hand-rolled chart) run `init`, read the
target names it found, and pass `--target <name>`; the faults themselves don't
change.

---

## 6. Write your own fault

A fault is one YAML file in `faults/catalog/<group>/<id>.yaml`:

```yaml
id: oom-limit
title: Memory limit too low (OOMKilled)
scope: cluster          # cluster | fabric
blast: workload
params:
  kind: {type: enum, choices: [deployment, statefulset, daemonset], default: deployment}
  name: {type: name}
  namespace: {type: name}
  container: {type: token, default: ""}
  limit: {type: size, default: 8Mi}
prepare:                # read-only steps that fill variables
  - kubectl: [get, "${kind}/${name}", -n, "${namespace}", -o, "jsonpath={.spec.template.spec.containers[0].name}"]
    capture: container
    when_empty: container
inject:
  - snapshot: "${kind}/${name}"      # saved now, restored first on revert
  - kubectl: [set, resources, "${kind}/${name}", -n, "${namespace}", -c, "${container}", "--limits=memory=${limit}"]
check:                  # passes once it's healthy again (used by watch/status/drill)
  kubectl: [rollout, status, "${kind}/${name}", -n, "${namespace}", --timeout=5s]
expect:
  root_cause: the memory limit was lowered below what the process needs
  fix: restore the memory limit (patch) or roll back
```

Step kinds: `kubectl` (argument list), `snapshot` (Kubernetes object, restored
with `kubectl replace` on revert), `exec` (shell in the workload's pod), `node`
(shell on the node), `device` (shell on a switch/server), `host` (shell on the
containerlab host), `sleep`. Steps can `capture:` output into a variable for
later steps and for revert, run only `when_empty:`, target another node or
device with `at:`, or feed `stdin:` (for `kubectl apply -f -`). Built-in
variables: `${fault_id}`, `${env}`, `${clab_prefix}`. A fault must be
revertible (revert steps, a snapshot, or `self_heals: true`); the loader refuses
it otherwise. Run `pytest tests/test_faults.py` after adding one.

A recipe (`faults/recipes/<group>/<id>.yaml`) is `fault:`, `target:`,
`params:`, a `story:` and an `expect:` answer key. Eval scenarios
([`evals/scenarios/`](../evals/scenarios)) are the graded counterpart:
the same kind of break, with a strict answer key that `daalu eval` scores.

---

## 7. What's been verified

Tested end to end on **2026-11-05** on a throwaway two-node kind cluster running
OpenStack-shaped stand-ins (Deployment, StatefulSet with a PVC, DaemonSet,
Service, ConfigMap, Secret) and on a throwaway containerlab fabric (two Cumulus
VX 5.3 switches with BGP unnumbered, two Linux servers):

- **Kubernetes:** all 16 workload faults injected, observed broken, reverted;
  the object's spec matched byte for byte afterwards. A drill ran two incidents
  and timed a recovery made outside the tool (43 s).
- **Nodes:** `noexecute-taint`, `drain` and `kubelet-stop` (node NotReady, then
  restored by its own systemd timer) ran live. `disk-pressure` ran its helper-pod
  path with a no-op size. A real fill wasn't run, because kind nodes share this
  machine's disk.
- **Network:** all 16 fabric faults injected and reverted with every device's
  state fingerprint (links, MTUs, qdiscs, routes, iptables, authorized_keys,
  sysctl, NVUE config) identical before and after; BGP re-established. `cable-swap`
  crossed and un-crossed two server links with addresses kept.

**Not yet run:** anything on the real OpenStack lab (recipe targets are
openstack-helm names, unconfirmed against your install); `gpu-persistence-off`
(no GPU on the test host); the `ssh` transport for nodes and devices (covered by
unit tests only); `nvue-mtu-change` (its NVUE syntax was checked on a Cumulus
5.3 switch, but it wasn't applied). Rehearse each recipe once with `plan` and a
throwaway inject before its shoot day.
