# Fault catalog for `daalu fault`

Revertible faults for lab clusters, nodes, OpenStack on Kubernetes and network
fabrics, used to give Daalu real incidents to find and fix in demos, episodes,
drills and evals.

| Directory | What's in it |
|---|---|
| `catalog/<group>/` | Generic faults (`kubernetes`, `node`, `network`): params, inject, revert, check, answer key |
| `recipes/<group>/` | Episode-ready incidents: a fault + a named target + a story (`openstack`, `fabric`, `lab-llm`, `demo`) |
| `drills/<group>/` | Game days: several recipes in a row, each timed to recovery |
| `inventory.example.yaml` | The environments the tool may touch (copy to `~/.daalu/faults/inventory.yaml` or use `daalu fault init`) |

Guide (why it's a CLI and not a UI button, safety model, the full catalog, how to
write a fault): [`docs/fault-injection.md`](../docs/fault-injection.md).
Code: `src/daalu_automation/faults/`. Tests: `tests/test_faults.py`.
