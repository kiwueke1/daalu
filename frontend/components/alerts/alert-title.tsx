import type { Alert } from "@/lib/api";

/**
 * Build a human-friendly headline from an alert.
 *
 * Prometheus annotations are generic ("Pod is crash looping.",
 * "Deployment has not matched the expected number of replicas.").
 * The operator wants to know *which* pod / deployment from a glance.
 *
 * Strategy: read the subject (pod / deployment name) from the alert's
 * `metadata_json.labels` and prepend a short, Title-Cased subject to
 * the original sentence. Examples:
 *
 *   "Pod is crash looping."
 *     → "Octavia pod is crash looping."  (pod=octavia-health-manager-…)
 *
 *   "Deployment has not matched the expected number of replicas."
 *     → "Argocd-Dex-Server deployment has not matched the expected …"
 *
 * Falls back to the original title when we can't derive a subject.
 */
export function enrichAlertTitle(alert: Alert): string {
  const original = alert.title.trim();
  const md = (alert.metadata_json ?? {}) as Record<string, unknown>;
  const labels = (md.labels as Record<string, string> | undefined) ?? {};

  const pod = (md.pod as string) || labels.pod;
  const deployment = (md.deployment as string) || labels.deployment;
  const namespace = (md.namespace as string) || labels.namespace;
  const container = labels.container;

  // Pick the most specific subject available, then strip off the
  // hashy ReplicaSet suffix that pods always carry
  // (e.g. "octavia-health-manager-default-787mv" → "octavia").
  const subjectRaw =
    container ||
    (pod ? podBaseName(pod) : undefined) ||
    deployment ||
    namespace;
  if (!subjectRaw) return original;

  const subject = humanizeSubject(subjectRaw);

  // If the original already mentions the subject, don't double up.
  if (subject && original.toLowerCase().includes(subject.toLowerCase())) {
    return original;
  }

  // A rule name ("LLMServerCrashLooping") isn't a sentence: prefixing and
  // lowercasing it gives "Vllm lLMServerCrashLooping". Keep it whole and
  // name the subject after it.
  if (!/\s/.test(original)) {
    return `${original} · ${subjectRaw}`;
  }

  // Lowercase the first letter of the original so it reads as a clause
  // after the subject ("Octavia pod is …", not "Octavia Pod is …").
  const tail = original.charAt(0).toLowerCase() + original.slice(1);
  return `${subject} ${tail}`;
}

function podBaseName(pod: string): string {
  // ReplicaSet pods end with -<hash>-<id>; DaemonSet pods end with -<id>;
  // bare pods just have the name. Strip the trailing -<short> chunks.
  const parts = pod.split("-");
  while (parts.length > 1) {
    const last = parts[parts.length - 1];
    if (/^[a-z0-9]{4,10}$/i.test(last)) {
      parts.pop();
      continue;
    }
    break;
  }
  return parts.join("-");
}

function humanizeSubject(s: string): string {
  // Take the first hyphenated segment as the "brand"; "Octavia",
  // "Argocd-Dex-Server", etc. read as proper-noun subjects.
  const first = s.split("-").filter(Boolean)[0] ?? s;
  return first.charAt(0).toUpperCase() + first.slice(1);
}
