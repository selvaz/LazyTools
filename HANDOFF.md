# Passaggio di consegne — estrattore bilanci + analista di credito

Incolla questo documento all'inizio di una nuova sessione.

---

## Il problema che devi risolvere

**L'estrazione dei bilanci non è affidabile, e questa è la cosa da affrontare
prima di qualunque altra.** Non è un difetto isolato: è uno schema che si è
ripetuto sei volte in cinque giorni, sempre nella stessa forma.

Il modello nomina la riga sbagliata. Il valore che ne esce è **reale,
correttamente scalato, preso dal documento giusto** — e significa un'altra cosa.
Nessun controllo a valle può accorgersene, perché non c'è niente di malformato
da rilevare.

Difetti misurati dal vivo, ciascuno trovato solo perché qualcuno guardava:

| emittente | l'elemento | cosa ha preso il modello | vero | falso |
|---|---|---|---|---|
| Nike | `operating_income` | «Gross profit» | 3,7 mld | **19,8 mld** |
| Walmart | `revenue` | «Net sales» | 681,0 mld | 674,5 mld |
| NVIDIA | `debt_maturity_*` | la scaletta dei **canoni di leasing** | non esiste | scaletta da 2,1 mld su 8,5 di debito |
| NVIDIA | `capex_ppe` + `capex_intangibles` | la stessa riga combinata, entrambi scartati | 3,2 mld | **nessun free cash flow** |
| Deere | `capex_intangibles` | «Cost of equipment on operating leases» | — | 2,9 mld di flotta del captive dentro il capex industriale |
| Walmart | `operating_da_total` + `depreciation` | la stessa riga, entrambi scartati | 13,0 mld | **nessun EBITDA** |

Ogni correzione è stata **reattiva**: ho scoperto il caso, ho affinato il
significato dell'elemento o aggiunto una guardia, ho rimappato. Il prossimo
emittente ne produrrà un altro. **Il compito non è correggere il settimo: è
capire se questo approccio può essere reso affidabile, o se va cambiato.**

Tre direzioni, e nessuna è stata provata:

1. **Il modello propone, un secondo controllo verifica.** Oggi il modello nomina
   una riga e il codice la legge. Nessuno chiede *"questa riga è davvero
   l'utile operativo?"* con la riga sotto gli occhi. Un secondo passaggio di
   verifica — un altro modello, o lo stesso con il compito invertito — è il
   pattern «propose-and-validate» che l'ecosistema usa altrove.
2. **Vincoli strutturali invece di significati in prosa.** Un utile operativo
   deve stare **sotto** il margine lordo e **sopra** l'utile ante imposte nello
   stesso prospetto. Nike sarebbe stata colta da questo, non da una frase
   migliore. La posizione della riga è già nella base (`StatementLine.section`,
   l'ordine) e non viene usata.
3. **Controlli di coerenza fra periodi.** La serie multiperiodo ha **già**
   trovato il caso Nike da sola: `operating_income` oscillava fra 6,7 e 22,9
   miliardi da un filing all'altro. Quel segnale esiste e nessuno lo usa come
   gate. È probabilmente il ritorno più alto per lo sforzo minore.

Una domanda che vale la pena porsi seriamente: **serve davvero un LLM per la
mappatura?** Per un emittente US i concetti XBRL sono già nel documento
(`StatementLine.concept`). Una tabella concetto→elemento, con l'LLM chiamato
solo dove la tabella non decide, sarebbe deterministica dove può esserlo. Non
l'ho provato. Il motivo per cui non ho cominciato di lì è documentato e reale
(Cisco: il fatto entity-wide dà 698 milioni contro un vero 1.653) — ma quello
riguarda le **API dei fatti**, non i concetti sulle righe renderizzate. La
distinzione non è mai stata testata.

---

## Dove sta tutto

- Worktree: `C:\Users\Administrator\Documents\GitHub\LazyTools-sec-retriever`
- Branch: `feat/sec-evidence-retriever` — **solo locale, mai pushato, nessuna PR**
- Ultimo commit: `d5d7b1e`
- Suite: **1169 passed, 8 skipped**

```bash
cd /c/Users/Administrator/Documents/GitHub/LazyTools-sec-retriever && PYTHONPATH="$PWD/src" /c/ProgramData/spyder-6/python -m pytest -q
```

L'interprete **deve** essere `/c/ProgramData/spyder-6/python` (il `python` nudo
è un 3.9 senza nulla installato).

### Modifiche non committate, e sono a metà

