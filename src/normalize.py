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


def normalize_sql(column: str) -> str:
    """Η ίδια κανονικοποίηση ως έκφραση DuckDB SQL για μια στήλη."""
    return f"replace(strip_accents(lower(coalesce({column}, ''))), 'ς', 'σ')"
