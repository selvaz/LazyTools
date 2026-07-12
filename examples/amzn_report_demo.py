# -*- coding: utf-8 -*-
"""
Demo end-to-end dell'ecosistema Lazy* / market-data-hub — da lanciare in Spyder.

Cosa fa, in ordine:
  1) Ingesta la serie storica giornaliera di un titolo NON ancora nel DB (AMZN)
     dal 2015 tramite market-data-hub (fonte Yahoo), e scarica il bilancio
     (SEC/EDGAR: filings + XBRL company facts).
  2) Analizza volatilità e outlier sui ritorni GIORNALIERI da inizio 2026 (YTD).
  3) Stima un HMM a ESATTAMENTE 3 regimi su TUTTO il campione dal 2015 e legge
     i parametri stimati (medie/vol per regime, matrice di transizione,
     distribuzione stazionaria, regime corrente).
  4) Fa commentare il bilancio a un LLM economico (DeepSeek).
  5) Costruisce un report HTML autosufficiente con: parametri precisi del modello,
     principali voci di bilancio, il commento dell'LLM, il grafico del prezzo
     dell'ultimo mese (scheme chart:) e il grafico prezzi+regimi HMM (scheme regimes:).

Prerequisiti (già presenti nel tuo ambiente Spyder):
  lazybridge, lazytools[charts], market_data_hub, lazystats[regimes], matplotlib, duckdb, openai.
Serve connessione a internet (Yahoo + SEC) e la chiave DeepSeek in deepseek.env
(alla root della cartella GitHub) oppure in DEEPSEEK_API_KEY.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # niente finestre: rende i grafici headless (salvati su bytes)

import numpy as np

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
TICKER = "AMZN"
COMPANY = "Amazon.com Inc"
START = "2015-01-01"          # inizio campione completo
YTD_START = "2026-01-01"      # inizio analisi vola/outlier
LAST_MONTH_START = "2026-06-12"

GITHUB = Path(__file__).resolve().parents[2]  # the workspace root (LazyTools/..)
HUB_DB = str(GITHUB / "market-data-hub" / "market_data.duckdb")
os.environ.setdefault("MARKET_DATA_DB", HUB_DB)

OUT_DIR = GITHUB / "reports_demo"
OUT_DIR.mkdir(exist_ok=True)
REGIME_DB = str(OUT_DIR / "amzn_regimes.db")     # depot SQLite per i plot dei regimi
OUT_HTML = str(OUT_DIR / "amzn_report.html")

# chiave DeepSeek da deepseek.env (chiave nuda) se non già in ambiente
_env = GITHUB / "deepseek.env"
if not os.environ.get("DEEPSEEK_API_KEY") and _env.exists():
    os.environ["DEEPSEEK_API_KEY"] = _env.read_text(encoding="utf-8").strip()

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def usd(x) -> str:
    """Formatta un importo in USD in modo leggibile (T/B/M)."""
    if x is None:
        return "n/d"
    x = float(x)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(x) >= div:
            return f"${x / div:.2f}{suf}"
    return f"${x:,.0f}"


def pct(x, dp=2) -> str:
    return "n/d" if x is None else f"{100 * float(x):.{dp}f}%"


# --------------------------------------------------------------------------- #
# 1) Ingestione prezzi (AMZN non è nell'universo config -> register + ensure)
# --------------------------------------------------------------------------- #
print(f"[1/5] Ingestione prezzi {TICKER} dal {START} ...")
from market_data_hub.services.prices import register_listing, ensure_price_history

register_listing(TICKER, exchange="NASDAQ", currency="USD", kind="EQUITY", name=COMPANY)
ing = ensure_price_history(TICKER, start=START)
print(f"      status={ing['status']} righe_aggiunte={ing.get('rows_added')} "
      f"riusato={ing.get('reused')} listing={ing.get('listing_id')}")

# --------------------------------------------------------------------------- #
# 1b) Bilancio (SEC/EDGAR) — degrada con eleganza se la rete SEC non risponde
# --------------------------------------------------------------------------- #
print(f"[1/5] Bilancio SEC/EDGAR di {TICKER} ...")
bs_items: dict = {}
try:
    from market_data_hub.services.financials import (
        ensure_filings_and_facts, get_statement, get_facts,
    )
    ensure_filings_and_facts(TICKER)  # risolve AMZN->CIK e scarica facts XBRL
    bal = get_statement(TICKER, statement="balance", periods=4)
    inc = get_statement(TICKER, periods=4)  # tutte le linee mappate (revenue, net_income, ocf)

    def _line(stmt, key):
        try:
            per = stmt["periods"][0]
            return stmt["lines"][key][per]["value"], per
        except Exception:
            return None, None

    assets, per = _line(bal, "assets")
    bs_items = {
        "period": per,
        "assets": assets,
        "liabilities": _line(bal, "liabilities")[0],
        "equity": _line(bal, "equity")[0],
        "revenue": _line(inc, "revenue")[0],
        "net_income": _line(inc, "net_income")[0],
        "operating_cash_flow": _line(inc, "operating_cash_flow")[0],
    }
    try:  # 'cash' non è una linea mappata: concetto XBRL grezzo
        cf = get_facts(TICKER, concepts=["CashAndCashEquivalentsAtCarryingValue"],
                       forms=["10-K"], limit=8)
        bs_items["cash"] = cf["facts"][0]["value"] if cf.get("facts") else None
    except Exception:
        bs_items["cash"] = None
    print(f"      bilancio al {bs_items.get('period')}: attivo {usd(bs_items.get('assets'))}, "
          f"ricavi {usd(bs_items.get('revenue'))}")
except Exception as e:  # pragma: no cover
    print(f"      bilancio non disponibile ({type(e).__name__}: {e}) — il report lo segnala")

# --------------------------------------------------------------------------- #
# 2) Ritorni giornalieri + volatilità/outlier YTD 2026
# --------------------------------------------------------------------------- #
print("[2/5] Ritorni giornalieri e analisi volatilità/outlier (YTD 2026) ...")
from market_data_hub.extract import extract_returns

rets_full, _meta = extract_returns(TICKER, start=START, frequency="D")  # log-returns giornalieri
SERIES = TICKER if TICKER in rets_full.columns else rets_full.columns[0]
ser = rets_full[SERIES].dropna()
print(f"      {len(ser)} osservazioni giornaliere dal {ser.index[0].date()} al {ser.index[-1].date()}")

from lazystats.io.local import returns_from_dataframe
from lazystats.core import return_volatility, return_outliers

ytd = ser.loc[YTD_START:]
ds_ytd = returns_from_dataframe(ytd.to_frame(SERIES))
vol_ytd = return_volatility(ds_ytd, frequency="D")["volatility"][SERIES]
out_ytd = return_outliers(ds_ytd, frequency="D", threshold=2.0)
print(f"      YTD: {vol_ytd['observations']} giorni, vol annualizzata "
      f"{pct(vol_ytd['annualized_volatility'])}, outlier: {out_ytd['total_outliers']}")

# --------------------------------------------------------------------------- #
# 3) HMM a 3 regimi sull'intero campione + parametri esatti + plot nel depot
# --------------------------------------------------------------------------- #
print("[3/5] Stima HMM a 3 regimi sull'intero campione (2015->oggi) ...")
from lazystats.regimes import (
    init_regime_db, fit_regimes, generate_regime_plots,
    get_current_regime, regime_params_load,
)
from lazystats.regimes.tools import _swrite

init_regime_db(REGIME_DB)
_swrite("amzn_ret", {
    "Y": ser.values.reshape(-1, 1),
    "columns": [SERIES],
    "index": [d.strftime("%Y-%m-%d") for d in ser.index],
})
fit = fit_regimes(data_key="amzn_ret", result_key="amzn_hmm", model="panel",
                  S_min=3, S_max=3, criterion="bic", n_starts=20, random_state=42)

rec = regime_params_load(fit["params_key"])["params_by_series"][SERIES]
means = np.asarray(rec["means_"]).ravel()                 # media per regime (log-ret giornaliero)
stds = np.sqrt(np.asarray(rec["covars_"]).ravel())        # vol per regime = sqrt(varianza diag)
P = np.asarray(rec["transmat_"])                          # matrice di transizione (3x3)

# distribuzione stazionaria: autovettore sinistro di P per autovalore 1
w, v = np.linalg.eig(P.T)
stat = np.real(v[:, np.argmin(np.abs(w - 1.0))])
stat = stat / stat.sum()

srow = fit["series"][SERIES]
labels = [rs["label"] for rs in srow["regime_stats"]]
occ = [rs["occupancy_pct"] for rs in srow["regime_stats"]]
dur = [rs["expected_duration"] for rs in srow["regime_stats"]]
cur = get_current_regime(result_key="amzn_hmm", series_name=SERIES)
print(f"      regime corrente: {cur['current_label']} "
      f"(P={pct(cur['prob_current_state'])}); BIC={srow['bic']:.1f}")

plots = generate_regime_plots("amzn_hmm", points_per_year=252, last_years=100)
regime_plot_key = next(k for k in plots["plot_keys"] if "__series_with_regimes__" in k)
print(f"      plot regimi generato: {regime_plot_key}")

# Distribuzione MARGINALE (smoothed) del regime all'ultimo giorno.
# Attenzione: 'current_state' di get_current_regime è lo stato del PATH di
# Viterbi (sequenza globalmente più probabile, con vincoli di transizione),
# NON l'argmax della marginale del singolo giorno. I due possono differire:
# per non confondere, nel report mostriamo la marginale completa esplicita.
import lazystats.regimes.db as _rdb
_srow = _rdb.get_db().generic_read("amzn_hmm")["series"][SERIES]
last_probs = np.asarray(_srow["state_probs"][-1], dtype=float)  # [Low, Mid, High]
viterbi_state = int(np.asarray(_srow["states"])[-1])
marg_state = int(np.argmax(last_probs))
_vit_states = np.asarray(_srow["states"])
_disagree_pct = 100.0 * float((_vit_states != np.asarray(_srow["state_probs"]).argmax(1)).mean())
print(f"      marginale ultimo giorno: {dict(zip(labels, np.round(last_probs, 4)))} "
      f"(argmax={labels[marg_state]}); Viterbi={labels[viterbi_state]}")

# --------------------------------------------------------------------------- #
# 4) Commento LLM sul bilancio (DeepSeek, tier economico) — degrada se assente
# --------------------------------------------------------------------------- #
print("[4/5] Commento LLM sul bilancio (DeepSeek) ...")
comment = "(commento LLM non disponibile in questa esecuzione)"
if bs_items:
    try:
        from lazybridge import Agent, LLMEngine
        facts_txt = "; ".join(
            f"{k}={usd(bs_items[k])}" for k in
            ("assets", "liabilities", "equity", "revenue", "net_income",
             "operating_cash_flow", "cash") if bs_items.get(k) is not None
        )
        agent = Agent(engine=LLMEngine(
            os.getenv("LB_MODEL", "deepseek-v4-flash"),
            system=("Sei un analista finanziario. Scrivi in italiano, tono "
                    "professionale e conciso, senza elenchi puntati."),
        ))
        env_ = agent(
            f"Commenta in 4-6 frasi il bilancio più recente di {COMPANY} "
            f"(dati in USD, periodo {bs_items.get('period')}): {facts_txt}. "
            "Valuta solidità patrimoniale, redditività e liquidità; niente disclaimer."
        )
        comment = env_.text().strip()
        print(f"      commento generato ({len(comment)} caratteri)")
    except Exception as e:  # pragma: no cover
        print(f"      LLM non disponibile ({type(e).__name__}: {e})")

# --------------------------------------------------------------------------- #
# 5) Report HTML autosufficiente con tabelle + grafici
# --------------------------------------------------------------------------- #
print("[5/5] Costruzione e salvataggio del report HTML ...")
from lazytools.report import (
    Memo, Section, TableBlock, FigureBlock, render_html, ecosystem_resolvers,
)

# -- tabella regimi (dati precisi del modello) --
regimi_tbl = TableBlock(
    columns=["Regime", "Media gg", "Vol gg", "Vol annua", "Occupazione", "Durata attesa (gg)", "Stazionaria"],
    rows=[[
        labels[s],
        f"{means[s]:+.4%}",
        f"{stds[s]:.4%}",
        f"{stds[s] * np.sqrt(252):.2%}",
        f"{occ[s]:.1f}%",
        f"{dur[s]:.1f}",
        f"{stat[s]:.1%}",
    ] for s in range(len(labels))],
)
# -- matrice di transizione --
trans_tbl = TableBlock(
    columns=["da \\ a", *labels],
    rows=[[labels[i], *[f"{P[i, j]:.1%}" for j in range(len(labels))]] for i in range(len(labels))],
)
# -- volatilità/outlier YTD --
vola_tbl = TableBlock(
    columns=["Metrica", "Valore"],
    rows=[
        ["Osservazioni (YTD 2026)", str(vol_ytd["observations"])],
        ["Vol giornaliera (dev. std log-ret)", f"{vol_ytd['period_volatility']:.4%}"],
        ["Vol annualizzata", pct(vol_ytd["annualized_volatility"])],
        ["Ritorno medio giornaliero", f"{vol_ytd['mean_log_return']:+.4%}"],
        ["Outlier |z|>=2", str(out_ytd["total_outliers"])],
    ],
)
outlier_rows = [[o["date"], f"{o['log_return']:+.2%}", f"{o['z_score']:+.2f}", o["direction"]]
                for o in out_ytd["outliers"][:8]]
outlier_tbl = TableBlock(columns=["Data", "Ritorno", "z-score", "Direzione"],
                         rows=outlier_rows or [["—", "—", "—", "nessun outlier"]])
# -- bilancio --
if bs_items:
    bilancio_tbl = TableBlock(
        columns=["Voce", "Valore", f"Periodo {bs_items.get('period', '')}"],
        rows=[
            ["Totale attivo", usd(bs_items.get("assets")), ""],
            ["Totale passivo", usd(bs_items.get("liabilities")), ""],
            ["Patrimonio netto", usd(bs_items.get("equity")), ""],
            ["Ricavi", usd(bs_items.get("revenue")), ""],
            ["Utile netto", usd(bs_items.get("net_income")), ""],
            ["Cash flow operativo", usd(bs_items.get("operating_cash_flow")), ""],
            ["Cassa e equivalenti", usd(bs_items.get("cash")), ""],
        ],
    )
else:
    bilancio_tbl = TableBlock(columns=["Voce", "Valore"],
                              rows=[["Bilancio", "non disponibile (rete SEC)"]])

memo = Memo(
    title=f"{COMPANY} ({TICKER}) — Report quantitativo",
    as_of=datetime.now(timezone.utc),
    sections=[
        Section(
            title="Panoramica",
            body=(f"Campione giornaliero dal {ser.index[0].date()} al {ser.index[-1].date()} "
                  f"({len(ser)} osservazioni), fonte market-data-hub (Yahoo). "
                  f"Ingestione: {ing['status']}, {ing.get('rows_added', 0)} righe aggiunte."),
            figures=[FigureBlock(
                ref=(f"chart:symbols={TICKER}&start={LAST_MONTH_START}&end={TODAY}"
                     f"&field=adj_close&transform=level&frequency=D"),
                caption=f"{TICKER} — prezzo giornaliero (adj close), ultimo mese",
            )],
        ),
        Section(
            title="Volatilità e outlier (YTD 2026, giornalieri)",
            body="Volatilità campionaria dei log-ritorni e outlier a |z|>=2 sul periodo da inizio 2026.",
            tables=[vola_tbl, outlier_tbl],
        ),
        Section(
            title="Modello HMM a 3 regimi (2015→oggi)",
            body=(
                f"HMM gaussiano panel, 3 stati ordinati per volatilità crescente, "
                f"stimato su {len(ser)} osservazioni "
                f"(BIC {srow['bic']:.1f}, log-lik {srow['loglik']:.1f}).\n\n"
                f"Regime a oggi ({ser.index[-1].date()}) — distribuzione marginale "
                f"(smoothed): {labels[0]} {last_probs[0]:.1%}, {labels[1]} "
                f"{last_probs[1]:.1%}, {labels[2]} {last_probs[2]:.1%}. Lo stato più "
                f"probabile oggi è quindi {labels[marg_state]} "
                f"({last_probs[marg_state]:.1%}); persistenza {cur['steps_in_current_regime']} "
                f"giorni, ultimo cambio il {cur['last_change_date']}.\n\n"
                f"Nota metodologica: la colorazione del grafico usa il path di "
                f"Viterbi, che oggi assegna {labels[viterbi_state]}. Viterbi "
                f"massimizza la sequenza di stati globalmente più probabile (con i "
                f"vincoli della matrice di transizione), non la marginale del "
                f"singolo giorno, perciò può differire dall'argmax giornaliero — qui "
                f"nel {_disagree_pct:.1f}% dei giorni. Le due letture coincidono nei "
                f"regimi persistenti e divergono vicino ai punti di svolta."),
            tables=[regimi_tbl, trans_tbl],
            figures=[FigureBlock(ref=f"regimes:{regime_plot_key}",
                                 caption=f"{TICKER} — prezzi con bande di regime (HMM 3 stati)")],
        ),
        Section(
            title="Bilancio e commento",
            body=comment,
            tables=[bilancio_tbl],
        ),
    ],
    metadata={
        "titolo": TICKER,
        "generato_da": "amzn_report_demo.py",
        "regimi": "3 (HMM panel, vol-ascending)",
        "vol_annua_ytd": pct(vol_ytd["annualized_volatility"]),
    },
)

resolvers = ecosystem_resolvers(datahub_db_path=HUB_DB, regimes_db=REGIME_DB)
html = render_html(memo, artifacts=resolvers)
Path(OUT_HTML).write_text(html, encoding="utf-8")
print(f"\nReport salvato: {OUT_HTML}  ({len(html):,} byte, "
      f"{html.count('data:image/png;base64,')} grafici incorporati)")
print("Aprilo nel browser: è un HTML autosufficiente (immagini incluse).")
