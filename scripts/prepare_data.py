"""Προετοιμασία δεδομένων: καθαρισμός άρθρων και ευρετήριο λέξεων.

Βήμα 1 — data/articles.parquet: καθαρά άρθρα με κανονικοποιημένο κείμενο (για τα notebooks).
Βήμα 2 — data/news.duckdb: τα άρθρα + ευρετήριο λέξεων («ποια άρθρα περιέχουν ποια λέξη»),
         ώστε η αναζήτηση να μη διαβάζει όλα τα κείμενα σε κάθε ερώτηση·
         data/texts.parquet: τα κείμενα, για τις σπάνιες ερωτήσεις που χρειάζονται έλεγχο στο κείμενο.

Το dataset είναι gated: πρώτα αίτημα πρόσβασης στη σελίδα του στο Hugging Face
και σύνδεση με `hf auth login` (ή μεταβλητή HF_TOKEN).

Εκτέλεση:  python scripts/prepare_data.py            (παραλείπει ό,τι υπάρχει ήδη)
           python scripts/prepare_data.py --force    (ξαναφτιάχνει τα πάντα)
"""

import argparse
import sys
import time
from pathlib import Path

import duckdb
from huggingface_hub import hf_hub_download

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.normalize import WORD_SQL, normalize_sql  # noqa: E402

REPO_ID = "glossAPI/amna-press"
RAW_DIR = ROOT / "data" / "raw"
LOCAL_COPY = ROOT / "data" / "Huggingface data source" / "amna-press.parquet"
ARTICLES = ROOT / "data" / "articles.parquet"
INDEX = ROOT / "data" / "news.duckdb"
TEXTS = ROOT / "data" / "texts.parquet"
SCRATCH = ROOT / "data" / "_build.duckdb"

BATCH = 50_000          # άρθρα ανά παρτίδα κατά την εξαγωγή λέξεων
MEMORY_LIMIT = "4GB"    # ό,τι δεν χωράει γράφεται προσωρινά στον δίσκο


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


