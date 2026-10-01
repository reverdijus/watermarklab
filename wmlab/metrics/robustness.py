"""Robustness metrics (ARCHITECTURE.md §4.5): bit accuracy and detection at a fixed FPR.

Detection test: a signal is declared watermarked when at least ``k`` of its ``n`` extracted
bits match the expected message. Under H0 (no watermark) the number of matches follows
``Binom(n, 1/2)``; ``detection_threshold(n, fpr)`` is the smallest ``k`` with
``P(Binom(n, 1/2) ≥ k) ≤ fpr``, computed exactly with integers.
"""

from __future__ import annotations

from fractions import Fraction
from functools import lru_cache
from math import comb

import torch
from torch import Tensor


def bit_accuracy(logits: Tensor, msg: Tensor) -> Tensor:
    """Fraction of correctly decoded bits per item (``logits > 0`` ⇒ bit 1), ``[B]``."""
    decoded = (logits.detach() > 0).to(torch.float32)
    return (decoded == msg.detach().to(decoded.device, torch.float32).round()).float().mean(dim=1)


def matching_bits(logits: Tensor, msg: Tensor) -> Tensor:
    """Number of correctly decoded bits per item, ``[B]`` (int64)."""
    decoded = (logits.detach() > 0).to(torch.float32)
    return (decoded == msg.detach().to(decoded.device, torch.float32).round()).sum(dim=1)


@lru_cache(maxsize=4096)
def _threshold(nbits: int, fpr: Fraction) -> int:
    budget = fpr * (2 ** nbits)  # P(X ≥ k) ≤ fpr  ⇔  #{outcomes with X ≥ k} ≤ fpr·2ⁿ
    tail = 0
    k = nbits + 1  # tail(n+1) = 0 always satisfies the bound
    for i in range(nbits, -1, -1):  # grow the tail from the top while it stays within budget
        tail += comb(nbits, i)
        if tail > budget:
            break
        k = i
    return k


def detection_threshold(nbits: int, fpr: float) -> int:
    """Smallest ``k`` such that ``P(Binom(nbits, 1/2) ≥ k) ≤ fpr`` (exact).

    Returns ``nbits + 1`` when even ``k = nbits`` exceeds ``fpr`` (detection impossible:
    ``2**-nbits > fpr``).
    """
    if int(nbits) != nbits or nbits <= 0:
        raise ValueError(f"nbits doit être un entier > 0 (reçu {nbits!r})")
    if not 0.0 < float(fpr) <= 1.0:
        raise ValueError(f"fpr doit être dans ]0, 1] (reçu {fpr!r})")
    return _threshold(int(nbits), Fraction(float(fpr)))


def tpr_at_fpr(logits: Tensor, msg: Tensor, fpr: float) -> float:
    """True positive rate of the detection test at false positive rate ``fpr``."""
    if logits.shape[0] == 0:
        return float("nan")
    k = detection_threshold(int(logits.shape[1]), fpr)
    return float((matching_bits(logits, msg) >= k).float().mean())


def false_positive_rate(nbits: int, k: int) -> float:
    """Exact ``P(Binom(nbits, 1/2) ≥ k)`` (float)."""
    if k <= 0:
        return 1.0
    if k > nbits:
        return 0.0
    tail = sum(comb(nbits, i) for i in range(k, nbits + 1))
    return float(Fraction(tail, 2 ** nbits))
