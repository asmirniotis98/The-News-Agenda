"""Κανονικοποίηση ελληνικού κειμένου για αναζήτηση όρων.

Ίδιος κανόνας για τα άρθρα (σε SQL, μέσω DuckDB) και για τον όρο του χρήστη (σε Python):
πεζά, αφαίρεση τόνων/διαλυτικών, τελικό ς -> σ.
"""

import unicodedata


def normalize(text: str) -> str:
    """'Πανδημία', 'ΠΑΝΔΗΜΙΑ', 'πανδημια' -> 'πανδημια'."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text.lower())
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", stripped).replace("ς", "σ")


# Λέξη = συνεχόμενα γράμματα (Unicode). Ίδιος ορισμός με τα όρια λέξης της αναζήτησης ([^\p{L}]).
WORD_SQL = r"\p{L}+"


def words(text: str) -> list[str]:
    """Οι λέξεις ενός κανονικοποιημένου κειμένου, με τον ίδιο ορισμό όπως το WORD_SQL."""
    out, cur = [], []
    for ch in text:
        if ch.isalpha():
            cur.append(ch)
        elif cur:
            out.append("".join(cur))
            cur = []
    if cur:
        out.append("".join(cur))
    return out


def normalize_sql(column: str) -> str:
    """Η ίδια κανονικοποίηση ως έκφραση DuckDB SQL για μια στήλη."""
    return f"replace(strip_accents(lower(coalesce({column}, ''))), 'ς', 'σ')"