`src/lazytools/financials/normalised.py` e
`src/lazytools/connectors/edgar/normalise.py` hanno modifiche **non committate e
non verificate**: aggiungono `gross_profit` e `operating_expenses_total`,
irrigidiscono il significato di `operating_income`, portano `SCHEMA_VERSION` a 4
e derivano l'utile operativo quando il filer non lo presenta.

**La suite non è mai stata eseguita su queste modifiche.** Decidi tu se
completarle o buttarle (`git checkout -- src/`). Se le tieni: il bump di
`SCHEMA_VERSION` ritira ogni mappatura in cache, il che è voluto.

---

## Come è fatto

Due agenti, e vale la pena essere precisi perché io all'inizio non lo sono stato.

**Il recuperatore non è un agente.** È una pipeline deterministica con dentro
**una sola chiamata a un modello**. Ordine: risolvi emittente → scegli il
deposito (il più recente non successivo a `as_of`, che rende riproducibile la
corsa) → classifica (ontologia + segnali strutturali: `captive_finance`,
`rate_regulated`, `non_recourse`, `consolidated_vie`) → risolvi il periodo →
leggi i prospetti renderizzati → **una chiamata al modello** → risolvi i
riferimenti in valori → scala e segno → riconcilia → deriva → assegna gli stati.

**L'analista è un agente vero**: `LLMEngine` + **un solo tool**
(`load_instructions`) + il ciclo. Prompt statico = identità + regole sulle prove
+ **catalogo**; il resto si carica su richiesta.

### La regola che regge tutto

> **Il modello nomina una RIGA, mai una cifra. Non ne vede nessuna e non ne
> restituisce nessuna. Il codice legge ogni valore.**

Non negoziabile. Un modello che non emette mai un numero non può inventarne uno.
Non spostare la lettura dei valori dentro il modello per nessun motivo.

Ed è anche il **limite** della garanzia, che è il punto di partenza sopra:
impedisce le cifre inventate, non impedisce la riga sbagliata.

### File che contano

| file | cosa fa |
|---|---|
| `connectors/edgar/normalise.py` | l'orchestratore, un periodo |
| `connectors/edgar/series.py` | multiperiodo + rilevamento ristrutturazioni |
| `connectors/edgar/mapping.py` | l'unica chiamata al modello |
| `connectors/edgar/mapping_store.py` | cache per `(accession, SCHEMA_VERSION)` |
| `connectors/edgar/statements.py` | legge i prospetti renderizzati `R*.htm` |
| `connectors/edgar/ontology.py` | classificazione + segnali |
| `connectors/esef/facts.py` | parser xBRL-JSON europeo |
| `financials/normalised.py` | il contratto: elementi, stati, `SCHEMA_VERSION` |
| `financials/period.py` | aritmetica dei periodi |
| `skills/library/*.md` | **7 librerie di istruzioni, 46 blocchi** |

---

## Cosa è finito

- **US**: base normalizzata da un filing SEC, ~45 elementi, stati e provenienza.
- **Multiperiodo**: una mappatura per filing serve tutti gli anni; cammina
  all'indietro sovrapponendo di un anno; confronta i periodi condivisi.
- **ESEF**: parser dei fatti, verificato su TotalEnergies (646 fatti).
- **Analista**: libreria comune (12 blocchi) + 5 settori + scheda a 8 sezioni.
- **Recuperatore**: libreria a 9 blocchi, renderizzata nel prompt vivo.
- **Corpus agenzie**: 180 file, 12 agenzie, in `research/agency-methodology/`
  (gitignored). Sintesi in `FINDINGS.md`.

## Cosa manca

1. **Affidabilità dell'estrazione** — sopra. Prima di tutto.
2. **ESEF non è collegato**: il parser esiste, non c'è un percorso di
   normalizzazione che porti quei fatti alla base. Lì il modello dovrebbe
   nominare un **concetto**, non una riga.
3. **Nessun fallback IR.** Deliberatamente non scritto: l'utente aveva vietato
   fin dall'inizio di scrivere istruzioni su fonti da conoscenza generale, e il
   percorso IR va **misurato** prima. È l'unico senza difesa strutturale contro
   il contenuto inventato.
4. **Nessuna libreria semiconduttori** (NVIDIA è girata sul livello comune) né
   per i beni di consumo di marca (Nike: né retail né industrials la descrivono).
5. **Nessun rating**: il corpus delle agenzie non contiene alcuna taratura
   corporate. Non è una scelta di stile — è misurato, vedi `FINDINGS.md`.

---

## Trappole già pagate — non ripagarle

**Ambiente**
- Il TLS di Python **fallisce** su `data.sec.gov` (`ASN1: NOT_ENOUGH_DATA`) a
  ogni richiesta. `curl` con UA passa. Vale anche per FRED.
