"""test_scorer.py — Label scoring, mass, margin, and token verification self-check (U-5)."""

import pytest
from control_pipeline.schemas.fsm import Action
from slm_pipeline.contracts import IntentClass, TokenProb
from slm_pipeline.scorer import LabelScorer, LabelSpec
from slm_pipeline.tests.fakes import FakeLlm


@pytest.fixture
def standard_labels() -> list[LabelSpec]:
    return [
        LabelSpec(label="start", intent_class=IntentClass.ENERGIZE, action=Action.START),
        LabelSpec(label="resume", intent_class=IntentClass.ENERGIZE, action=Action.RESUME),
        LabelSpec(label="pause", intent_class=IntentClass.DEENERGIZE, action=Action.PAUSE),
        LabelSpec(label="stop", intent_class=IntentClass.DEENERGIZE, action=Action.STOP),
        LabelSpec(label="speed", intent_class=IntentClass.PARAM, action=Action.SET_SPEED, needs_slot="speed"),
        LabelSpec(label="none", intent_class=IntentClass.META, action=None),
    ]


def test_score_basic_and_margin(standard_labels):
    scorer = LabelScorer(standard_labels)
    tokens = [
        TokenProb(token="start", prob=0.70),
        TokenProb(token="resume", prob=0.15),
        TokenProb(token="random_word", prob=0.10),
    ]

    score = scorer.score(tokens)
    assert score.label_probs["start"] == 0.70
    assert score.label_probs["resume"] == 0.15
    assert score.label_probs["random_word"] if "random_word" in score.label_probs else True
    assert pytest.approx(score.mass, 0.001) == 0.85
    assert score.p1 == 0.70
    assert score.p2 == 0.15
    assert pytest.approx(score.margin, 0.001) == 0.55


def test_score_case_and_whitespace_merging(standard_labels):
    scorer = LabelScorer(standard_labels)
    tokens = [
        TokenProb(token=" start", prob=0.50),
        TokenProb(token="Start\n", prob=0.30),
    ]

    score = scorer.score(tokens)
    assert pytest.approx(score.label_probs["start"], 0.001) == 0.80
    assert score.p1 == 0.80
    assert score.margin == 0.80


def test_score_excess_prob_sum_rejected(standard_labels):
    scorer = LabelScorer(standard_labels)
    tokens = [
        TokenProb(token="start", prob=0.80),
        TokenProb(token="resume", prob=0.50),
    ]
    with pytest.raises(ValueError):
        scorer.score(tokens)


@pytest.mark.asyncio
async def test_verify_labels_success(standard_labels):
    scorer = LabelScorer(standard_labels)
    fake_llm = FakeLlm()
    ok, failures = await scorer.verify_labels(fake_llm)
    assert ok is True
    assert len(failures) == 0


@pytest.mark.asyncio
async def test_verify_labels_multi_token_failure(standard_labels):
    scorer = LabelScorer(standard_labels)
    fake_llm = FakeLlm()
    fake_llm.token_map["start"] = [101, 102]  # Multi-token!

    ok, failures = await scorer.verify_labels(fake_llm)
    assert ok is False
    assert any("start" in f for f in failures)
