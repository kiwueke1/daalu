"""The laptop CLI ships a vendored copy of the MCP stdio bridge; keep it in sync."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_vendored_bridge_matches_server_copy() -> None:
    server = (ROOT / "src/daalu_automation/core/aiops/mcp_cli.py").read_text()
    vendored = (ROOT / "cli/daalu_cli/mcp_bridge.py").read_text()
    body = "\n".join(
        line for line in vendored.splitlines(keepends=False)
        if not line.startswith("# Vendored copy") and not line.startswith("# CLI must not")
        and not line.startswith("# fails if the two drift")
    )
    assert body.strip() == server.strip()
