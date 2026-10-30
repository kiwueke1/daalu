# GpuUnderUtilised

> GPU averaged <5% over 12h.

Informational. Not paged. Two possibilities:

1. **Traffic is low.** Normal off-hours pattern — verify with the
   "Daalu — Overview" dashboard's request-rate panel.
2. **Routing is bypassing local.** Check
   `sum by (served_by) (rate(daalu_llm_requests_total[1h]))` — if
   the `local` count is near zero but classifier traffic isn't, the
   routing decisions are skipping local. Likely cause:
   `LLM_LOCAL_BASE_URL` got unset, or every tenant happens to be on
   the External-Only SKU.

If (2), no action needed beyond noting it. The architecture is doing
exactly what you asked it to.
