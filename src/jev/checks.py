"""Verification of the generated output: JEV post checks plus code checks.

Some verifications must not be left to the plan. "Riassumi" implies a text
shorter than the original, and the end-to-end run in docs/spec.md shows four
post checks passing at 0.79-0.99 on a "summary" 25% longer than the minutes it
summarised. Length and language are ratios and token profiles, so they are
measured here, in code, keyed off the verb of the request.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Sequence

from .client import JevClient
from .contract import Plan, PostCheck
from .primitives import Answer, Question, QuestionType

POST_CHECK_THRESHOLD = 0.5

#: One regeneration, then escalate. Never a second silent retry.
MAX_REGENERATIONS = 1


@dataclass(frozen=True)
class CheckOutcome:
    id: str
    passed: bool
    detail: str
    score: float | None = None
    where: str = "jev"


@dataclass
class VerificationReport:
    outcomes: list[CheckOutcome] = field(default_factory=list)
    attempts: int = 1

    @property
    def failures(self) -> list[CheckOutcome]:
        return [o for o in self.outcomes if not o.passed]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def escalate(self) -> bool:
        return bool(self.failures) and self.attempts > MAX_REGENERATIONS


def run_post_checks(
    jev: JevClient, checks: Sequence[PostCheck], output: str, brief: str
) -> list[CheckOutcome]:
    """One ``jev_ask``: state is the generated output plus the original brief."""
    if not checks:
        return []
    questions = [
        Question(id=c.id, type=QuestionType.NOUL, instructions=c.instructions) for c in checks
    ]
    answers = jev.ask({"output": output, "brief": brief}, questions)
    outcomes: list[CheckOutcome] = []
    for c in checks:
        answer: Answer | None = answers.get(c.id)
        value = float(answer.probability or 0.0) if answer else 0.0
        outcomes.append(
            CheckOutcome(c.id, value >= POST_CHECK_THRESHOLD, c.instructions, score=value)
        )
    return outcomes


# -- code checks tied to the verb of the request ---------------------------

_SUMMARY_VERBS = re.compile(
    r"\b(riassum\w*|sintetizz\w*|sintesi|summar(?:y|ise|ize|ised|ized)|recap)\b", re.IGNORECASE
)
_SHORTEN_VERBS = re.compile(r"\b(accorcia\w*|pi[uù]\s+cort\w+|shorten|condens\w+)\b", re.IGNORECASE)
_LINE_BUDGET = re.compile(
    r"\b(?:in\s+)?(due|tre|quattro|cinque|two|three|four|five|\d{1,3})\s+"
    r"(righe|riga|linee|lines|line|parole|words|frasi|sentences)\b",
    re.IGNORECASE,
)
_NUMBER_WORDS = {
    "due": 2, "tre": 3, "quattro": 4, "cinque": 5,
    "two": 2, "three": 3, "four": 4, "five": 5,
}

_STOPWORDS = {
    "it": {"il", "lo", "la", "i", "gli", "le", "di", "che", "e", "per", "non", "con", "una", "un",
           "del", "della", "sono", "come", "questo", "questa", "nel", "alla", "più"},
    "en": {"the", "of", "and", "to", "in", "is", "it", "that", "for", "with", "this", "you",
           "are", "not", "be", "on", "as", "we", "your"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "mit", "den", "von", "zu", "im", "für",
           "ein", "eine", "auf", "sie", "wir", "dem"},
    "fr": {"le", "la", "les", "de", "des", "et", "est", "que", "pour", "dans", "un", "une",
           "pas", "vous", "nous", "au", "sur"},
    "es": {"el", "la", "los", "las", "de", "que", "y", "en", "un", "una", "por", "para", "no",
           "con", "se", "su", "es"},
}


def guess_language(text: str) -> str | None:
    """A stopword profile, enough to tell "same language as the request" apart.

    Returns None when the text is too short or no profile stands out — the
    caller then skips the check instead of reporting a wrong language.
    """
    words = _words(text)
    if len(words) < 8:
        return None
    hits = {lang: sum(1 for w in words if w in stop) for lang, stop in _STOPWORDS.items()}
    best, best_hits = max(hits.items(), key=lambda kv: kv[1])
    runner_up = sorted(hits.values(), reverse=True)[1] if len(hits) > 1 else 0
    if best_hits == 0 or best_hits <= runner_up:
        return None
    return best


def _words(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.findall(r"[\w'-]+", folded, re.UNICODE)


def verb_checks(request: str, output: str, source: str | None = None) -> list[CheckOutcome]:
    """Checks implied by the verb of the request, measured rather than judged."""
    outcomes: list[CheckOutcome] = []
    out_words = len(_words(output))

    if source and _SUMMARY_VERBS.search(request):
        src_words = len(_words(source))
        ratio = out_words / src_words if src_words else float("inf")
        outcomes.append(CheckOutcome(
            "code.shorter_than_source", ratio < 1.0,
            f"a summary must be shorter than its source: {out_words} words against "
            f"{src_words} (ratio {ratio:.2f})",
            score=ratio, where="code",
        ))
    if source and _SHORTEN_VERBS.search(request):
        src_words = len(_words(source))
        outcomes.append(CheckOutcome(
            "code.shorter_than_source", out_words < src_words,
            f"the rewrite must be shorter: {out_words} words against {src_words}",
            where="code",
        ))

    budget = _LINE_BUDGET.search(request)
    if budget:
        raw = budget.group(1).lower()
        limit = _NUMBER_WORDS.get(raw, None)
        if limit is None:
            limit = int(raw)
        unit = budget.group(2).lower()
        if unit.startswith(("righ", "line", "lin")):
            actual = len([ln for ln in output.splitlines() if ln.strip()])
            label = "lines"
        elif unit.startswith(("parol", "word")):
            actual = out_words
            label = "words"
        else:
            actual = len([s for s in re.split(r"[.!?]+", output) if s.strip()])
            label = "sentences"
        outcomes.append(CheckOutcome(
            "code.length_budget", actual <= limit,
            f"the request asks for at most {limit} {label}, the output has {actual}",
            score=float(actual), where="code",
        ))

    request_lang = guess_language(request)
    output_lang = guess_language(output)
    if request_lang and output_lang and not _is_translation(request):
        outcomes.append(CheckOutcome(
            "code.same_language", request_lang == output_lang,
            f"the request is in {request_lang}, the output in {output_lang}",
            where="code",
        ))
    return outcomes


_TRANSLATE_VERBS = re.compile(r"\b(traduc\w*|traduzione|translat\w*)\b", re.IGNORECASE)


def _is_translation(request: str) -> bool:
    return bool(_TRANSLATE_VERBS.search(request))


def verify(
    jev: JevClient,
    plan: Plan,
    output: str,
    source: str | None = None,
    attempts: int = 1,
) -> VerificationReport:
    """Post checks plus code checks, in one report."""
    outcomes = run_post_checks(jev, plan.post_checks, output, plan.request)
    outcomes.extend(verb_checks(plan.request, output, source))
    return VerificationReport(outcomes=outcomes, attempts=attempts)
