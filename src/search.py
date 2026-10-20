"""Αναζήτηση όρου στα άρθρα και χρονοσειρά άρθρων ανά μήνα."""

from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from src.normalize import normalize, words

DATA = Path(__file__).resolve().parents[1] / "data" / "articles.parquet"
INDEX = Path(__file__).resolve().parents[1] / "data" / "news.duckdb"

# Μήνες με λιγότερα άρθρα θεωρούνται ημιτελείς (π.χ. Ιούνιος 2011: 1 άρθρο) και εξαιρούνται.
MIN_MONTH_ARTICLES = 50

# Συχνές καταλήξεις ονομάτων/επιθέτων (κανονικοποιημένες), από τη μεγαλύτερη στη μικρότερη.
_SUFFIXES = sorted(
    ["ιεσ", "ιων", "ιασ", "ια", "ιο", "ιου", "ου", "οσ", "ον", "οι", "ων", "ουσ",
     "εσ", "ασ", "ησ", "α", "η", "ο", "ε", "ι", "υ", "ω"],
    key=len, reverse=True,
)
MIN_STEM = 4

FIELDS = {"text": "text_norm", "title": "title_norm"}


@dataclass(frozen=True)
class Query:
    """Ένας όρος έτοιμος για αναζήτηση: μία ή περισσότερες λέξεις, η καθεμία ως πρόθεμα ή ακριβής λέξη."""

    term: str                            # όπως τον έγραψε ο χρήστης
    words: tuple[tuple[str, bool], ...]  # (κανονικοποιημένη λέξη ή θέμα, True αν αναζητείται ως πρόθεμα)

    @property
    def pattern(self) -> str:
        # Το  του RE2 είναι μόνο ASCII, οπότε τα όρια λέξης ορίζονται με κλάση γραμμάτων Unicode.
        parts = [_escape(w) + (r"\p{L}*" if prefix else "") for w, prefix in self.words]
        return r"(^|[^\p{L}])" + r"[^\p{L}]+".join(parts) + r"([^\p{L}]|$)"

    @property
    def anchor(self) -> str:
        """Η μεγαλύτερη λέξη, για γρήγορο προφίλτρο με contains()."""
        return max((w for w, _ in self.words), key=len)

    @property
    def label(self) -> str:
        return " ".join(w + ("…" if prefix else "") for w, prefix in self.words)


def _escape(text: str) -> str:
    return "".join("\\" + ch if ch in r".^$*+?()[]{}|\\" else ch for ch in text)


def stem(word: str) -> str:
    """Αφαιρεί μία κατάληξη: 'μνημονιο' -> 'μνημον', 'πανδημια' -> 'πανδημ'."""
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= MIN_STEM:
            return word[: -len(suffix)]
    return word


def make_query(term: str, all_forms: bool = True) -> Query:
    """all_forms=True: κάθε λέξη ως θέμα + οποιαδήποτε κατάληξη. Λέξεις έως 4 γράμματα (π.χ. «ΕΚΤ»)
    αναζητούνται πάντα ακριβώς, αλλιώς θα ταίριαζαν με άσχετες λέξεις («εκτός»)."""
    parts = []
    for w in normalize(term).split():
        if all_forms and len(w) > MIN_STEM:
            parts.append((stem(w), True))
        else:
            parts.append((w, False))
    return Query(term=term.strip(), words=tuple(parts))


