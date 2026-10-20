"""Τα αρχεία δεδομένων της εφαρμογής και η λήψη τους στον server.

Τοπικά τα αρχεία φτιάχνονται με `python scripts/prepare_data.py`.
Στο Hugging Face Space δεν υπάρχουν στο image (το image ενός δημόσιου Space μπορεί να το κατεβάσει
οποιοσδήποτε), οπότε κατεβαίνουν κατά την εκκίνηση από ιδιωτικό dataset repo, με token που δίνεται
ως secret (μεταβλητή περιβάλλοντος) και δεν αποθηκεύεται πουθενά.
"""

import os
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
FILES = ["news.duckdb", "texts.parquet"]


def ensure_data() -> None:
    """Αν λείπουν τα αρχεία, τα κατεβάζει από το DATA_REPO (απαιτεί HF_TOKEN με δικαίωμα ανάγνωσης)."""
    missing = [f for f in FILES if not (DATA / f).exists()]
    if not missing:
        return
    repo, token = os.environ.get("DATA_REPO"), os.environ.get("HF_TOKEN")
    if not repo or not token:
        raise RuntimeError(
            f"Λείπουν τα {', '.join(missing)} από το {DATA}. Τοπικά τρέξτε `python scripts/prepare_data.py`· "
            "στον server ορίστε DATA_REPO και HF_TOKEN.")

    from huggingface_hub import hf_hub_download

    DATA.mkdir(exist_ok=True)
    for name in missing:
        print(f"Λήψη {name} από {repo}…", flush=True)
        hf_hub_download(repo_id=repo, filename=name, repo_type="dataset", local_dir=DATA, token=token)
