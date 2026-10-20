"""Φόρτωση του αρχείου .env (τοπικά). Πρέπει να γίνεται πριν διαβαστούν ρυθμίσεις από το περιβάλλον.

Μορφή: ΟΝΟΜΑ=τιμή ανά γραμμή· γραμμές με # αγνοούνται. Τιμές που υπάρχουν ήδη στο περιβάλλον (π.χ. secrets του
Hugging Face) δεν αλλάζουν. Το .env δεν ανεβαίνει ποτέ στο git (.gitignore).
"""

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_env(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)

