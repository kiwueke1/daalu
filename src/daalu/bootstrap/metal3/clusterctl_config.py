# src/daalu/bootstrap/metal3/clusterctl_config.py
from __future__ import annotations

from pathlib import Path


BEGIN = "# --- DAALU: CLUSTERCTL VARS BEGIN ---"
END = "# --- DAALU: CLUSTERCTL VARS END ---"


def upsert_clusterctl_vars_block(clusterctl_yaml_path: Path, block_text: str) -> None:
    if not clusterctl_yaml_path.exists():
        clusterctl_yaml_path.write_text("", encoding="utf-8")
    
    current = clusterctl_yaml_path.read_text(encoding="utf-8")

    new_block = f"{BEGIN}\n{block_text.rstrip()}\n{END}\n"

    if BEGIN in current and END in current:
        pre = current.split(BEGIN, 1)[0]
        post = current.split(END, 1)[1]
        updated = pre + new_block + post.lstrip("\n")
    else:
        sep = "" if current.endswith("\n") or current == "" else "\n"
        updated = current + sep + new_block
    if updated != current:
        clusterctl_yaml_path.write_text(updated, encoding="utf-8")