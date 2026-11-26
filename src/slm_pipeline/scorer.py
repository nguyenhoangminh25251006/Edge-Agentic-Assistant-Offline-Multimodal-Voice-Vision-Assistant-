"""scorer.py — Label probability scoring, margin calculation, and token verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from control_pipeline.schemas.fsm import Action
from slm_pipeline.contracts import IntentClass, IntentScore, LlmClient, TokenProb


@dataclass(frozen=True)
class LabelSpec:
    label: str
    intent_class: IntentClass
    action: Action | None
    needs_slot: Literal["speed"] | None = None


class LabelScorer:
    """Aggregates first-token probabilities into discrete label probabilities."""

    def __init__(self, labels: list[LabelSpec]) -> None:
        self._labels = labels
        self._label_map = {l.label: l for l in labels}

    @property
    def label_map(self) -> dict[str, LabelSpec]:
        return self._label_map

    def score(self, tokens: list[TokenProb]) -> IntentScore:
        """Score token probabilities into labels without renormalization."""
        # Sanity check: individual token probability bounds and sum bound
        total_token_prob = 0.0
        for tp in tokens:
            if tp.prob < 0.0 or tp.prob > 1.0001:
                raise ValueError(f"Token probability out of legal range [0, 1]: {tp.prob}")
            total_token_prob += tp.prob

        if total_token_prob > 1.0001:
            raise ValueError(f"Total token probabilities sum ({total_token_prob:.4f}) exceeds 1.0")

        # Accumulate probabilities by matching normalized label
        probs: dict[str, float] = {lbl: 0.0 for lbl in self._label_map}

        for tp in tokens:
            cleaned = tp.token.strip().lower()
            if cleaned in probs:
                probs[cleaned] += tp.prob

        # Calculate mass (sum over our label set)
        mass = sum(probs.values())

        # Sort descending
        top = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)

        p1 = top[0][1] if top else 0.0
        p2 = top[1][1] if len(top) > 1 else 0.0
        margin = p1 - p2

        return IntentScore(
            label_probs=probs,
            mass=mass,
            top=top,
            p1=p1,
            p2=p2,
            margin=margin,
        )

    async def verify_labels(self, client: LlmClient) -> tuple[bool, list[str]]:
        """Startup check: ensure all labels tokenize to exactly one distinct token id."""
        failures: list[str] = []
        token_to_label: dict[int, str] = {}

        for label in self._label_map:
            try:
                tokens = await client.tokenize(label)
            except Exception as exc:
                failures.append(f"Failed to tokenize label '{label}': {exc}")
                continue

            if len(tokens) != 1:
                failures.append(f"Label '{label}' tokenizes to {len(tokens)} tokens ({tokens}), expected 1")
                continue

            tid = tokens[0]
            if tid in token_to_label:
                failures.append(f"Label '{label}' shares token id {tid} with label '{token_to_label[tid]}'")
            else:
                token_to_label[tid] = label

        return (len(failures) == 0, failures)