class NewsIndex:
    """Αναζήτηση μέσω του ευρετηρίου του data/news.duckdb (βλ. scripts/prepare_data.py).

    Για κάθε λέξη του όρου βρίσκουμε στο λεξιλόγιο το εύρος word_id που ταιριάζει (οι λέξεις είναι
    ταξινομημένες, άρα όσες έχουν κοινό πρόθεμα είναι συνεχόμενες) και από εκεί τα άρθρα:
    - μία λέξη: πίνακας postings — ακριβές αποτέλεσμα,
    - φράση δύο λέξεων: πίνακας pairs (διαδοχικές λέξεις) — ακριβές αποτέλεσμα,
    - φράση 3+ λέξεων ή όρος με ψηφία/σύμβολα («covid-19»): το ευρετήριο δίνει τα υποψήφια άρθρα και
      το regex τα επιβεβαιώνει στο κείμενο (data/texts.parquet) — σωστό αλλά πιο αργό.
    """

    def __init__(self, path: Path = INDEX, threads: int = 4):
        # Λίγα νήματα: οι συνήθεις αναζητήσεις δεν επηρεάζονται (~0,02 s), ενώ ο έλεγχος στα κείμενα
        # αποσυμπιέζει λιγότερα κομμάτια ταυτόχρονα — μέγιστη μνήμη ~330 MB αντί ~1 GB με 20 νήματα.
        self.con = duckdb.connect(str(path), read_only=True, config={"threads": threads})
        self.texts = (Path(path).parent / "texts.parquet").as_posix()
        self.months = self.con.sql(f"""
            SELECT month, sum(n) AS n FROM month_totals GROUP BY 1
            HAVING n >= {MIN_MONTH_ARTICLES} ORDER BY 1
        """).df()
        self.categories = [r[0] for r in self.con.sql(
            "SELECT category FROM month_totals GROUP BY 1 ORDER BY sum(n) DESC").fetchall()]
        # Οι κατηγορίες του ΑΠΕ-ΜΠΕ άλλαξαν με τα χρόνια (π.χ. «Ελλάδα», «Κόσμος» μόνο από το 2017).
        self.category_since = {c: m for c, m in self.con.sql(f"""
            SELECT category, min(month) FROM month_totals WHERE n >= {MIN_MONTH_ARTICLES // 5} GROUP BY 1
        """).fetchall()}

    @staticmethod
    def _word_range(cur, word: str, prefix: bool) -> tuple[int, int] | None:
        cond = "starts_with(word, $w)" if prefix else "word = $w"
        lo, hi = cur.execute(f"SELECT min(word_id), max(word_id) FROM words WHERE {cond}", {"w": word}).fetchone()
        return None if lo is None else (lo, hi)

    def _matches(self, cur, q: Query, field: str) -> tuple[str, dict] | None:
        """SQL που επιστρέφει τα aid των άρθρων με τον όρο, ή None αν σίγουρα δεν υπάρχει κανένα."""
        pure = all(words(w) == [w] for w, _ in q.words)  # κάθε λέξη μόνο γράμματα
        if pure:
            tokens = list(q.words)
        else:
            # Λέξη με ψηφία/σύμβολα: τα τμήματά της με γράμματα, ως προθέματα, δίνουν υπερσύνολο.
            tokens = [(r, True) for w, _ in q.words for r in words(w)]
        if not tokens:  # ο όρος δεν έχει καθόλου γράμματα (π.χ. «2015»): έλεγχος σε όλα τα κείμενα
            return self._verify(cur, "SELECT aid FROM articles", q, field)

        ranges = []
        for word, prefix in tokens:
            r = self._word_range(cur, word, prefix)
            if r is None:
                return None
            ranges.append(r)

        if pure and len(ranges) <= 2:
            if len(ranges) == 1:
                (lo, hi), = ranges
                return f"SELECT DISTINCT aid FROM postings_{field} WHERE word_id BETWEEN {lo} AND {hi}", {}
            (lo1, hi1), (lo2, hi2) = ranges
            return (f"SELECT DISTINCT aid FROM pairs_{field} "
                    f"WHERE w1 BETWEEN {lo1} AND {hi1} AND w2 BETWEEN {lo2} AND {hi2}"), {}

        if pure:  # 3+ λέξεις: κάθε διαδοχικό ζεύγος πρέπει να υπάρχει στο άρθρο
            selects = [f"SELECT aid FROM pairs_{field} WHERE w1 BETWEEN {a[0]} AND {a[1]} "
                       f"AND w2 BETWEEN {b[0]} AND {b[1]}" for a, b in zip(ranges, ranges[1:])]
        else:
            selects = [f"SELECT aid FROM postings_{field} WHERE word_id BETWEEN {lo} AND {hi}" for lo, hi in ranges]
        candidates = " INTERSECT ".join(f"({s})" for s in selects)
        return self._verify(cur, candidates, q, field)

    def _verify(self, cur, candidates: str, q: Query, field: str) -> tuple[str, dict] | None:
        """Κρατά από τα υποψήφια άρθρα όσα ταιριάζουν πραγματικά με το regex του όρου.

        Τα κείμενα είναι ταξινομημένα κατά aid (= χρονολογικά), οπότε διαβάζεται μόνο το τμήμα του αρχείου
        ανάμεσα στο πρώτο και το τελευταίο υποψήφιο άρθρο — π.χ. για «covid-19» κυρίως το 2020–2022.
        """
        ids = [r[0] for r in cur.execute(candidates).fetchall()]
        if not ids:
            return None
        table, col = ("articles", "title_norm") if field == "title" else (f"read_parquet('{self.texts}')", "text_norm")
        rows = cur.execute(f"""
            SELECT aid FROM {table}
            WHERE aid BETWEEN {min(ids)} AND {max(ids)}
              AND aid IN (SELECT unnest($ids))
              AND regexp_matches({col}, $pattern)
        """, {"ids": ids, "pattern": q.pattern}).fetchall()
        return "SELECT unnest($aids) AS aid", {"aids": [r[0] for r in rows]}

    def monthly(self, q: Query, field: str = "text", categories: list[str] | None = None) -> pd.DataFrame:
        """Για κάθε μήνα: σύνολο άρθρων, άρθρα με τον όρο (hits) και ποσοστό (share, %)."""
        cur = self.con.cursor()
        params = {"months": list(self.months.month)}
        cat_filter = ""
        if categories:
            cat_filter = "AND list_contains($cats, category)"
            params["cats"] = categories
        match = self._matches(cur, q, field)
        if match is None:
            hits_sql = "SELECT NULL::DATE AS month, 0 AS hits WHERE false"
        else:
            msql, mparams = match
            params.update(mparams)
            hits_sql = f"""SELECT a.month, count(*) AS hits FROM articles a
                           WHERE a.aid IN ({msql}) {cat_filter} GROUP BY 1"""
        df = cur.execute(f"""
            WITH hits AS ({hits_sql}),
                 totals AS (SELECT month, sum(n) AS total FROM month_totals WHERE true {cat_filter} GROUP BY 1)
            SELECT m.month, coalesce(t.total, 0) AS total, coalesce(h.hits, 0) AS hits
            FROM (SELECT unnest($months) AS month) m
            LEFT JOIN totals t USING (month)
            LEFT JOIN hits h USING (month)
            ORDER BY m.month
        """, params).df()
        df["month"] = pd.to_datetime(df["month"])
        df["total"] = df["total"].astype(int)
        df["share"] = (100 * df["hits"] / df["total"]).where(df["total"] > 0, 0.0)
        return df

    def articles(self, q: Query, month, field: str = "text", categories: list[str] | None = None,
                 limit: int | None = None) -> pd.DataFrame:
        """Τα άρθρα ενός μήνα που περιέχουν τον όρο (με τους τίτλους που ταιριάζουν πρώτους)."""
        cur = self.con.cursor()
        match = self._matches(cur, q, field)
        if match is None:
            return pd.DataFrame(columns=["created", "category", "title", "url", "in_title"])
        msql, params = match
        params = {**params, "pattern": q.pattern, "month": pd.Timestamp(month).date()}
        cat_filter = ""
        if categories:
            cat_filter = "AND list_contains($cats, category)"
            params["cats"] = categories
        lim = f"LIMIT {int(limit)}" if limit else ""
        return cur.execute(f"""
            SELECT created, category, title, url, regexp_matches(title_norm, $pattern) AS in_title
            FROM articles
            WHERE month = $month AND aid IN ({msql}) {cat_filter}
            ORDER BY in_title DESC, created
            {lim}
        """, params).df()


