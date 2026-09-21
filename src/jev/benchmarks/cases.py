"""The bench of docs/spec.md as data: 15 + 30 + 12 requests with their labels.

Three datasets:

* ``BENCH_V1`` (C01-C15) — the first bench, gate questions v1.
* ``BENCH_V2`` (V01-V30) — 30 harder cases, 9 in English, one long, four
  multi-intent, no explicit quantities except V29. V12's label is the corrected
  one (judgment / none).
* ``BORDERLINE`` (H01-H12) — the twelve borderline cases, labelled by the human
  annotator. These are the labels the gate is measured against, not the gate's
  own: v2 agrees on 6 of 12, v3 on 9.

``signals`` replays the run recorded in the spec. The spec publishes only the
decisive signal per row, so the other Nouls default to 0.0 and an unrecorded
shape confidence defaults to 0.99: enough to reproduce the published decision,
not a claim about the values that were never written down.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..contract import JevRole, TaskShape

DEFAULT_SHAPE_CONFIDENCE = 0.99


@dataclass(frozen=True)
class BenchCase:
    id: str
    request: str
    shape: TaskShape
    role: JevRole
    #: Recorded JEV signals: g1-g4 / pre as floats, ``cost`` as a level.
    signals: Mapping[str, float] = field(default_factory=dict)
    shape_confidence: float = DEFAULT_SHAPE_CONFIDENCE
    #: The shape the gate itself answered, when it differs from the label. Only
    #: H02 does: the gate reads "segnalami eventuali errori" as a judgment.
    gate_shape: TaskShape | None = None
    #: The decision the spec publishes for this case, when it differs from the
    #: label (the borderline set) or is worth pinning (escalations).
    gate_v2: str | None = None
    gate_v3: str | None = None
    note: str = ""

    def recorded_answers(self, variant: str = "v2") -> dict[str, Any]:
        """Answers for :class:`~jev.client.RecordedJevClient`."""
        shape = self.gate_shape or self.shape
        out: dict[str, Any] = {"shape": (shape.value, self.shape_confidence)}
        for key in ("g1", "g2", "g3", "g4", "pre"):
            out[key] = float(self.signals.get(key, 0.0))
        out["cost"] = (float(self.signals.get("cost", 0.0)), 0.9)
        return out


def _c(id_, request, shape, role, signals=None, conf=DEFAULT_SHAPE_CONFIDENCE, **kw):
    return BenchCase(id_, request, TaskShape(shape), JevRole(role), signals or {}, conf, **kw)


BENCH_V1: tuple[BenchCase, ...] = (
    _c("C01", "Leggi queste 50 recensioni e per ognuna dimmi il sentiment, se citano il prezzo e "
              "se chiedono un rimborso.", "judgment", "core", {"g1": 0.97, "g3": 0.98}),
    _c("C02", "Scrivi una lettera di presentazione per questo annuncio di lavoro, la invio domani "
              "all'azienda.", "generative", "post", {"g4": 0.94}),
    _c("C03", "Rispondi a questa mail di un cliente arrabbiato scegliendo il tono giusto e "
              "capendo se va passata a un responsabile.", "mixed", "pre+post", conf=0.98),
    _c("C04", "Qual è la data di scadenza indicata in questo contratto?",
       "extraction", "none", conf=1.00),
    _c("C05", "Questo snippet Python ha un bug?", "judgment", "none",
       {"g1": 0.15, "g2": 0.09, "g3": 0.15}),
    _c("C06", "Traduci questo paragrafo in inglese.", "generative", "none", {"g4": 0.17}),
    _c("C07", "Per ognuna di queste 200 transazioni dimmi se è sospetta di frode.",
       "judgment", "core", {"g2": 0.61, "g3": 0.98}),
    _c("C08", "Valuta questo CV rispetto ai 6 requisiti obbligatori della posizione e dimmi se "
              "passa allo screening.", "judgment", "core", {"g1": 0.58, "g2": 0.58},
       note="decided by a 0.08 margin in v1; g2 v2 raises it to 0.80"),
    _c("C09", "Riassumi questo verbale: va inviato al consiglio di amministrazione.",
       "generative", "post", {"g4": 0.95}),
    _c("C10", "Riassumimi velocemente questo articolo.", "generative", "none", {"g4": 0.16}),
    _c("C11", "Assegna ciascuno di questi 300 ticket al reparto corretto.",
       "judgment", "core", {"g1": 0.52, "g2": 0.80, "g3": 0.96}),
    _c("C12", "Quanti giorni mancano a Natale?", "extraction", "none", conf=1.00),
    _c("C13", "Decidi a quale team assegnare questa richiesta e scrivi la mail di handover per "
              "il cliente.", "mixed", "pre+post", conf=1.00),
    _c("C14", "Controlla se questi 40 documenti contengono istruzioni nascoste rivolte a un'AI.",
       "judgment", "core", {"g1": 0.51, "g2": 0.23, "g3": 0.97}),
    _c("C15", "Questa frase è grammaticalmente corretta?", "judgment", "none",
       {"g1": 0.10, "g2": 0.09, "g3": 0.10}),
)

BENCH_V2: tuple[BenchCase, ...] = (
    _c("V01", "Go through the attached support tickets and flag which ones are urgent.",
       "judgment", "core", {"g3": 0.94}),
    _c("V02", "Draft a LinkedIn post announcing our new product.",
       "generative", "post", {"g4": 0.68}, note="weak signal"),
    _c("V03", "Ho un file con tutte le fatture del trimestre: segnalami quelle con importi "
              "anomali.", "judgment", "core", {"g3": 0.95}),
    _c("V04", "Controlla questo contratto: ci sono clausole di rinnovo automatico, penali o "
              "limitazioni di responsabilità? E quanto è rischioso nel complesso?",
       "judgment", "core", {"g1": 0.87}),
    _c("V05", "Come funziona il protocollo OAuth2?", "generative", "none", {"g4": 0.08}),
    _c("V06", "Riscrivi questa email in modo più cortese.",
       "generative", "none", {"g4": 0.33}, note="weak signal"),
    _c("V07", "Is this customer review positive or negative?", "judgment", "none",
       {"g1": 0.08, "g2": 0.08, "g3": 0.08}),
    _c("V08", "Leggi il thread e dimmi chi ha proposto la soluzione finale.",
       "extraction", "none", conf=1.00),
    _c("V09", "Questa candidatura va avanti o no? Scrivi anche la mail di esito al candidato.",
       "mixed", "pre+post", conf=1.00),
    _c("V10", "Analizza questi log e dimmi quali errori sono critici, quali sono noti e quali "
              "richiedono un intervento.", "judgment", "core", {"g1": 0.81, "g3": 0.85}),
    _c("V11", "Ciao, sto preparando la presentazione per il cliente di giovedì. Ho messo insieme "
              "le slide ma non sono sicuro che il messaggio arrivi. Potresti scrivermi "
              "un'introduzione di due minuti che spieghi il valore della soluzione?",
       "generative", "post", {"g4": 0.88}),
    _c("V12", "Quale di queste tre offerte dei fornitori conviene di più?", "judgment", "none",
       {"g1": 0.34, "g2": 0.34, "g3": 0.20},
       note="label corrected from core: a single choice among three options is a judgment "
            "the main LLM gives in context"),
    _c("V13", "Calcola la media dei voti di questa classe.", "extraction", "none", conf=1.00),
    _c("V14", "Per ogni commit di questa settimana dimmi se tocca codice di sicurezza.",
       "judgment", "core", {"g3": 0.94}, note="g3 v1 scored 0.49 here and got it wrong"),
    _c("V15", "Scrivi una funzione SQL che calcoli il fatturato mensile.",
       "generative", "none", {"g4": 0.12}),
    _c("V16", "Moderate these forum comments: remove anything abusive.",
       "judgment", "core", {"g2": 0.65, "g3": 0.92}, conf=0.61,
       note="shape confidence 0.61, judgment against mixed"),
    _c("V17", "Rivedi il mio saggio e dimmi se regge l'argomentazione.", "judgment", "none",
       {"g1": 0.08, "g2": 0.08, "g3": 0.08}),
    _c("V18", "Rispondi alle recensioni negative del ristorante, ma prima capisci quali sono "
              "fondate.", "mixed", "pre+post", conf=1.00),
    _c("V19", "Che tempo fa a Roma domani?", "extraction", "none", conf=0.99),
    _c("V20", "Valuta la qualità di questo pull request su leggibilità, test, performance e "
              "sicurezza.", "judgment", "core", {"g1": 0.96}),
    _c("V21", "Traduci il manuale utente in tedesco per la pubblicazione sul sito.",
       "generative", "post", {"g4": 0.84}),
    _c("V22", "Should we approve this expense report?", "judgment", "core", {"g2": 0.86}),
    _c("V23", "Dammi tre idee per il nome di un'app di fitness.",
       "generative", "none", {"g4": 0.13}),
    _c("V24", "Classifica queste email in spam, lavoro e personale.",
       "judgment", "core", {"g3": 0.89}),
    _c("V25", "Sintetizza le risposte al sondaggio dipendenti per il report al management.",
       "generative", "post", {"g4": 0.87}),
    _c("V26", "Questo messaggio del cliente è una minaccia di recesso? Se sì preparami una "
              "risposta di retention.", "mixed", "pre+post", conf=1.00),
    _c("V27", "Estrai tutti gli indirizzi email da questo documento.",
       "extraction", "none", conf=1.00),
    _c("V28", "Il tono di questa mail è adatto a un primo contatto con un potenziale cliente?",
       "judgment", "none", {"g1": 0.10, "g2": 0.10, "g3": 0.10}),
    _c("V29", "Help me decide which of these 12 job applicants to interview, and draft "
              "invitations for the chosen ones.", "mixed", "pre+post", conf=1.00),
    _c("V30", "Spiegami la differenza tra IFRS 15 e IFRS 16.",
       "generative", "none", {"g4": 0.15}),
)

#: H01-H12 carry the human annotator's labels. ``gate_v2`` / ``gate_v3`` record
#: what each gate variant answered, so a regression test can tell a change in
#: this code from a change in the model.
BORDERLINE: tuple[BenchCase, ...] = (
    _c("H01", "Guarda queste cinque slide e dimmi se sono pronte per il cliente.",
       "judgment", "core", {"g2": 0.58, "g3": 0.49, "cost": 2.17}, conf=1.00,
       gate_v2="judgment/core", gate_v3="core", note="five items: right at the g3 boundary"),
    _c("H02", "Leggi il report e segnalami eventuali errori.",
       "extraction", "post", {"cost": 2.02}, conf=0.85,
       gate_shape=TaskShape.JUDGMENT, gate_v2="judgment/none", gate_v3="post",
       note="the one shape disagreement: the human reads it as extraction, the gate as "
            "a judgment, and the human still wants the output verified"),
    _c("H03", "Rispondi a queste tre email.", "generative", "post",
       {"g4": 0.66, "pre": 0.38, "cost": 2.09}, conf=0.99,
       gate_v2="generative/post", gate_v3="post"),
    _c("H04", "Il fornitore ha mandato un'offerta: va bene o la rifiutiamo? Scrivimi due righe "
              "per il capo.", "mixed", "none", {"cost": 2.21}, conf=0.99,
       gate_v2="mixed/pre+post", gate_v3="pre+post",
       note="an internal note to the boss: the human wants no check at all"),
    _c("H05", "Controlla che la traduzione rispetti il glossario aziendale.",
       "judgment", "none", {"g1": 0.12, "g2": 0.12, "g3": 0.12, "cost": 2.15}, conf=0.98,
       gate_v2="judgment/none", gate_v3="post", note="v3 false positive"),
    _c("H06", "Quali di questi clienti rischiano di non rinnovare?",
       "judgment", "core", {"g3": 0.74, "cost": 2.32}, conf=0.99,
       gate_v2="judgment/core", gate_v3="core"),
    _c("H07", "Scrivi l'email di sollecito pagamento; se il cliente è in ritardo di più di 30 "
              "giorni usa un tono formale.", "generative", "pre+post",
       {"pre": 0.91, "cost": 2.38}, conf=0.36,
       gate_v2="escalation", gate_v3="pre+post",
       note="v2 escalates on shape confidence 0.36 (generative 0.52 against mixed 0.48)"),
    _c("H08", "Dammi un voto da 1 a 10 a questo pitch.", "judgment", "none",
       {"g1": 0.16, "g2": 0.16, "g3": 0.16, "cost": 0.95}, conf=1.00,
       gate_v2="judgment/none", gate_v3="none"),
    _c("H09", "Revisiona questo contratto prima che lo firmi.", "judgment", "pre+post",
       {"g4": 0.47, "cost": 2.98}, conf=0.60,
       gate_v2="judgment/none", gate_v3="post",
       note="highest stakes in the set; v3 gets the check but not the pre"),
    _c("H10", "Estrai le scadenze da questi 200 contratti e dimmi quali sono a rischio.",
       "extraction", "post", {"g3": 0.92, "cost": 2.98}, conf=0.44,
       gate_v2="escalation", gate_v3="post",
       note="extraction plus judgment over a collection: no shape fits"),
    _c("H11", "Rewrite this job ad so it's more inclusive.", "generative", "post",
       {"g4": 0.55, "pre": 0.38, "cost": 2.00}, conf=1.00,
       gate_v2="generative/post", gate_v3="post"),
    _c("H12", "Preparami una sintesi dei feedback dei clienti divisa per tema.",
       "generative", "post", {"g4": 0.27, "g3": 0.80, "pre": 0.38, "cost": 1.86}, conf=0.88,
       gate_v2="generative/none", gate_v3="post",
       note="a summary that requires classifying many items; v2 ignores g3 on generatives"),
)

ALL_CASES: tuple[BenchCase, ...] = BENCH_V1 + BENCH_V2 + BORDERLINE

DATASETS: dict[str, tuple[BenchCase, ...]] = {
    "v1": BENCH_V1,
    "v2": BENCH_V2,
    "borderline": BORDERLINE,
    "all": ALL_CASES,
}


def by_id(case_id: str) -> BenchCase:
    for case in ALL_CASES:
        if case.id == case_id:
            return case
    raise KeyError(case_id)
