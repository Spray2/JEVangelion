---
name: jev-drive
description: Esegue la pipeline JEV di questo repo pilotando `jev-drive` e rispondendo alle sue chiamate con il tool MCP typesafe-jev e con le proprie generazioni. Usa questa skill quando l'utente chiede di provare la pipeline su una richiesta reale, di testare il gate o il compilatore contro JEV, di far girare un caso del banco end-to-end, o dice "esegui con jev", "prova la pipeline", "driver mode". NON usarla per modificare il codice del repo o per far girare i test, che sono `pytest`.
---

# Pilotare la pipeline JEV

Questo repo implementa il JEV Prompt Compiler (`docs/spec.md`). In driver mode
la pipeline non chiama i modelli: si ferma a ogni chiamata, dice cosa serve, e
riprende con la risposta. Tu sei i due modelli — l'MCP `typesafe-jev` per le
chiamate JEV, te stesso per le generazioni.

Il protocollo completo è in `docs/driver.md`. Leggilo se qualcosa non torna.

## Prima volta: verifica lo schema del server

Fallo una volta sola per installazione, prima di qualunque corsa:

```bash
jev-drive probe
```

Manda `state` e `questions` a `typesafe-jev` in una sola chiamata, poi:

```bash
echo '<la risposta grezza del tool>' | jev-drive probe --check -
```

Se stampa warning, **riportali all'utente prima di proseguire**: dicono cosa si
romperà (gate v3 senza livelli frazionari, escalation senza confidence). Non
aggirarli inventando valori.

## Il ciclo

```bash
jev-drive init --state run.json --request "<la richiesta dell'utente, verbatim>" \
               [--inputs inputs.json] [--lint code]
```

`--inputs` è un oggetto JSON che lega i segnaposto del piano ai dati veri
(`{"customer_message": "..."}`). Senza dati a runtime la corsa si ferma quando
il piano li chiede.

Poi ripeti finché lo stato non è `done`:

1. Leggi il JSON stampato. Se `status` è `done` hai finito.
2. Guarda `call.kind`:
   - **`jev`** → una chiamata a `typesafe-jev`: passa `call.state` come stato e
     `call.questions` come domande, in **una sola** chiamata. `call.expects` dice
     la forma di risposta attesa per ogni domanda.
   - **`llm`** → generi tu. Se `call.purpose` è `compile the plan`, `call.system`
     contiene il prompt compilatore: rispondi con **solo** l'oggetto JSON del
     piano, senza commenti né recinti di codice. Se è `generate the text`,
     `call.system` è vuoto e rispondi con il testo per l'utente e basta.
3. Reimmetti la risposta:

```bash
echo '<risposta>' | jev-drive submit --state run.json --result - \
                                     --fingerprint <call.fingerprint>
```

Passa sempre `--fingerprint`: è la garanzia che stai rispondendo alla chiamata
giusta.

4. Alla fine: `jev-drive result --state run.json`.

## Regole

- **Non rispondere tu al posto di JEV.** Le chiamate `kind: "jev"` vanno al tool
  MCP. Inventare le probabilità rende la corsa priva di significato, ed è
  esattamente ciò che il banco serve a misurare.
- **Non modificare la richiesta né gli input a metà corsa.** Il driver se ne
  accorge dal fingerprint e si ferma. Se devono cambiare, ricomincia da `init`.
- **Se una risposta era sbagliata**, `jev-drive rewind --state run.json` toglie
  l'ultima e riporta indietro la corsa. Non modificare il file di stato a mano.
- **Un Noul risponde con una sola probabilità**, senza confidence: "sì" è il
  valore alto. Uno Score risponde con il livello atteso, che può essere
  frazionario. Una Choice risponde con l'id dell'opzione più la confidence.
- **Il lint pieno costa 8 chiamate su 14.** Se l'utente vuole solo vedere la
  pipeline girare, proponi `--lint code`: restano 6 chiamate e i controlli
  bloccanti girano comunque.

## Cosa riportare all'utente

Alla fine mostra il testo di `jev-drive result` e, in due righe:

- forma e ruolo decisi dal gate, con il segnale che li ha decisi;
- quanti check del lint sono falliti, e quali;
- se c'è stata escalation e perché;
- quante chiamate JEV e quante LLM sono servite.

Se il gate ha deciso `none`, dillo esplicitamente: significa che la pipeline ha
concluso che JEV non serviva per quella richiesta, ed è un esito corretto, non
un fallimento.
