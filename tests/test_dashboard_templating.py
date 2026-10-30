"""Unit tests for Grafana template-variable expansion in dashboard panels.

Daalu renders Grafana panels natively with no variable picker, so
``expand_template_vars`` rewrites ``$var``/``$__macro`` into runnable PromQL
that returns the all-values aggregate. The critical case is exact-equality
matchers (``label="$var"``) used by the kube-mixin Compute Resources
dashboards: a naive ``$var`` → ``.*`` substitution would produce
``label=".*"`` (an exact match for the literal string ``.*``, matching nothing).
These tests lock in that exact matchers get promoted to regex matchers first.
"""

from __future__ import annotations

from daalu_automation.core.tenant_metrics import expand_template_vars


def test_exact_matcher_promoted_to_regex():
    # The bug that blanked every Compute Resources panel: `label="$var"` must
    # become `label=~".*"` (matches all series + absent labels), not `label=".*"`.
    expr = 'sum(container_cpu{namespace="$namespace", pod="$pod"}) by (container)'
    out = expand_template_vars(expr)
    assert 'namespace=~".*"' in out
    assert 'pod=~".*"' in out
    assert 'namespace=".*"' not in out
    assert 'pod=".*"' not in out


def test_absent_cluster_label_matched_by_wildcard_regex():
    # Single-node k3s has no `cluster` label; `cluster=~".*"` matches absent
    # labels whereas the old exact `cluster=".*"` never could.
    out = expand_template_vars('up{cluster="$cluster"}')
    assert out == 'up{cluster=~".*"}'


def test_existing_regex_matcher_left_as_regex():
    # `pod=~"$pod"` is already a regex matcher — only the value is wildcarded,
    # the operator must not gain a second `~`.
    out = expand_template_vars('rate(net{pod=~"$pod"}[$__rate_interval])')
    assert 'pod=~".*"' in out
    assert "=~~" not in out


def test_not_equal_matcher_promoted_and_empty_literal_untouched():
    # `!=` with a var → `!~`; the literal `container!=""` has no `$` and stays.
    out = expand_template_vars('x{job!="$job", container!=""}')
    assert 'job!~".*"' in out
    assert 'container!=""' in out


def test_interval_and_range_macros():
    out = expand_template_vars("rate(m[$__rate_interval]) and m[$__range]")
    assert "[5m]" in out
    assert "[1h]" in out


def test_mixed_literal_and_var_in_value():
    # A value mixing a literal and a var (`$node:9100`) still becomes a regex.
    out = expand_template_vars('node_load{instance="$node:9100"}')
    assert 'instance=~".*:9100"' in out


def test_datasource_selector_dropped():
    out = expand_template_vars('count(up{datasource="$datasource"})')
    # `"$datasource"` is emptied before wildcarding — no stray `.*` for it.
    assert "$datasource" not in out


def test_no_dollar_is_noop():
    expr = 'sum(rate(http_requests_total{job="api"}[5m]))'
    assert expand_template_vars(expr) == expr
