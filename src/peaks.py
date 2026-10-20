"""Εντοπισμός μηνών-κορυφών στη χρονοσειρά ενός όρου.

Δύο μέθοδοι, πάνω στο ποσοστό άρθρων του μήνα (share):
- "prominence": τοπικά μέγιστα που ξεχωρίζουν από τους γύρω μήνες (scipy.signal.find_peaks).
- "zscore": μήνες πολύ πάνω από το «φυσιολογικό» των προηγούμενων 12 μηνών.
"""

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

METHODS = {"prominence": "Προεξοχή (prominence)", "zscore": "Απόκλιση από τον μέσο όρο 12 μηνών (z-score)"}

MIN_HITS = 5        # μια κορυφή χρειάζεται τουλάχιστον τόσα άρθρα (αλλιώς είναι θόρυβος μικρών αριθμών)
MIN_DISTANCE = 3    # ελάχιστη απόσταση (μήνες) μεταξύ δύο κορυφών
MIN_RELATIVE = 0.08  # μια κορυφή πρέπει να φτάνει τουλάχιστον το 8% της μέγιστης τιμής του όρου


def detect(df: pd.DataFrame, method: str = "prominence", n: int = 6, column: str = "share") -> pd.DataFrame:
    """Επιστρέφει τις n σημαντικότερες κορυφές: month, hits, total, share, score, rank (1 = η ισχυρότερη)."""
    if df.empty or df["hits"].sum() == 0:
        return _empty(df)
    values = df[column].to_numpy(dtype=float)

    if method == "prominence":
        idx, props = find_peaks(values, prominence=0.05 * values.max(), distance=MIN_DISTANCE)
        scores = props["prominences"]
    elif method == "zscore":
        idx, scores = _zscore_peaks(df[column])
    else:
        raise ValueError(f"Άγνωστη μέθοδος: {method}")

    peaks = df.iloc[idx].assign(score=scores)
    peaks = peaks[(peaks["hits"] >= MIN_HITS) & (peaks[column] >= MIN_RELATIVE * values.max())]
    peaks = peaks.nlargest(n, "score")
    peaks["rank"] = range(1, len(peaks) + 1)
    return peaks.sort_values("month").reset_index(drop=True)


def _zscore_peaks(series: pd.Series, window: int = 12, threshold: float = 2.0):
    """z = (τιμή - μέσος όρος προηγούμενων 12 μηνών) / τυπική απόκλιση· κρατάμε τοπικά μέγιστα με z ≥ threshold."""
    history = series.shift(1).rolling(window, min_periods=6)
    std = history.std().replace(0, np.nan)
    # Αν ο όρος δεν εμφανιζόταν καθόλου πριν (std = 0), η πρώτη εμφάνιση μετρά ως πολύ ισχυρή απόκλιση.
    z = ((series - history.mean()) / std).fillna(0).to_numpy(copy=True)
    first_rise = (history.max().fillna(0) == 0) & (series > 0)
    z[first_rise.to_numpy()] = threshold * 5
    idx, _ = find_peaks(z, height=threshold, distance=MIN_DISTANCE)
    return idx, z[idx]


def _empty(df: pd.DataFrame) -> pd.DataFrame:
    return df.iloc[0:0].assign(score=pd.Series(dtype=float), rank=pd.Series(dtype=int))
