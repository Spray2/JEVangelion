"""Verification: JEV post checks, and the code checks tied to the verb."""

from __future__ import annotations

from jev.benchmarks.plans import V21_PLAN, V26_PLAN
from jev.checks import guess_language, run_post_checks, verb_checks, verify
from jev.client import RecordedJevClient


def test_post_checks_are_one_call_against_output_plus_brief():
    client = RecordedJevClient({"v1": 0.9, "v2": 0.95, "v3": 0.8})
    outcomes = run_post_checks(client, V26_PLAN.post_checks, "una risposta", V26_PLAN.request)
    assert len(client.calls) == 1
    state, ids = client.calls[0]
    assert set(state) == {"output", "brief"} and ids == ("v1", "v2", "v3")
    assert all(o.passed for o in outcomes)


def test_a_post_check_below_the_threshold_fails():
    client = RecordedJevClient({"v1": 0.9, "v2": 0.95, "v3": 0.31})
    outcomes = run_post_checks(client, V26_PLAN.post_checks, "reply", V26_PLAN.request)
    assert [o.id for o in outcomes if not o.passed] == ["v3"]


def test_a_summary_longer_than_its_source_is_caught_in_code():
    # The end-to-end run passed four JEV post checks at 0.79-0.99 on a 110-word
    # "summary" of an 88-word document. A ratio catches what a Noul did not.
    outcomes = verb_checks(
        "Riassumi questo verbale: va inviato al consiglio di amministrazione.",
        "parola " * 110,
        "testo " * 88,
    )
    failed = [o for o in outcomes if not o.passed]
    assert [o.id for o in failed] == ["code.shorter_than_source"]
    assert failed[0].score > 1


def test_a_real_summary_passes():
    outcomes = verb_checks("Riassumi questo verbale.", "parola " * 30, "testo " * 88)
    assert all(o.passed for o in outcomes)


def test_a_line_budget_is_measured_not_judged():
    outcomes = verb_checks("Scrivimi due righe per il capo.", "uno\ndue\ntre")
    budget = [o for o in outcomes if o.id == "code.length_budget"]
    assert budget and not budget[0].passed


def test_a_respected_line_budget_passes():
    outcomes = verb_checks("Scrivimi due righe per il capo.", "uno\ndue")
    assert all(o.passed for o in outcomes)


def test_language_drift_is_caught_unless_the_request_asks_for_a_translation():
    english_output = "The quick brown fox jumps over the lazy dog and it is not a problem."
    italian_request = ("Rispondi a questa mail di un cliente arrabbiato e dimmi se va passata "
                       "a un responsabile del servizio clienti.")
    drift = [o for o in verb_checks(italian_request, english_output)
             if o.id == "code.same_language"]
    assert drift and not drift[0].passed

    translation_request = ("Traduci il manuale utente in tedesco per la pubblicazione sul "
                           "sito di questa azienda che vende prodotti.")
    assert not [o for o in verb_checks(translation_request, english_output)
                if o.id == "code.same_language"]


def test_guess_language_abstains_on_short_or_ambiguous_text():
    assert guess_language("ciao") is None
    assert guess_language("xyz " * 20) is None


def test_verify_merges_jev_and_code_outcomes():
    client = RecordedJevClient({}, default_noul=0.9)
    report = verify(client, V26_PLAN, "Gentile cliente, mi dispiace per il disservizio che ha "
                                      "avuto e le propongo una soluzione concreta per il "
                                      "rinnovo del contratto con noi.")
    assert {o.where for o in report.outcomes} == {"jev", "code"}
    assert report.ok


def test_a_translation_request_produces_no_language_check():
    # V21 asks for German output from an Italian request: "same language" would
    # be exactly the wrong check, so it is not emitted at all.
    client = RecordedJevClient({}, default_noul=0.9)
    report = verify(client, V21_PLAN, "Das Handbuch ist auf Deutsch und nicht zu kurz für die "
                                      "Veröffentlichung auf der Website von dem Kunden.")
    assert {o.where for o in report.outcomes} == {"jev"}
    assert report.ok


def test_escalation_only_after_the_regeneration_budget_is_spent():
    client = RecordedJevClient({}, default_noul=0.1)
    first = verify(client, V21_PLAN, "kurz", attempts=1)
    second = verify(client, V21_PLAN, "kurz", attempts=2)
    assert not first.ok and not first.escalate
    assert second.escalate