class ScanIndex:
    """Αρχική υλοποίηση: όλα τα κείμενα στη μνήμη, regex σε κάθε άρθρο σε κάθε ερώτηση.
    Αργή (~1,5 s ανά όρο, ~660 MB μνήμη) αλλά απλή· κρατιέται ως σημείο αναφοράς για τον έλεγχο ορθότητας
    και τη μέτρηση του NewsIndex (scripts/benchmark_search.py)."""

    def __init__(self, path: Path = DATA):
        self.con = duckdb.connect()
        self.con.sql(f"""
            CREATE TABLE articles AS
            SELECT id, created, month, category, title, url, title_norm, text_norm
            FROM read_parquet('{Path(path).as_posix()}')
        """)
        self.months = self.con.sql(f"""
            SELECT month, count(*) AS n FROM articles GROUP BY 1
            HAVING n >= {MIN_MONTH_ARTICLES} ORDER BY 1
        """).df()
        self.categories = [r[0] for r in self.con.sql(
            "SELECT category FROM articles GROUP BY 1 ORDER BY count(*) DESC").fetchall()]

    def _where(self, q: Query, field: str, categories: list[str] | None) -> tuple[str, dict]:
        col = FIELDS[field]
        # Το contains() είναι γρήγορο προφίλτρο· το regex ελέγχει την αρχή της λέξης.
        sql = f"contains({col}, $anchor) AND regexp_matches({col}, $pattern)"
        params = {"anchor": q.anchor, "pattern": q.pattern}
        if categories:
            sql += " AND list_contains($cats, category)"
            params["cats"] = categories
        return sql, params

    def monthly(self, q: Query, field: str = "text", categories: list[str] | None = None) -> pd.DataFrame:
        """Για κάθε μήνα: σύνολο άρθρων, άρθρα με τον όρο (hits) και ποσοστό (share, %)."""
        where, params = self._where(q, field, categories)
        cat_filter = "WHERE list_contains($cats, category)" if categories else ""
        df = self.con.sql(f"""
            WITH hits AS (SELECT month, count(*) AS hits FROM articles WHERE {where} GROUP BY 1),
                 totals AS (SELECT month, count(*) AS total FROM articles {cat_filter} GROUP BY 1)
            SELECT m.month, coalesce(t.total, 0) AS total, coalesce(h.hits, 0) AS hits
            FROM (SELECT unnest($months) AS month) m
            LEFT JOIN totals t USING (month)
            LEFT JOIN hits h USING (month)
            ORDER BY m.month
        """, params={**params, "months": list(self.months.month)}).df()
        df["month"] = pd.to_datetime(df["month"])
        df["share"] = (100 * df["hits"] / df["total"]).where(df["total"] > 0, 0.0)
        return df

    def articles(self, q: Query, month, field: str = "text", categories: list[str] | None = None,
                 limit: int | None = None) -> pd.DataFrame:
        """Τα άρθρα ενός μήνα που περιέχουν τον όρο (με τους τίτλους που ταιριάζουν πρώτους)."""
        where, params = self._where(q, field, categories)
        lim = f"LIMIT {int(limit)}" if limit else ""
        return self.con.sql(f"""
            SELECT created, category, title, url,
                   regexp_matches(title_norm, $pattern) AS in_title
            FROM articles
            WHERE month = $month AND {where}
            ORDER BY in_title DESC, created
            {lim}
        """, params={**params, "month": pd.Timestamp(month).date()}).df()
