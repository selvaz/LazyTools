# Diagnosi: `financial_investigator` «non restituisce nulla»

Sessione Claude Code parallela, 2026-09-01. **Seconda versione: la prima era
sbagliata** e indicava l'encoding. Il guasto è stato riprodotto e la causa è
un'altra. Nessun vostro file è stato modificato.

---

## Il guasto è reale ed è riprodotto

Lo **stesso identico script**, stesso interprete, stesso `PYTHONPATH`, stesso
`PYTHONIOENCODING=utf-8`, risultato scritto su file e mai stampato:

| eseguito da | risultato |
|---|---|
| sessione Claude Code | `'READY'` |
| **sessione Codex** | `''` — stringa vuota |

E un turno banale («rispondi READY, non usare tool») lanciato da dentro Codex è
poi andato in **timeout a 300 secondi**. Dalla mia sessione impiega secondi.

La differenza non è il codice: è **chi ospita il processo**.

## La causa

`financial_investigator` costruisce un `CodexEngine`, che per rispondere deve
lanciare un `codex app-server` come sottoprocesso e parlargli. Quando
l'investigatore gira **dentro una sessione Codex**, quel server annidato nasce
nella sandbox di Codex — e quella sandbox **non ha rete in uscita**.

Prova diretta, dalla stessa sandbox, oggi: a Codex era stato chiesto di
scaricare documenti da `sec.gov` e ha riferito che *«curl ha raggiunto il DNS
della SEC ma l'HTTPS in uscita è stato rifiutato con `Bad access`»*, e che anche
il runtime HTTP alternativo era bloccato.

Un `codex app-server` annidato senza rete non raggiunge il modello. Il turno non
produce nulla, e quel nulla arriva come stringa vuota.

## Cosa NON è la causa — smentito, non supposto

- **Non è l'encoding.** L'utente ha forzato la formattazione e il guasto resta.
  E il confronto decisivo (`'READY'` contro `''`) è stato fatto con gli stessi
  identici parametri di encoding da entrambe le parti, scrivendo su file.
  *La mia prima diagnosi era questa ed era sbagliata.*
- **Non è il trasporto MCP.** `mcp.server.stdio.stdio_server()` avvolge
  `sys.stdout.buffer` in un `TextIOWrapper(encoding="utf-8")` esplicito, e il
  tubo verso l'app-server usa `.encode()`, che senza argomento è UTF-8.
- **Non è web + tool dinamici.** Verificato dal vivo: `CodexEngine` con ricerca
  web nativa **e** tool dinamici insieme risponde correttamente, e
  l'investigatore completo ha svolto una ricerca web reale, chiamato
  `save_evidence` e persistito `issuer:NKE:evidence:fy_end`.

## Un secondo difetto, indipendente e reale

`lazytools` si risolve sull'editable install in **`LazyTools-mcp-deploy`**, che
**non contiene** `skills/financial_investigator.py`. Senza un `PYTHONPATH`
esplicito sul worktree `LazyTools-sec-retriever/src`, l'import fallisce:

```
ModuleNotFoundError: No module named 'lazytools.skills.financial_investigator'
```

E vince anche avendo il worktree sec-retriever come `cwd`. Confermato dalla
stessa sessione Codex. Non è la causa del `''` (con `PYTHONPATH` corretto
l'import riesce e il risultato resta vuoto), ma è un secondo modo indipendente
di ottenere «non funziona».

## Cosa fare

**Non eseguire un agente `CodexEngine` da dentro una sessione Codex.** È un
Codex che ne lancia un altro dentro una sandbox senza rete. Lanciatelo da un
processo che la rete ce l'ha: un terminale normale, il server MCP, o una
sessione Claude. Da lì funziona — l'ho eseguito e risponde.

Sempre con `PYTHONPATH` sul worktree finché l'investigatore non arriva nel
worktree di deploy che serve l'editable install.

## Quello che resta aperto, e va chiuso

Non ho potuto stabilire **se LazyBridge segnali il fallimento o lo nasconda**: il
turno che doveva catturare `result.error` insieme al testo è andato in timeout
prima di scrivere. La domanda è precisa e vale la pena rispondere:

> quando il turno annidato non produce nulla, `envelope.error` è valorizzato,
> oppure resta `None` accanto a un testo vuoto?

Se è `None`, è un difetto di LazyBridge a sé stante: un turno fallito che si
presenta come riuscito. È esattamente ciò che ha fatto sembrare il guasto un
mistero invece di un errore di rete.
