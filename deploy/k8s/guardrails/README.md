# NeMo Guardrails — Daalu policy layer

A small set of cross-cutting policies enforced **between the caller
and the LLM**. Lives as a ConfigMap; mounted into a Guardrails sidecar
that sits in front of the classifier tier.

Three policy categories:

* **destructive_actions** — refuse anything that asks the agent to
  delete tickets, drop tables, push to prod, or otherwise take
  irreversible action without explicit human approval.
* **pii_in_briefings** — redact email/phone/SSN-shaped strings from
  briefing output. Inference inputs are untouched; outputs that go
  into Postgres or Slack are scrubbed.
* **internal_model_details** — refuse to discuss the inference stack
  ("are you Llama? which GPU is this running on?"). Reduces social-
  engineering attack surface on the model itself.

## Files

```
deploy/k8s/guardrails/
├── README.md                      — you are here
├── config.yaml                    — top-level Guardrails config
├── policies.co                    — Colang flows (the actual rules)
└── deployment.yaml                — Guardrails sidecar Deployment
```

## Apply

```bash
kubectl -n daalu create configmap guardrails-config \
  --from-file=config.yaml --from-file=policies.co \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f deployment.yaml
```

## Wiring from the app

The Guardrails service exposes an OpenAI-compatible endpoint that
proxies to the local NIM. To turn it on:

```env
LLM_LOCAL_BASE_URL=http://llm-guardrails.daalu.svc.cluster.local/v1
```

`core/llm.py` doesn't need any changes — Guardrails is transparent on
the wire. You can flip between "raw NIM" and "Guardrails-wrapped NIM"
by changing the env var.