- La SEC vuole uno User-Agent dichiarato: `"Selva Zenone doctor.selva@gmail.com"`.
- `codex_write` va in **timeout a 300s** sui compiti grossi e non scrive niente.
  `codex_ask` con una domanda singola risponde. **La sandbox di Codex non ha
  rete**: tu scarichi, lui legge.
- `telegram_send_document` accetta solo file dentro `~/.lazytools/reports`
  (guardia anti-esfiltrazione, non aggirarla: copia il file lì).
- La console è cp1252: non stampare `−` (U+2212) dall'output di un modello.
- Niente Python-dentro-heredoc-dentro-Python. Usa lo strumento Write.

**Il crawler dell'ecosistema inventa.** In modalità smart fabbrica il contenuto
delle pagine JS-only: richiesta una rating action su `ZZQX Holdings` — società
inesistente, URL impossibile — ha risposto `status: done`,
`full_text_available: true` e una nota Fitch su Petrobras con dateline e
rapporti. **Usa modalità raw, mai LLM, per qualunque fonte esterna.**

**Dati SEC**
- I siti delle agenzie di rating sono SPA senza testo. Fitch è bloccato da policy
  nel browser. S&P dà 403.
- `list_filings` con `include_history=False` mostra **solo il blocco recente**:
  Walmart sembra avere 3 bilanci annuali.
- `is_primary_statement` non può fidarsi di `MenuCategory`: il filing Walmart
  2022 mette il conto economico sotto `Uncategorized`.
- Un filing porta 2-3 anni di colonne, ma **lo stato patrimoniale ne ha due e il
  conto economico tre**: risolvi la colonna **per prospetto**.
- ESEF: **entrambi gli estremi del periodo sono esclusivi, istantanei compresi**.
  Una cassa timbrata `2025-01-01` è il saldo del **31 dicembre 2024**.
- **GLEIF non è affidabile per nome**: «LVMH» risolve su «LVMH Publica», entità
  diversa che non deposita nulla. Il LEI è l'unica chiave ESEF.
- L'indice ESEF non copre Germania e Irlanda. Zero filing per SAP significa
  **copertura mancante**, non società che non deposita.

**Processo**
- Un test che passa non prova che la logica ci sia: tre volte ho scritto test che
  passavano senza esercitare quello che dichiaravano. Fai la **mutation test** —
  rompi apposta il codice e verifica che il test cada.
- `str.replace` senza `assert` fallisce in silenzio. Due volte.
- Non fidarti di Codex a scatola chiusa: ha smentito giustamente una mia
  affermazione assoluta, ma ha anche proposto di cancellare funzionalità che
  erano requisiti espliciti. Consultalo, poi ri-deriva.

---

## Come far girare qualcosa

```python
from datetime import date
from lazytools.connectors.edgar.client import EdgarClient
from lazytools.connectors.edgar.mapping_store import MappingStore
from lazytools.connectors.edgar.series import normalise_series
from lazytools.skills.credit import base_as_context, credit_analyst

client = EdgarClient("Selva Zenone doctor.selva@gmail.com")
store = MappingStore("mappings.sqlite")     # passalo sempre: la cache è gratis

series = normalise_series(client, company="NKE", years=5,
                          as_of=date(2025, 12, 1), store=store)
for label in ("revenue", "operating_income", "cfo"):
    print(label, series.value(label))

base = series.periods[-1]
note = credit_analyst(model="deepseek-v4-flash", sector="retail")(
    "Produce a credit assessment ...\n\n" + base_as_context(base)).text()
```

**`base_as_context` accetta una base sola.** Non esiste `series_as_context`:
l'analista non ha ancora modo di leggere una serie, ed è un pezzo che manca.

---

## Come lavora l'utente

- Italiano. Diretto. Vuole **concretezza e precisione**, non output vistosi.
- «rivedi sempre i piani iniziali e il codice per snellire e semplificare» —
  fatti aiutare da Codex per la review, ma **mantieni tu il controllo**.
- «mandami aggiornamenti dopo ogni milestone spiegando cosa hai implementato».
- «ripulisci da tutto quello che non serve, voglio codice pulito e no monkey patch».
- Vuole la **verifica dal vivo**, non la lettura del codice. Dichiara cosa resta
  non verificato.
- È l'unico utente dell'intero ecosistema Lazy*: fai pure modifiche
  incompatibili, niente shim di retrocompatibilità.
- Lavora in **più sessioni sugli stessi repo**. Non fare audit dei working tree
  altrui; metti al sicuro il tuo ramo e fermati.
