# Driver mode — pilotare la pipeline da una sessione Claude

Dentro una sessione Claude i due modelli ci sono già: Claude **è** l'LLM
principale e tiene già aperta la connessione MCP a JEV. Far partire
`pipeline.run()` in un processo Python significherebbe aprire una seconda
connessione all'API e un secondo client MCP verso un server già collegato.

In driver mode la pipeline non chiama i modelli: **li chiede**. Si ferma a ogni
chiamata, dice cosa serve, e riprende quando arriva la risposta. Claude fa le
due cose che già sa fare — invocare `typesafe-jev` e generare testo — e Python
fa tutto il resto: contratto, lint, espansione `for_each`/`criteria_from`,
policy, conteggi, controlli di lunghezza e lingua, sintesi.

## Come funziona

Ogni stadio fra due chiamate ai modelli è Python puro, quindi la corsa è
**interamente determinata dalla sequenza di risposte**. Il driver registra le
risposte e riesegue `run()` dall'inizio ogni volta, riservendo quelle
registrate; quando la pipeline ne chiede una non ancora registrata, il client di
replay solleva `Pending` e il driver riporta la chiamata.

Niente generatori da congelare, niente macchina a stati da tenere allineata: lo
stato è un file JSON che sopravvive fra un turno e l'altro, fra processi e fra
macchine. Il costo è rieseguire qualche microsecondo di Python a ogni passo.

Ogni chiamata porta un `fingerprint` (hash di state + domande, o di system +
user). Se si modifica la richiesta o gli input a metà corsa, il replay se ne
accorge e si ferma, invece di servire silenziosamente risposte sbagliate.

## Prima di tutto: verificare lo schema del server

La forma delle risposte dipende dal server MCP davanti a JEV. Una volta sola,
per installazione:

```bash
jev-drive probe                                   # -> una chiamata con le tre primitive
echo '<risposta grezza>' | jev-drive probe --check -
```

Il check dice cosa il parser ha letto e segnala ciò che si romperà: un Noul con
polarità invertita, uno Score senza livelli frazionari (gate v3 cieco sotto la
soglia 1,75), una Choice o uno Score senza confidence (escalation mai
raggiungibile), un'opzione fuori dalla tassonomia. Esce con 1 se c'è almeno un
avviso.

## Il ciclo

```bash
jev-drive init   --state run.json --request "..." --inputs inputs.json
jev-drive next   --state run.json                    # -> la chiamata da fare
jev-drive submit --state run.json --result -         # <- la sua risposta
# ...finché 'next' stampa il risultato invece di una chiamata...
jev-drive result --state run.json
```

`init` e `submit` stampano già la chiamata successiva, quindi `next` serve solo
per riorientarsi.

## Cosa stampa il driver

```json
{
  "status": "pending",
  "call": {
    "kind": "jev",
    "index": 0,
    "fingerprint": "0f5a30a7ee1c",
    "purpose": "gate",
    "state": {"request": "..."},
    "questions": [ {"id": "shape", "type": "choice", "criteria": [...]}, ... ],
    "expects": {
      "shape": "{\"option\": \"<option id>\", \"confidence\": <0-1>}",
      "g1": "a probability in [0,1] — \"yes\" is the high value, no confidence"
    }
  }
}
```

`kind` dice chi deve rispondere:

- `"jev"` → una chiamata `typesafe-jev`: uno state, molte domande.
- `"llm"` → una generazione. `purpose` distingue i due casi: `compile the plan`
  (il campo `system` contiene il prompt compilatore v0.4, e la risposta deve
  essere **solo** l'oggetto JSON del piano) e `generate the text` (`system` è
  vuoto e la risposta è il testo per l'utente).

`expects` descrive, per ogni domanda, la forma della risposta attesa.

## Rispondere a una chiamata JEV

Il parser è volutamente tollerante, perché la forma sul filo dipende dal server
MCP davanti a JEV. Tutte queste funzionano, per id di domanda:

```json
{"q1": 0.87}
{"q1": {"probability": 0.87}}
{"q2": {"score": 2.17, "confidence": 0.88}}
{"q3": {"option": "price", "confidence": 0.91}}
[{"id": "q1", "probability": 0.87}]
{"answers": {"q1": 0.87}}
```

Tre punti da rispettare:

1. **Un Noul risponde con una sola probabilità**, senza confidence: "sì" è
   sempre il valore alto. La banda 0,4–0,6 viene trattata come incerta dal
   runtime e finisce in sintesi come "da verificare".
2. **Uno Score risponde con il livello atteso, che può essere frazionario.**
   La soglia `cost` del gate v3 (1,75) confronta proprio quel valore. Se
   `typesafe-jev` restituisce solo il livello discreto, il gate v3 perde
   risoluzione: il v2 non ne risente.
3. **Una Choice risponde con l'id dell'opzione** più la confidence.

Se `typesafe-jev` restituisce una forma diversa da tutte queste, il punto in cui
adattarla è `parse_answers` in `src/jev/driver.py`: è l'unico posto che tocca il
formato del server.

## Costo di una corsa

Su un caso `pre+post` completo, 14 chiamate: 1 gate, 1 compilatore, 8 lint,
1 round, 1 generazione, 1 verifica post, 1 coerenza.

Il lint da solo è 8 chiamate perché ogni check ha uno state diverso, e la
specifica impone una `jev_ask` per state. È consultivo per progetto, quindi si
può spegnere:

```bash
jev-drive init --state run.json --request "..." --lint code
```

Restano 6 chiamate. I controlli in codice — L1–L5 e L10, quelli che **bloccano**
— girano sempre: `--lint code` toglie solo i check semantici (L5 via JEV, L6–L9,
L11–L13).

## Se una risposta era sbagliata

```bash
jev-drive rewind --state run.json --count 1
```

Toglie le ultime risposte e riporta la corsa al punto giusto. Utile quando una
chiamata a `typesafe-jev` è andata male o quando il compilatore ha prodotto un
piano da rifare.

## Usare il driver da Python invece che dalla CLI

```python
from jev.driver import Driver, Transcript
from jev.pipeline import PipelineResult

driver = Driver(Transcript(request=richiesta, inputs={"customer_message": msg}))
step = driver.step()
while not isinstance(step, PipelineResult):
    risposta = esegui(step)          # il tuo MCP, o il tuo modello
    step = driver.submit(risposta, step.fingerprint)
print(step.text)
```
