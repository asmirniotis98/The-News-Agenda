"""Η Ατζέντα των Ειδήσεων — Streamlit εφαρμογή.

Εκτέλεση:  streamlit run app.py
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src import peaks
from src.search import NewsIndex, Query, make_query

MONTHS_GR = ["Ιανουάριος", "Φεβρουάριος", "Μάρτιος", "Απρίλιος", "Μάιος", "Ιούνιος",
             "Ιούλιος", "Αύγουστος", "Σεπτέμβριος", "Οκτώβριος", "Νοέμβριος", "Δεκέμβριος"]
EXAMPLES = ["μνημόνιο", "πανδημία", "ακρίβεια", "μεταναστευτικό", "σεισμός", "Ουκρανία", "ενεργειακή κρίση"]
TITLES_SHOWN = 30

st.set_page_config(page_title="Η Ατζέντα των Ειδήσεων", page_icon="📰", layout="wide")


def month_label(m) -> str:
    m = pd.Timestamp(m)
    return f"{MONTHS_GR[m.month - 1]} {m.year}"


@st.cache_resource(show_spinner="Φόρτωση των άρθρων του ΑΠΕ-ΜΠΕ…")
def load_index() -> NewsIndex:
    return NewsIndex()


@st.cache_data(show_spinner="Αναζήτηση…", max_entries=200)
def monthly(_idx: NewsIndex, q: Query, field: str, categories: tuple[str, ...]) -> pd.DataFrame:
    return _idx.monthly(q, field, list(categories))


@st.cache_data(max_entries=500)
def month_articles(_idx: NewsIndex, q: Query, month, field: str, categories: tuple[str, ...]) -> pd.DataFrame:
    return _idx.articles(q, month, field, list(categories))


def chart(df: pd.DataFrame, pk: pd.DataFrame, metric: str, label: str) -> go.Figure:
    y_title = "% άρθρων του μήνα" if metric == "share" else "αριθμός άρθρων"
    fig = go.Figure()
    fig.add_scatter(
        x=df["month"], y=df[metric], mode="lines", name=label, line=dict(width=2),
        customdata=df[["hits", "total", "share"]],
        hovertemplate="%{x|%m/%Y}<br>%{customdata[0]:,} από %{customdata[1]:,} άρθρα"
                      "<br>%{customdata[2]:.1f}%<extra></extra>",
    )
    if not pk.empty:
        fig.add_scatter(
            x=pk["month"], y=pk[metric], mode="markers+text", name="κορυφές",
            marker=dict(size=11, color="#d62728", line=dict(width=1, color="white")),
            text=[f"#{r}" for r in pk["rank"]], textposition="top center",
            hovertemplate="Κορυφή %{text}: %{x|%m/%Y}<extra></extra>",
        )
    fig.update_layout(
        height=420, margin=dict(l=10, r=10, t=30, b=10), showlegend=False,
        yaxis_title=y_title, hovermode="x unified" if pk.empty else "closest",
        yaxis_range=[0, df[metric].max() * 1.15],  # χώρος για τις ετικέτες των κορυφών
    )
    return fig


# ---------------------------------------------------------------- πλαϊνή στήλη
idx = load_index()

with st.sidebar:
    st.header("Αναζήτηση")
    if "term" not in st.session_state:
        st.session_state.term = EXAMPLES[0]

    def use_example():
        if st.session_state.example:
            st.session_state.term = st.session_state.example
            st.session_state.example = None

    term = st.text_input("Όρος", key="term", help="Μία λέξη ή φράση, π.χ. «μνημόνιο» ή «ενεργειακή κρίση».")
    st.pills("Παραδείγματα", EXAMPLES, selection_mode="single", key="example", on_change=use_example)

    all_forms = st.toggle("Όλοι οι τύποι της λέξης", value=True,
                          help="«μνημόνιο» βρίσκει και «μνημονίου», «μνημόνια». Απενεργοποιήστε για ακριβή λέξη.")
    field = st.radio("Αναζήτηση σε", ["text", "title"], horizontal=True,
                     format_func={"text": "τίτλο + κείμενο", "title": "μόνο τίτλους"}.get)
    metric = st.radio("Μέτρηση", ["share", "hits"], horizontal=True,
                      format_func={"share": "% άρθρων", "hits": "πλήθος άρθρων"}.get,
                      help="Το ποσοστό λαμβάνει υπόψη ότι ο συνολικός όγκος ειδήσεων αυξάνεται πολύ μετά το 2017.")
    categories = st.multiselect("Κατηγορίες", idx.categories, placeholder="Όλες")
    years = st.slider("Έτη", 2011, 2026, (2011, 2026))

    st.header("Κορυφές")
    method = st.selectbox("Μέθοδος", list(peaks.METHODS), format_func=peaks.METHODS.get)
    n_peaks = st.slider("Πλήθος κορυφών", 1, 10, 6)

# ---------------------------------------------------------------- κύρια σελίδα
st.title("📰 Η Ατζέντα των Ειδήσεων")
st.caption("Πώς κινήθηκε ένας όρος στην ειδησεογραφία του ΑΠΕ-ΜΠΕ (2011–2026) και τι συνέβη στις κορυφώσεις του.")

if not term.strip():
    st.info("Γράψτε έναν όρο στην αριστερή στήλη ή επιλέξτε ένα παράδειγμα.")
    st.stop()

q = make_query(term, all_forms)
cats = tuple(categories)
df = monthly(idx, q, field, cats)
df = df[df["month"].dt.year.between(*years)].reset_index(drop=True)
pk = peaks.detect(df, method, n_peaks, column=metric)

total_hits = int(df["hits"].sum())
c1, c2, c3 = st.columns(3)
c1.metric("Άρθρα με τον όρο", f"{total_hits:,}".replace(",", "."))
if total_hits:
    top = df.loc[df[metric].idxmax()]
    c2.metric("Μέγιστο", month_label(top["month"]), f"{top['share']:.1f}% των άρθρων", delta_color="off")
c3.metric("Αναζήτηση για", q.label)

if total_hits == 0:
    st.warning(f"Δεν βρέθηκαν άρθρα για «{term}». Δοκιμάστε άλλη λέξη ή ενεργοποιήστε «Όλοι οι τύποι της λέξης».")
    st.stop()

st.plotly_chart(chart(df, pk, metric, q.label), width="stretch")

st.subheader(f"Κορυφώσεις ({len(pk)})")
if pk.empty:
    st.info("Δεν εντοπίστηκαν σαφείς κορυφές. Δοκιμάστε την άλλη μέθοδο ή ευρύτερο διάστημα ετών.")

for r in pk.itertuples():
    with st.container(border=True):
        left, right = st.columns([3, 1])
        left.markdown(f"### #{r.rank} · {month_label(r.month)}")
        right.markdown(f"**{r.hits:,}** άρθρα  \n**{r.share:.1f}%** του μήνα".replace(",", "."))

        st.caption("🤖 Σύνοψη «τι συνέβη» — προστίθεται στη Φάση 4 (LLM).")

        arts = month_articles(idx, q, r.month, field, cats)
        with st.expander(f"Τίτλοι άρθρων ({len(arts)})"):
            for a in arts.head(TITLES_SHOWN).itertuples():
                st.markdown(f"- {a.created:%d/%m} · [{a.title}]({a.url}) · *{a.category}*")
            if len(arts) > TITLES_SHOWN:
                st.caption(f"… και {len(arts) - TITLES_SHOWN} ακόμη.")

st.divider()
st.caption("Δεδομένα: [glossAPI/amna-press](https://huggingface.co/datasets/glossAPI/amna-press) "
           "(ΑΠΕ-ΜΠΕ, CC BY-NC-ND 4.0) · Κώδικας: [GitHub](https://github.com/asmirniotis98/The-News-Agenda)")
