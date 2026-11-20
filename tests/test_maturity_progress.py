"""Per-criterion progress on the Maturity level cards."""

from daalu_automation.core.aiops.maturity import build_progress


def _m(**kw):
    base = {
        "days": 30, "alerts_total": 29, "investigations_total": 10, "actions": {"approved": 0, "rejected": 0},
        "proposal_approval_rate": None, "change_failure_rate": None, "escalation_rate": None,
        "agreement_rate": None, "runbooks_published": 0, "eval_runs": 0, "audit_chain_ok": True,
    }
    return base | kw


EX = {"human_executed": 0, "auto_executed": 0, "alerts_triaged": 8, "verdicts_total": 2}
POL = {"auto_rules": 0, "guardrails": False}


def test_triage_says_how_many_more():
    p = build_progress(_m(), EX, POL)["triage_rate"]
    assert p["text"] == "8 of 29 investigated · 7 more for 50%"
    assert round(p["ratio"], 3) == round(8 / 14.5, 3)


def test_triage_met_drops_the_more():
    p = build_progress(_m(), EX | {"alerts_triaged": 15}, POL)["triage_rate"]
    assert p["text"] == "15 of 29 investigated"
    assert p["ratio"] == 1.0


def test_no_alerts_and_counts_and_ceilings():
    p = build_progress(_m(alerts_total=0, change_failure_rate=0.1), EX | {"alerts_triaged": 0}, POL)
    assert p["triage_rate"] == {"text": "No alerts yet", "ratio": 0.0}
    assert p["alert_volume"]["ratio"] == 0.0
    assert p["human_approved_executed"] == {"text": "0 of 1 approved fix", "ratio": 0.0}
    assert p["change_failure_rate"] == {"text": "10% of fixes failed · limit 15%", "ratio": None}
    assert p["verdict_coverage"]["text"] == "2 of 10 with a verdict · 1 more for 30%"



def test_met_count_reads_naturally():
    p = build_progress(_m(), EX | {"human_executed": 5}, POL)
    assert p["human_approved_executed"] == {"text": "5 approved fixes", "ratio": 1.0}
