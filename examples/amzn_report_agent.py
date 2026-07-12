# -*- coding: utf-8 -*-
"""
Agente AUTONOMO che produce un report quantitativo su un titolo usando SOLO i
tool provider di LazyTools — da lanciare in Spyder.

A differenza di amzn_report_demo.py (che chiamava le librerie in-process), qui
NON orchestro io: costruisco un Agent lazybridge, gli monto i tool di LazyTools
e gli do un incarico. È l'agente che decide ed esegue i passi in autonomia:

    register listing -> ensure prezzi -> ensure+leggi bilancio -> vola/outlier
    -> carica returns dal datahub -> stima HMM 3 regimi -> genera plot dei regimi
    -> compone il memo (tabelle + figure) -> render HTML -> salva su file

Tool montati (cinque provider di LazyTools):
  - DataHubTools(allow_refresh=True) : register/ensure prezzi, ensure/leggi bilancio SEC
  - StatisticalAnalysisTools()       : volatilità e outlier (leggono la serie dal hub)
  - RegimeTools(allow_write=True)    : load-from-datahub, fit HMM, generate plots, params
  - ReportTools(artifacts=...)       : render_memo / render_memo_html (con figure chart:/regimes:)
  - ReportFiles(base_dir=...)        : save_report

Prerequisiti: come amzn_report_demo.py (lazybridge, lazytools[charts],
market_data_hub, lazystats[regimes], matplotlib, openai) + chiave DeepSeek in
deepseek.env o DEEPSEEK_API_KEY. Serve rete (Yahoo + SEC).

Modello: default il tier economico 'deepseek-v4-flash'. Il flusso è lungo
(~12 tool call in sequenza + composizione del memo): se il modello economico
non completa in modo affidabile, esporta LB_MODEL=deepseek-v4-pro.
"""

from __future__ import annotations

import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # rendering headless dei grafici dei regimi

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
TICKER = "AMZN"
COMPANY = "Amazon.com Inc"

GITHUB = Path(r"C:\Users\Administrator\Documents\GitHub")
HUB_DB = str(GITHUB / "market-data-hub" / "market_data.duckdb")
os.environ.setdefault("MARKET_DATA_DB", HUB_DB)

OUT_DIR = GITHUB / "reports_demo"
OUT_DIR.mkdir(exist_ok=True)
REGIME_DB = str(OUT_DIR / "amzn_agent_regimes.db")   # depot dei regimi per questo run
MODEL = os.getenv("LB_MODEL", "deepseek-v4-flash")

# chiave DeepSeek da deepseek.env (chiave nuda) se non già in ambiente
_env = GITHUB / "deepseek.env"
if not os.environ.get("DEEPSEEK_API_KEY") and _env.exists():
    os.environ["DEEPSEEK_API_KEY"] = _env.read_text(encoding="utf-8").strip()

# Il depot dei regimi va inizializzato una volta: i tool regime_* vi scrivono.
import lazystats.regimes.db as _rdb
_rdb.init_regime_db(REGIME_DB)

# --------------------------------------------------------------------------- #
# Tool provider di LazyTools + agente
# --------------------------------------------------------------------------- #
from lazybridge import Agent, LLMEngine, Session
from lazytools.connectors.datahub import DataHubTools
from lazytools.connectors.regimes import RegimeTools
from lazytools.statistical_analysis import StatisticalAnalysisTools
from lazytools.report import ReportTools, ReportFiles, ecosystem_resolvers

# Cablaggio delle figure a build-time: senza ecosystem_resolvers, le figure
# chart:/regimes: NON si risolvono al render.
resolvers = ecosystem_resolvers(
    datahub_db_path=HUB_DB,        # scheme chart:  -> grafico on-demand dal DuckDB
    regimes_db=REGIME_DB,          # scheme regimes: -> PNG dal depot LazyStats
    file_base_dir=str(OUT_DIR),    # sandbox per lo scheme file:
)

report_files = ReportFiles(base_dir=str(OUT_DIR))
tools = [
    DataHubTools(allow_refresh=True),          # register/ensure prezzi + bilancio
    StatisticalAnalysisTools(),                # vola / outlier
    RegimeTools(allow_write=True),             # load / fit / plots regimi
    # files=... abilita save_memo_html: rende e salva in UN passo, così l'HTML
    # con le immagini base64 non deve ripassare dall'LLM (che lo troncherebbe).
    ReportTools(artifacts=resolvers, files=report_files),
    report_files,                              # save_report (testo generico)
]

SYSTEM = """\
Sei un analista quantitativo con accesso a una serie di tool per: dati di
mercato e bilanci societari, analisi statistica dei rendimenti, modelli a
regimi di volatilità, e generazione/salvataggio di report. Ispeziona le
descrizioni dei tool e usali in autonomia per portare a termine l'incarico,
un passo alla volta, leggendo l'esito di ogni chiamata (inclusi eventuali
errori) e adattandoti. Non inventare mai numeri, chiavi o riferimenti: ricavali
sempre dagli output dei tool. Scrivi le parti testuali in italiano. Oggi è il
2026-07-12.
"""

TASK = f"""\
Produci e salva su file un report HTML di analisi quantitativa su {COMPANY}
({TICKER}). Il report deve contenere:
  - un grafico dell'andamento del prezzo nell'ultimo mese;
  - le principali voci del bilancio più recente e un tuo breve commento su
    solidità patrimoniale, redditività e liquidità;
  - un'analisi di volatilità e degli outlier dei rendimenti da inizio 2026;
  - un modello a 3 regimi di volatilità stimato sull'intera storia dei prezzi
    (dal 2015), con i parametri principali del modello e un grafico dei prezzi
    con i regimi evidenziati.
Assicurati che i dati del titolo siano disponibili prima di analizzarli.
"""

if __name__ == "__main__":
    sess = Session(db=str(OUT_DIR / "amzn_agent.db"), console=True)  # mostra i tool call
    try:
        agent = Agent(engine=LLMEngine(MODEL, system=SYSTEM), tools=tools,
                      session=sess, name="quant_report_agent")
        env = agent(TASK)
        print("\n=== RISPOSTA FINALE DELL'AGENTE ===")
        # la console di Windows (cp1252) può non reggere emoji nel testo dell'LLM
        print(env.text().encode("ascii", "replace").decode("ascii"))
        # individua il report salvato (l'agente chiama save_report che scrive qui)
        saved = sorted(OUT_DIR.glob("amzn_report_agent*.html"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if saved:
            print(f"\nReport salvato: {saved[0]}  ({saved[0].stat().st_size:,} byte)")
        else:
            print("\nATTENZIONE: nessun HTML salvato — controlla i tool call sopra.")
    finally:
        sess.close()
