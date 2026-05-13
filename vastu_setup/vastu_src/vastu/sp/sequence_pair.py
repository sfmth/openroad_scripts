from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass
class SequencePair:
    """Two permutations (gamma_plus, gamma_minus) over the same set of block names.

    Constraint semantics (Tang & Otten):
        a < b in both           => a is to the LEFT of b
        a < b in plus, a > b in minus  => a is BELOW b
        a > b in plus, a < b in minus  => a is ABOVE b
        a > b in both           => a is to the RIGHT of b
    """

    plus: list[str]
    minus: list[str]

    def __post_init__(self) -> None:
        if len(self.plus) != len(self.minus):
            raise ValueError("plus and minus must be the same length")
        if set(self.plus) != set(self.minus):
            raise ValueError("plus and minus must contain the same blocks")

    @classmethod
    def random(cls, names: list[str], rng: random.Random | None = None) -> "SequencePair":
        rng = rng or random.Random()
        plus = list(names)
        minus = list(names)
        rng.shuffle(plus)
        rng.shuffle(minus)
        return cls(plus=plus, minus=minus)

    def copy(self) -> "SequencePair":
        return SequencePair(plus=list(self.plus), minus=list(self.minus))

    @property
    def n(self) -> int:
        return len(self.plus)

    def index_in(self, seq: list[str]) -> dict[str, int]:
        return {name: i for i, name in enumerate(seq)}
