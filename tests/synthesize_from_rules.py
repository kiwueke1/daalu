"""Pull all alerting rules from the demo Prometheus, relax each rule's
threshold, run the relaxed query as a range query, and emit one Daalu
event per resulting sample using the rule's real name + annotations.

Logs per-rule progress to stderr so a stuck rule is identifiable.
Uses a short HTTP timeout — bad PromQL fails fast instead of hanging."""

import json
import re
import sys
import time
from datetime import datetime, timezone

import httpx

PROM         = "https://prometheus.demo.prometheus.io"
LOOKBACK_S   = 3600      # 1 hour of history per rule
STEP_S       = 60        # one sample per minute
RELAX        = 0.05      # multiply ">" thresholds by this, divide "<" by this
MAX_PER_RULE = 30        # cap so a single rule doesn't dominate
HTTP_TIMEOUT = 8.0       # short — bad queries should fail fast

def log(msg):
    print(msg, file=sys.stderr, flush=True)

# 1. Catalogue all alerting rules.
log("Fetching rules catalogue...")
t0 = time.time()
rules_doc = httpx.get(f"{PROM}/api/v1/rules", timeout=HTTP_TIMEOUT).json()
alerting  = [r for g in rules_doc["data"]["groups"]
               for r in g["rules"]
               if r["type"] == "alerting"]
log(f"  {len(alerting)} alerting rules found in {time.time()-t0:.1f}s")

end   = int(datetime.now(tz=timezone.utc).timestamp())
start = end - LOOKBACK_S

# 2. Best-effort threshold relaxation.
#    X > N   →   X > (N * RELAX)        easier to breach
#    X < N   →   X < (N / RELAX)        easier to breach
#    Handles the first numeric comparison; rules without one are skipped.
def relax(query: str):
    def _gt(m):
        return f"{m.group(1)} {float(m.group(2)) * RELAX:.6g}"

    def _lt(m):
        return f"{m.group(1)} {float(m.group(2)) / RELAX:.6g}"

    q = re.sub(r"(>=?)\s*([\d.]+(?:e[-+]?\d+)?)", _gt, query, count=1)
    if q != query:
        return q
    q = re.sub(r"(<=?)\s*([\d.]+(?:e[-+]?\d+)?)", _lt, query, count=1)
    return q if q != query else None

total_events = 0
with httpx.Client(timeout=HTTP_TIMEOUT) as client:
    for i, rule in enumerate(alerting, 1):
        relaxed = relax(rule["query"])
        if relaxed is None:
            log(f"[{i:>2}/{len(alerting)}] {rule['name']:<40} SKIP (no threshold)")
            continue

        t0 = time.time()
        try:
            r = client.get(
                f"{PROM}/api/v1/query_range",
                params={"query": relaxed, "start": start,
                        "end": end, "step": STEP_S},
            )
            elapsed = time.time() - t0
            if r.status_code != 200:
                log(f"[{i:>2}/{len(alerting)}] {rule['name']:<40} HTTP {r.status_code} in {elapsed:.1f}s")
                continue
            result = r.json().get("data", {}).get("result", [])
        except httpx.HTTPError as e:
            log(f"[{i:>2}/{len(alerting)}] {rule['name']:<40} ERR {type(e).__name__} after {time.time()-t0:.1f}s")
            continue

        emitted = 0
        for series in result:
            if emitted >= MAX_PER_RULE:
                break
            labels = series["metric"]
            for ts, val in series["values"]:
                if emitted >= MAX_PER_RULE:
                    break
                try:
                    v = float(val)
                except (ValueError, TypeError):
                    continue

                occurred_at = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                ann = rule.get("annotations", {}) or {}
                sev = (rule.get("labels", {}) or {}).get("severity", "warning")

                print(json.dumps({
                    "type":     "infra.alert.fired",
                    "module":   "infra",
                    "source":   "prometheus-demo",
                    "severity": sev,
                    "summary":  ann.get("summary") or rule["name"],
                    "occurred_at": occurred_at.isoformat(),
                    "payload": {
                        "alert_name":  rule["name"],
                        "service":     labels.get("job") or labels.get("instance"),
                        "cluster":     "demo.prometheus.io",
                        "value":       v,
                        "labels":      labels,
                        "annotations": ann,
                        "_synthetic_relax_factor": RELAX,
                    },
                }))
                emitted += 1
        total_events += emitted
        log(f"[{i:>2}/{len(alerting)}] {rule['name']:<40} OK in {elapsed:.1f}s, {emitted} events (running total: {total_events})")

log(f"\nDone. {total_events} total events emitted.")
