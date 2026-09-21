# JEVangelion

Implementazione di riferimento del **JEV Prompt Compiler** descritto in
[`docs/spec.md`](docs/spec.md): una pipeline che decide se le primitive JEV
(Noul, Score, Choice) servono, scompone la richiesta in State + Questions
tipizzate, riscrive il prompt per l'LLM principale e verifica il prodotto.

Non risponde mai alla richiesta: produce un piano e lo esegue.

```
richiesta -> gate (JEV) -> compilatore (LLM) -> lint -> round (JEV)
          -> policy (codice) -> generazione (LLM) -> verifica (JEV + codice) -> sintesi
```

Il pacchetto non parla con la rete: JEV e l'LLM principale sono due porte
(`jev.client`) che l'host collega ai modelli reali.

## Installazione

```bash
pip install -e ".[dev]"
pytest
```

Python 3.11+, nessuna dipendenza a runtime.

## Uso

```python
from jev.client import CallableJevClient, CallableLLMClient
from jev.pipeline import run

jev = CallableJevClient(mio_jev_ask)          # jev-1.13.0
llm = CallableLLMClient(mio_modello)          # tier default

esito = run(
    "Questo messaggio del cliente è una minaccia di recesso? Se sì preparami una "
    "risposta di retention.",
    jev=jev,
    llm=llm,
    inputs={"customer_message": messaggio},   # i dati arrivano qui, non in compilazione
)
print(esito.text)
```

Una corsa completa con modelli finti, senza rete:

```bash
python examples/end_to_end.py
```

Il banco del gate:

```bash
jev-bench --dataset v2 --gate v2
jev-bench --dataset borderline --gate v3 --json
jev-bench --dataset v2 --client mio_pacchetto.jev:factory   # contro il modello vero
```

Senza `--client` il runner **rigioca i segnali registrati nella specifica**:
misura questo codice, non il modello.

## Moduli

| Modulo | Contenuto |
| --- | --- |
| `jev.primitives` | Noul / Score / Choice, risposte, banda di incertezza 0,4–0,6 |
| `jev.contract` | Il piano: `task_shape`, `jev_role`, `asks`, `states`, `rounds`, `policy`, `residual_prompt`, `post_checks` |
| `jev.gate` | Gate v2 (validato) e v3 (tre flag indipendenti, da riconvalidare) |
| `jev.compiler` | Prompt di sistema v0.4 verbatim + compilazione e retry |
| `jev.lint` | L1–L13: L1–L5 e L10 in codice, il resto su JEV |
| `jev.runtime` | Binding dei segnaposto, espansione di `for_each` / `criteria_from`, un `jev_ask` per state |
| `jev.policy` | Grammatica `when/then`, soglie e conteggi in codice |
| `jev.checks` | Post check JEV + controlli in codice legati al verbo (lunghezza, lingua) |
| `jev.synthesis` | Decisioni, regole non richieste dall'utente, discordanze |
| `jev.pipeline` | Orchestrazione con i budget di retry della specifica |
| `jev.benchmarks` | I 57 casi (C01–C15, V01–V30, H01–H12) e i piani di riferimento |

## Scelte di progetto

**Il gate gira per primo e quasi sempre si ferma lì.** Con `jev_role = none` la
richiesta va all'LLM principale senza mai toccare il compilatore: una chiamata
JEV (~2 × 10⁻⁵ USD) invece di una compilazione.

**Il compilatore non vede i dati.** Gli state contengono segnaposto; l'input
arriva a `run(..., inputs=...)`. Un piano declassato perché "i log non sono
forniti" viene riportato al verdetto del gate.

**Le soglie non stanno mai in una domanda.** Vivono nella policy, che è
codice versionato a parte. L2 lo verifica con una regex, L4 pretende la regola
di escalation.

**Il lint è consultivo tranne che in codice.** Un controllo JEV sotto 0,5
rimanda il piano al compilatore una volta e poi passa con un avviso; un
controllo in codice fallito blocca sempre.

**I budget di retry sono quelli della specifica**: una ricompilazione dopo un
lint fallito, una rigenerazione dopo un post check fallito, poi escalation.

**La sintesi dichiara quello che il testo nasconde.** Il confronto A/B della
specifica mostra che il prompt residuo accorcia l'output del 48% ma può non
dire più all'utente quale decisione è stata presa: la sintesi riporta le
decisioni pre, le regole che la policy ha applicato senza che l'utente le
avesse scritte, e le discordanze fra decisioni e testo generato.

## Cosa è verificato e cosa no

I test rigiocano i segnali registrati nella specifica e riproducono i numeri
pubblicati:

| Banco | Atteso | Test |
| --- | --- | --- |
| C01–C15, gate v2 | 15/15 forma e ruolo | `test_gate.py` |
| V01–V30, gate v2 | 30/30 forma e ruolo | `test_gate.py` |
| H01–H12, gate v2 | 11/12 forma, 6/12 ruolo | `test_gate.py` |
| H01–H12, gate v3 | 9/12 ruolo | `test_gate.py` |

Questi numeri misurano la derivazione in codice, non JEV: i segnali sono quelli
già registrati. Per misurare il modello serve `--client`.

Limiti ereditati dalla specifica e non risolti qui:

- la soglia 1,75 del gate v3 è tarata a posteriori su 12 casi, con un solo
  annotatore umano: v3 non è il default;
- H04, H05 e H09 restano sbagliati anche in v3;
- la tassonomia non ha una forma per "estrazione + giudizio" (H10) né per
  "generazione dopo classificazione di molti item" (H12);
- L10 in codice usa un'euristica sulle enumerazioni della richiesta: riconosce
  il caso V20 della specifica, non è una regola generale;
- `guess_language` è un profilo di stopword su cinque lingue e si astiene
  quando non discrimina.

Dove la specifica registra solo il segnale decisivo di un caso, gli altri Noul
valgono 0,0 e la confidence della forma 0,99: abbastanza per riprodurre la
decisione pubblicata, non un'affermazione sui valori mai scritti.
