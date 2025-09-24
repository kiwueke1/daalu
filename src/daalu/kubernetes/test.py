#import ansible_runner
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
ANSIBLE_DIR = BASE_DIR / "atmosphere" / "playbooks"

print(BASE_DIR)
print(ANSIBLE_DIR)

