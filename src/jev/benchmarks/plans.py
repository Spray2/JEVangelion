"""Reference plans from docs/spec.md, used as fixtures.

``V26_PLAN`` is the corrected plan printed in the spec, with the ``v3`` post
check the L6 test then found missing. ``V21_PLAN`` follows the spec's prose for
the translation case, including the ``v4`` fluency check L6 asked for.
``V20_BAD_PLAN`` is the economy-tier regression the code form of L10 catches.
"""

from __future__ import annotations

from ..benchmarks.cases import by_id
from ..contract import Plan

V26_REQUEST = by_id("V26").request
V21_REQUEST = by_id("V21").request
V20_REQUEST = by_id("V20").request

V26_PLAN = Plan.parse(
    {
        "task_shape": "mixed",
        "jev_role": "pre+post",
        "gate_rationale": "A decision on the message, then a retention reply.",
        "asks": [
            "Decide whether the customer message is a threat to terminate the contract.",
            "If it is, write a retention reply to the customer.",
        ],
        "states": {"S1": {"source": "user_input", "content": "{{customer_message}}"}},
        "rounds": [
            {
                "round": 1,
                "state": "S1",
                "questions": [
                    {
                        "id": "q2",
                        "type": "score",
                        "role": "pre",
                        "instructions": "How firmly does `customer_message` express an "
                                        "intention to leave?",
                        "criteria": [
                            {"what": "No mention of leaving",
                             "examples": ["not happy with the last delivery"]},
                            {"what": "Leaving mentioned as a possibility",
                             "examples": ["we might look at other options"]},
                            {"what": "Conditional ultimatum",
                             "examples": ["if this is not fixed by Friday we will cancel"]},
                            {"what": "Formal notice of cancellation",
                             "examples": ["please consider this our notice of termination"]},
                        ],
                    },
                    {
                        "id": "q3",
                        "type": "choice",
                        "role": "pre",
                        "instructions": "What is the main reason for dissatisfaction in "
                                        "`customer_message`?",
                        "criteria": [
                            {"id": "price", "what": "The cost of the service or product"},
                            {"id": "service", "what": "How the customer was treated or supported"},
                            {"id": "product", "what": "The product itself"},
                            {"id": "competitor", "what": "A better offer elsewhere"},
                            {"id": "other", "what": "Anything else"},
                        ],
                    },
                ],
            }
        ],
        "policy": [
            {"when": "q2.score < 1",
             "then": "not a threat: tell the user, no retention reply"},
            {"when": "q2.score >= 3", "then": "notify account manager and draft reply"},
            {"when": "any.confidence < 0.5", "then": "escalate"},
        ],
        "residual_prompt": "Prepara una risposta di retention a questo messaggio del cliente. "
                           "Contesto già valutato: motivo principale {{q3}}, intensità della "
                           "minaccia {{q2}}.",
        "post_checks": [
            {"id": "v1", "type": "noul",
             "instructions": "Does `reply` address the specific reason for dissatisfaction?"},
            {"id": "v2", "type": "noul",
             "instructions": "Is `reply` written in the same language as `customer_message`?"},
            {"id": "v3", "type": "noul",
             "instructions": "Does `reply` try to retain the customer with a concrete fix, "
                             "offer or commitment?"},
        ],
    },
    request=V26_REQUEST,
)

V21_PLAN = Plan.parse(
    {
        "task_shape": "generative",
        "jev_role": "post",
        "gate_rationale": "Generated text going to publication must be verified.",
        "asks": [
            "The user manual must be translated.",
            "The translation must be in German.",
            "The result must be fit for publication on the website.",
        ],
        "states": {},
        "rounds": [],
        "policy": [
            {"when": "post_check_failed_twice",
             "then": "escalate to a human translator"},
        ],
        "residual_prompt": V21_REQUEST,
        "post_checks": [
            {"id": "v1", "type": "noul",
             "instructions": "Does `output` contain a translation of the whole user manual?"},
            {"id": "v2", "type": "noul", "instructions": "Is `output` written in German?"},
            {"id": "v3", "type": "noul",
             "instructions": "Does `output` keep the meaning of every section of the source "
                             "manual?"},
            {"id": "v4", "type": "noul",
             "instructions": "Is `output` fluent and idiomatic enough to publish without "
                             "further editing?"},
        ],
    },
    request=V21_REQUEST,
)

#: The economy tier used `criteria_from` for four dimensions the request already
#: names. The default tier writes four Scores instead; L10 in code catches this.
V20_BAD_PLAN = Plan.parse(
    {
        "task_shape": "judgment",
        "jev_role": "core",
        "gate_rationale": "Several kinds of assessment on the same pull request.",
        "asks": [
            "Rate the readability of the pull request.",
            "Rate the test coverage of the pull request.",
            "Rate the performance of the pull request.",
            "Rate the security of the pull request.",
        ],
        "states": {"S1": {"source": "user_input", "content": "{{pull_request}}"}},
        "rounds": [
            {
                "round": 1,
                "state": "S1",
                "questions": [
                    {
                        "id": "q1",
                        "type": "score",
                        "role": "core",
                        "criteria_from": "{{dimensions}}",
                        "instructions": "How well does `pull_request` satisfy 'criterion'?",
                        "criteria": [
                            {"what": "Not satisfied", "examples": ["no tests at all"]},
                            {"what": "Partly satisfied", "examples": ["one happy-path test"]},
                            {"what": "Fully satisfied", "examples": ["edge cases covered"]},
                        ],
                    }
                ],
            }
        ],
        "policy": [{"when": "any.confidence < 0.5", "then": "escalate"}],
        "residual_prompt": None,
        "post_checks": [],
    },
    request=V20_REQUEST,
)

PLANS = {"V20_bad": V20_BAD_PLAN, "V21": V21_PLAN, "V26": V26_PLAN}