def build_articles(raw: Path) -> None:
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
        ) TO '{ARTICLES.as_posix()}' (FORMAT parquet, COMPRESSION zstd)
    """)

    kept = con.sql(f"SELECT count(*) FROM read_parquet('{ARTICLES.as_posix()}')").fetchone()[0]
    print(f"Κρατήθηκαν: {kept:,} (αφαιρέθηκαν {total - kept:,})")
    print(f"Αποθηκεύτηκε: {ARTICLES} ({ARTICLES.stat().st_size / 1e6:,.0f} MB)")


def build_index() -> None:
    """Δημιουργεί το data/news.duckdb με τους πίνακες:

    articles       (aid, id, created, month, category, title, url, title_norm)
    words          (word_id, word, df)   λεξιλόγιο, ταξινομημένο αλφαβητικά → οι λέξεις με κοινό
                                         πρόθεμα έχουν συνεχόμενα word_id
    postings_text  (word_id, aid)        ποια άρθρα περιέχουν κάθε λέξη (τίτλος + κείμενο)
    postings_title (word_id, aid)        το ίδιο, μόνο για τους τίτλους
    pairs_text     (w1, w2, aid)         ποια άρθρα περιέχουν τη λέξη w1 ακριβώς πριν από τη w2
    pairs_title    (w1, w2, aid)         (για φράσεις δύο λέξεων, π.χ. «ενεργειακή κρίση»)
    month_totals   (month, category, n)  σύνολο άρθρων ανά μήνα και κατηγορία

    και το data/texts.parquet (aid, text_norm): τα κείμενα, συμπιεσμένα, μόνο για τις σπάνιες
    περιπτώσεις που χρειάζεται έλεγχος στο ίδιο το κείμενο (φράσεις 3+ λέξεων, όροι με ψηφία/σύμβολα).
    """
    for f in (INDEX, SCRATCH, TEXTS):
        f.unlink(missing_ok=True)
    t0 = time.time()

    # Τα ενδιάμεσα (εκατομμύρια ζεύγη άρθρο-λέξη) μπαίνουν σε ξεχωριστό αρχείο που σβήνεται στο τέλος,
    # ώστε το τελικό αρχείο να μη μένει φουσκωμένο.
    con = duckdb.connect(str(SCRATCH))
    con.sql(f"SET memory_limit = '{MEMORY_LIMIT}'")
    con.sql(f"SET temp_directory = '{(ROOT / 'data' / '_tmp').as_posix()}'")
    con.sql(f"ATTACH '{INDEX.as_posix()}' AS out")

    con.sql(f"""
        CREATE TABLE src AS
        SELECT CAST(row_number() OVER (ORDER BY created, id) AS INTEGER) AS aid, *
        FROM read_parquet('{ARTICLES.as_posix()}')
        ORDER BY aid
    """)
    con.sql("""
        CREATE TABLE out.articles AS
        SELECT aid, id, created, month, category, title, url, title_norm FROM src ORDER BY aid
    """)
    con.sql(f"""
        COPY (SELECT aid, text_norm FROM src ORDER BY aid)
        TO '{TEXTS.as_posix()}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 8192)
    """)
    n = con.sql("SELECT count(*) FROM src").fetchone()[0]
    print(f"Άρθρα: {n:,}  [{time.time() - t0:.0f}s]")

    for field, col in [("text", "text_norm"), ("title", "title_norm")]:
        con.sql(f"CREATE TABLE tok_{field} (aid INTEGER, word VARCHAR)")
        con.sql(f"CREATE TABLE pair_{field} (aid INTEGER, w1 VARCHAR, w2 VARCHAR)")
        for start in range(1, n + 1, BATCH):
            con.sql(f"""
                CREATE OR REPLACE TEMP TABLE batch AS
                SELECT aid, regexp_extract_all({col}, '{WORD_SQL}') AS ws
                FROM src WHERE aid BETWEEN {start} AND {start + BATCH - 1}
            """)
            con.sql(f"INSERT INTO tok_{field} SELECT DISTINCT aid, unnest(ws) FROM batch")
            con.sql(f"""
                INSERT INTO pair_{field}
                SELECT DISTINCT aid, ws[i], ws[i + 1]
                FROM (SELECT aid, ws, unnest(range(1, len(ws))) AS i FROM batch)
            """)
            print(f"  λέξεις ({field}): {min(start + BATCH - 1, n):,}/{n:,} άρθρα  [{time.time() - t0:.0f}s]")

    # Ο τίτλος είναι μέρος του text_norm, άρα το λεξιλόγιο του κειμένου καλύπτει και τους τίτλους.
    con.sql("""
        CREATE TABLE out.words AS
        SELECT CAST(row_number() OVER (ORDER BY word) AS INTEGER) AS word_id, word, df
        FROM (SELECT word, count(*) AS df FROM tok_text GROUP BY word)
        ORDER BY word
    """)
    print(f"Λεξιλόγιο: {con.sql('SELECT count(*) FROM out.words').fetchone()[0]:,} λέξεις  [{time.time() - t0:.0f}s]")

    for field in ("text", "title"):
        con.sql(f"""
            CREATE TABLE out.postings_{field} AS
            SELECT w.word_id, t.aid
            FROM tok_{field} t JOIN out.words w USING (word)
            ORDER BY w.word_id, t.aid
        """)
        con.sql(f"""
            CREATE TABLE out.pairs_{field} AS
            SELECT a.word_id AS w1, b.word_id AS w2, p.aid
            FROM pair_{field} p
            JOIN out.words a ON a.word = p.w1
            JOIN out.words b ON b.word = p.w2
            ORDER BY w1, w2, aid
        """)
        rows = con.sql(f"SELECT count(*) FROM out.postings_{field}").fetchone()[0]
        pairs = con.sql(f"SELECT count(*) FROM out.pairs_{field}").fetchone()[0]
        print(f"Ευρετήριο ({field}): {rows:,} λέξη-άρθρο, {pairs:,} ζεύγη λέξεων  [{time.time() - t0:.0f}s]")

    con.sql("""
        CREATE TABLE out.month_totals AS
        SELECT month, category, count(*) AS n FROM out.articles GROUP BY ALL ORDER BY ALL
    """)

    con.sql("DETACH out")
    con.close()
    SCRATCH.unlink(missing_ok=True)
    print(f"Αποθηκεύτηκε: {INDEX} ({INDEX.stat().st_size / 1e6:,.0f} MB)")
    print(f"Αποθηκεύτηκε: {TEXTS} ({TEXTS.stat().st_size / 1e6:,.0f} MB)  [{time.time() - t0:.0f}s]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true", help="ξαναφτιάχνει και τα δύο αρχεία")
    args = parser.parse_args()

    if args.force or not ARTICLES.exists():
        raw = download()
        print(f"Πηγή: {raw}")
        build_articles(raw)
    else:
        print(f"Υπάρχει ήδη: {ARTICLES}")

    if args.force or not INDEX.exists():
        build_index()
    else:
        print(f"Υπάρχει ήδη: {INDEX}")
