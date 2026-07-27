from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path

from fastembed import TextEmbedding

from app.core.config import settings


@dataclass(frozen=True)
class IntentMatch:
    intent: str
    score: float
    margin: float


class IntentRouter:
    def __init__(
        self,
        model=None,
        examples: dict[str, list[str]] | None = None,
        threshold: float = settings.intent_confidence_threshold,
        margin_threshold: float = settings.intent_margin_threshold,
    ):
        self.model = model or TextEmbedding(model_name=settings.intent_model_name)
        self.threshold = threshold
        self.margin_threshold = margin_threshold
        self.examples = examples or json.loads(
            Path(settings.intent_examples_path).read_text(encoding="utf-8")
        )
        texts = [text for values in self.examples.values() for text in values]
        labels = [intent for intent, values in self.examples.items() for _ in values]
        self.vectors = list(zip(labels, self.model.embed(texts)))

    def classify(self, text: str) -> IntentMatch:
        query = next(iter(self.model.embed([text])))
        scores = {
            intent: max(
                self._cosine(query, vector)
                for label, vector in self.vectors
                if label == intent
            )
            for intent in self.examples
        }
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        intent, score = ranked[0]
        margin = score - ranked[1][1]
        if score < self.threshold or margin < self.margin_threshold:
            intent = "unknown"
        return IntentMatch(intent=intent, score=score, margin=margin)

    @staticmethod
    def _cosine(left, right) -> float:
        dot = sum(float(a) * float(b) for a, b in zip(left, right))
        left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
        right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
        return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


@lru_cache
def get_intent_router() -> IntentRouter:
    return IntentRouter()
