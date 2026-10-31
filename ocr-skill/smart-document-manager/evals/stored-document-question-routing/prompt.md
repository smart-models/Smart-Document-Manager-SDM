---
tags: [smart-document-manager]
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep]
---

Esercizio di sola pianificazione: non chiamare nessuno strumento MCP, API o comando. Due dettagli di archivio sono già stati recuperati con `ocr_get_storage_archive` e salvati qui: `small-archive-detail.json` (un report trimestrale di 3 pagine elaborato con `max_pages=2`) e `large-archive-detail.json` (un report di sostenibilità di 85 pagine).

Per ciascuna domanda scrivi il piano che eseguiresti: quali chiamate, in ordine, con quali `key` e parametri copiati dal dettaglio; quali file ti servono e perché; cosa NON scaricheresti e perché; come leggeresti ciò che torna. Se una domanda si risponde senza download, dillo. Se qualcosa non si può fare via MCP, dillo.

Q1 (archivio piccolo): "Il parse di questo documento è completo? Quando è stato fatto e con quale modello?"
Q2 (archivio piccolo): "Cosa mostra la Figura 2 di pagina 1? Dammi i valori del grafico."
Q3 (archivio grande): "Riassumi il report e dimmi quali depositi hanno la puntualità più bassa."

Input files: small-archive-detail.json, large-archive-detail.json
