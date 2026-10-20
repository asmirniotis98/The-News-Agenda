"""Δημοσίευση στο Hugging Face: δεδομένα σε ιδιωτικό dataset repo, εφαρμογή σε Docker Space.

Προϋπόθεση: `hf auth login` με token που έχει δικαίωμα εγγραφής (write) — μόνο στον δικό σας υπολογιστή.

    python scripts/deploy_space.py data  --user <hf-user>              # ανεβάζει news.duckdb, texts.parquet
    python scripts/deploy_space.py app   --user <hf-user>              # ανεβάζει τον κώδικα στο Space
    python scripts/deploy_space.py app   --user <hf-user> --public     # ... και το κάνει δημόσιο (--private: το αντίθετο)

Το Space χρειάζεται επιπλέον το secret HF_TOKEN: ένα *ξεχωριστό* fine-grained token μόνο με δικαίωμα
ανάγνωσης στο dataset repo. Ορίζεται από τη σελίδα του Space (Settings → Variables and secrets),
όχι από αυτό το script, ώστε το token να μη περνά από αρχεία ή ιστορικό εντολών.
"""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.data_files import DATA, FILES  # noqa: E402

# Μόνο αυτά πάνε στο Space — ποτέ data/, .env, notebooks κ.λπ.
APP_FILES = ["Dockerfile", "requirements.txt", "server.py"]
APP_DIRS = ["src", "web"]


def deploy_data(api: HfApi, repo: str) -> None:
    api.create_repo(repo, repo_type="dataset", private=True, exist_ok=True)
    if not api.repo_info(repo, repo_type="dataset").private:
        sys.exit(f"Το {repo} είναι δημόσιο — σταματώ. Κάντε το ιδιωτικό από τις ρυθμίσεις του.")
    for name in FILES:
        path = DATA / name
        if not path.exists():
            sys.exit(f"Λείπει το {path}. Τρέξτε πρώτα `python scripts/prepare_data.py`.")
        print(f"Ανέβασμα {name} ({path.stat().st_size / 1e6:,.0f} MB) → {repo}…")
        api.upload_file(path_or_fileobj=str(path), path_in_repo=name, repo_id=repo, repo_type="dataset",
                        commit_message=f"Update {name}")
    print(f"✓ Δεδομένα στο ιδιωτικό https://huggingface.co/datasets/{repo}")


def deploy_app(api: HfApi, space: str, data_repo: str, public: bool | None) -> None:
    # Νέο Space: ιδιωτικό, εκτός αν ζητηθεί --public. Υπάρχον Space: η ορατότητα αλλάζει μόνο με --public/--private.
    api.create_repo(space, repo_type="space", space_sdk="docker", private=not public, exist_ok=True)
    if public is not None:
        api.update_repo_settings(space, repo_type="space", private=not public)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        for f in APP_FILES:
            shutil.copy2(ROOT / f, stage / f)
        for d in APP_DIRS:
            shutil.copytree(ROOT / d, stage / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(ROOT / "space" / "README.md", stage / "README.md")
        api.upload_folder(folder_path=stage, repo_id=space, repo_type="space", commit_message="Deploy app",
                          delete_patterns=["src/**", "web/**"])
    api.add_space_variable(space, "DATA_REPO", data_repo)

    private = api.repo_info(space, repo_type="space").private
    print(f"✓ Κώδικας στο https://huggingface.co/spaces/{space} ({'ιδιωτικό' if private else 'ΔΗΜΟΣΙΟ'})")
    print("\nΑν δεν το έχετε κάνει ήδη: στη σελίδα του Space, Settings → Variables and secrets → New secret\n"
          f"  HF_TOKEN = ένα fine-grained token με ΜΟΝΟ «Read» στο dataset {data_repo}.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("what", choices=["data", "app"])
    p.add_argument("--user", required=True, help="όνομα χρήστη στο Hugging Face")
    p.add_argument("--space", default="news-agenda")
    p.add_argument("--data-repo", default="news-agenda-data")
    vis = p.add_mutually_exclusive_group()
    vis.add_argument("--public", dest="public", action="store_true", default=None, help="κάνει το Space δημόσιο")
    vis.add_argument("--private", dest="public", action="store_false", help="κάνει το Space ιδιωτικό")
    a = p.parse_args()

    api = HfApi()
    data_repo = f"{a.user}/{a.data_repo}"
    if a.what == "data":
        deploy_data(api, data_repo)
    else:
        deploy_app(api, f"{a.user}/{a.space}", data_repo, a.public)


if __name__ == "__main__":
    main()
