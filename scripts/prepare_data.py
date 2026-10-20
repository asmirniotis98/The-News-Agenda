"""Λήψη του glossAPI/amna-press και δημιουργία του ελαφριού data/articles.parquet.

Το dataset είναι gated: πρώτα αίτημα πρόσβασης στη σελίδα του στο Hugging Face
και σύνδεση με `hf auth login` (ή μεταβλητή HF_TOKEN).

Εκτέλεση:  python scripts/prepare_data.py
"""

import sys
from pathlib import Path

import duckdb
from huggingface_hub import hf_hub_download

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.normalize import normalize_sql  # noqa: E402

REPO_ID = "glossAPI/amna-press"
RAW_DIR = ROOT / "data" / "raw"
OUT = ROOT / "data" / "articles.parquet"


LOCAL_COPY = ROOT / "data" / "Huggingface data source" / "amna-press.parquet"


def download() -> Path:
    if LOCAL_COPY.exists():
        return LOCAL_COPY
    path = hf_hub_download(
        repo_id=REPO_ID,
        filename="amna-press.parquet",
        repo_type="dataset",
        local_dir=RAW_DIR,
    )
    return Path(path)


def build(raw: Path) -> None:
    con = duckdb.connect()
    src = f"read_parquet('{raw.as_posix()}')"

    total = con.sql(f"SELECT count(*) FROM {src}").fetchone()[0]
    print(f"Γραμμές στο αρχικό αρχείο: {total:,}")

    # Εκτός: άρθρα-δοκιμές (τίτλοι όπως '336x280', 'b'), κενά κείμενα, διπλότυπα id.
    con.sql(f"""
        COPY (
            SELECT
                id,
                category,
                trim(title)                           AS title,
                abstract,
                created,
                CAST(date_trunc('month', created) AS DATE) AS month,
                url,
                {normalize_sql('title')}              AS title_norm,
                {normalize_sql("title || ' ' || abstract || ' ' || text_clean")} AS text_norm
            FROM {src}
            WHERE length(trim(title)) > 3
              AND NOT regexp_matches(trim(title), '^[0-9]+x[0-9]+$')
              AND length(trim(text_clean)) > 0
            QUALIFY row_number() OVER (PARTITION BY id ORDER BY modified DESC) = 1
            ORDER BY created
        ) TO '{OUT.as_posix()}' (FORMAT parquet, COMPRESSION zstd)
    """)

    kept = con.sql(f"SELECT count(*) FROM read_parquet('{OUT.as_posix()}')").fetchone()[0]
    size_mb = OUT.stat().st_size / 1e6
    print(f"Κρατήθηκαν: {kept:,} (αφαιρέθηκαν {total - kept:,})")
    print(f"Αποθηκεύτηκε: {OUT} ({size_mb:,.0f} MB)")


if __name__ == "__main__":
    raw = download()
    print(f"Λήψη: {raw}")
    build(raw)
