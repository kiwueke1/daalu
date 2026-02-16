# Copyright 2026 Kezie Iwueke
# SPDX-License-Identifier: Apache-2.0

#src/daalu/logging/log.py

from __future__ import annotations

import logging
from pathlib import Path
from datetime import datetime
import uuid

def init_logging(
    *,
    base_dir: Path | None = None,
    name: str = "daalu",
    verbose: bool = False,
) -> tuple[logging.Logger, str, Path]:
    """
    Initializes:
      - human readable log file (set -x style)
      - returns run_id so observers can reuse it
    """
    run_id = str(uuid.uuid4())

    if base_dir is None:
        base_dir = Path.home() / ".daalu"/ "logs"
    base_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    log_path = base_dir / f"{name}-{ts}-{run_id}.log"

    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File = FULL TRACE
    fh = logging.FileHandler(log_path)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)

    # Console = INFO by default, DEBUG when --debug is passed
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)

    logger.info("=== Daalu run started ===")
    logger.info(f"run_id={run_id}")
    logger.info(f"log_file={log_path}")

    return logger, run_id, log_path