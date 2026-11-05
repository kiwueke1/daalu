"""``python -m daalu_automation …`` = the server CLI (``daalu`` from this package).

Useful when the laptop coding CLI (daalu-cli) is also installed and owns the
``daalu`` name on PATH.
"""

from daalu_automation.cli import app

app(prog_name="daalu")
