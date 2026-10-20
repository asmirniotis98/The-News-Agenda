"""Σύγκριση αρχικής αναζήτησης (ScanIndex) και ευρετηρίου λέξεων (NewsIndex).

Μετρά για κάθε υλοποίηση, σε ξεχωριστή διεργασία ώστε η μνήμη να μετριέται καθαρά:
- χρόνο φόρτωσης και μνήμη (RAM) της διεργασίας,
- χρόνο ανά αναζήτηση: πρώτη εκτέλεση και διάμεσος 3 επαναλήψεων,
- ορθότητα: ίδιος αριθμός άρθρων ανά μήνα με την αρχική υλοποίηση, για κάθε περίπτωση.

Εκτέλεση:  python scripts/benchmark_search.py
Αποτελέσματα: evaluation/search_benchmark.md
"""

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "evaluation" / "search_benchmark.md"

# (όρος, όλοι οι τύποι, πεδίο, κατηγορίες) — καλύπτει: πρόθεμα, ακριβή λέξη, σύντομη λέξη, φράση,
# λέξη με σύμβολα/ψηφία, μόνο τίτλους, φίλτρο κατηγορίας, όρο χωρίς αποτελέσματα.
CASES = [
    ("μνημόνιο", True, "text", []),
    ("πανδημία", True, "text", []),
    ("ακρίβεια", True, "text", []),
    ("Ουκρανία", True, "text", []),
    ("ΕΚΤ", True, "text", []),
    ("ενεργειακή κρίση", True, "text", []),
    ("Covid-19", True, "text", []),
    ("μνημόνιο", False, "text", []),
    ("μνημόνιο", True, "title", []),
    ("σεισμός", True, "text", ["Ελλάδα"]),
    ("Τραμπ", True, "title", ["Κόσμος"]),
    ("ξξξξξξ", True, "text", []),
]


def worker(impl: str) -> None:
    import psutil

    from src.search import NewsIndex, ScanIndex, make_query

    proc = psutil.Process()
    t = time.perf_counter()
    idx = NewsIndex() if impl == "index" else ScanIndex()
    load_s = time.perf_counter() - t
    load_mb = proc.memory_info().rss / 1e6

    results = []
    for term, all_forms, field, cats in CASES:
        q = make_query(term, all_forms)
        times = []
        for _ in range(4):
            t = time.perf_counter()
            df = idx.monthly(q, field, cats)
            times.append(time.perf_counter() - t)
        t = time.perf_counter()
        idx.articles(q, df.loc[df["hits"].idxmax(), "month"], field, cats)
        articles_s = time.perf_counter() - t
        results.append({
            "first_s": times[0], "warm_s": statistics.median(times[1:]), "articles_s": articles_s,
            "hits": df["hits"].astype(int).tolist(), "total_hits": int(df["hits"].sum()),
        })
    mem = proc.memory_info()
    peak_mb = getattr(mem, "peak_wset", mem.rss) / 1e6
    print(json.dumps({"load_s": load_s, "load_mb": load_mb, "peak_mb": peak_mb, "results": results}))


def run(impl: str) -> dict:
    out = subprocess.run([sys.executable, __file__, "--worker", impl], capture_output=True, text=True,
                         encoding="utf-8", cwd=ROOT, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> None:
    from src.search import INDEX

    print("Αρχική αναζήτηση (scan)…")
    scan = run("scan")
    print("Ευρετήριο (index)…")
    index = run("index")

    rows, all_ok = [], True
    for (term, all_forms, field, cats), s, i in zip(CASES, scan["results"], index["results"]):
        ok = s["hits"] == i["hits"]
        all_ok &= ok
        label = f"«{term}»" + ("" if all_forms else " (ακριβής)") + (" · τίτλοι" if field == "title" else "") \
            + (f" · {', '.join(cats)}" if cats else "")
        rows.append(f"| {label} | {s['total_hits']:,} | {i['total_hits']:,} | {'✅' if ok else '❌'} "
                    f"| {s['first_s']:.2f} / {s['warm_s']:.2f} | {i['first_s']:.3f} / {i['warm_s']:.3f} "
                    f"| {s['warm_s'] / max(i['warm_s'], 1e-6):.0f}× |")

    med = lambda r, k: statistics.median(x[k] for x in r["results"])  # noqa: E731
    report = f"""# Μέτρηση αναζήτησης: πλήρης σάρωση vs ευρετήριο λέξεων

Παράγεται από `scripts/benchmark_search.py` ({time.strftime('%d/%m/%Y %H:%M')}).

- **Σάρωση (ScanIndex):** όλα τα κείμενα στη μνήμη, regex σε κάθε άρθρο.
- **Ευρετήριο (NewsIndex):** λεξιλόγιο + πίνακας «λέξη → άρθρα» στο `data/news.duckdb`, πάνω στον δίσκο.

## Σύνοψη

| | Σάρωση | Ευρετήριο |
|---|---|---|
| Χρόνος φόρτωσης | {scan['load_s']:.1f} s | {index['load_s']:.2f} s |
| Μνήμη μετά τη φόρτωση | {scan['load_mb']:,.0f} MB | {index['load_mb']:,.0f} MB |
| Μέγιστη μνήμη (peak) | {scan['peak_mb']:,.0f} MB | {index['peak_mb']:,.0f} MB |
| Διάμεσος χρόνος αναζήτησης (επανάληψη) | {med(scan, 'warm_s'):.2f} s | {med(index, 'warm_s'):.3f} s |
| Διάμεσος χρόνος λίστας άρθρων ενός μήνα | {med(scan, 'articles_s'):.2f} s | {med(index, 'articles_s'):.3f} s |
| Μέγεθος αρχείου δεδομένων | {(ROOT / 'data' / 'articles.parquet').stat().st_size / 1e6:,.0f} MB (parquet) | {INDEX.stat().st_size / 1e6:,.0f} MB (duckdb) |
| Ίδια αποτελέσματα σε όλες τις περιπτώσεις | — | {'✅ ναι' if all_ok else '❌ ΟΧΙ'} |

## Ανά περίπτωση

Χρόνοι σε δευτερόλεπτα: πρώτη εκτέλεση / διάμεσος 3 επαναλήψεων. «Ίδια» = ίδιος αριθμός άρθρων σε **κάθε** μήνα.

| Περίπτωση | Άρθρα (σάρωση) | Άρθρα (ευρετήριο) | Ίδια | Σάρωση (s) | Ευρετήριο (s) | Επιτάχυνση |
|---|---|---|---|---|---|---|
""" + "\n".join(rows) + "\n"
    OUT.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        worker(sys.argv[2])
    else:
        main()
