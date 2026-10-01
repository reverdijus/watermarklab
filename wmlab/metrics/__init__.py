"""Metrics (ARCHITECTURE.md §4.5): quality metrics registered in ``METRICS``, robustness helpers.

Only quality metrics ``(x, x_w, **ctx) -> Tensor[B]`` are registered; robustness functions live
in :mod:`wmlab.metrics.robustness`.
"""

from __future__ import annotations

from wmlab.metrics import quality
from wmlab.metrics.robustness import (
    bit_accuracy,
    detection_threshold,
    false_positive_rate,
    matching_bits,
    tpr_at_fpr,
)
from wmlab.registry import METRICS

DEFAULT_QUALITY_METRICS: dict[str, str] = {"image": "psnr", "audio": "snr", "video": "psnr"}
HIGHER_IS_BETTER: dict[str, bool] = {
    "psnr": True, "ssim": True, "snr": True, "si_snr": True, "stoi": True,
    "lpips": False, "flicker": False,
}

_REGISTRATIONS = [
    ("psnr", ["image", "video"], quality.psnr, "PSNR (dB) entre original et tatoué — plus haut = "
                                               "plus imperceptible."),
    ("ssim", ["image", "video"], quality.ssim, "SSIM (0-1) entre original et tatoué — plus haut "
                                               "= plus imperceptible."),
    ("snr", "audio", quality.snr, "SNR (dB) du signal tatoué par rapport à l'original — plus "
                                  "haut = plus imperceptible."),
    ("si_snr", "audio", quality.si_snr, "SI-SNR (dB), invariant au gain — plus haut = plus "
                                        "imperceptible."),
    ("stoi", "audio", quality.stoi, "STOI (0-1, intelligibilité) — optionnel : nécessite "
                                    "pystoi (extra « quality »)."),
    ("lpips", ["image", "video"], quality.lpips, "Distance perceptuelle LPIPS — plus bas = plus "
                                                 "proche ; optionnel : nécessite lpips "
                                                 "(extra « quality »)."),
    ("flicker", "video", quality.flicker, "Scintillement temporel ajouté par le tatouage "
                                          "(moyenne |Δt résidu|) — plus bas = meilleur."),
]
for _name, _modality, _fn, _description in _REGISTRATIONS:
    METRICS.register(_name, modality=_modality, description=_description)(_fn)


def default_quality_metric(modality: str) -> str:
    """Default quality metric of a modality: ``psnr`` (image, video) or ``snr`` (audio)."""
    try:
        return DEFAULT_QUALITY_METRICS[modality]
    except KeyError:
        raise ValueError(f"modalité inconnue '{modality}' — valeurs possibles : "
                         f"{', '.join(DEFAULT_QUALITY_METRICS)}") from None


__all__ = [
    "DEFAULT_QUALITY_METRICS",
    "HIGHER_IS_BETTER",
    "bit_accuracy",
    "default_quality_metric",
    "detection_threshold",
    "false_positive_rate",
    "matching_bits",
    "tpr_at_fpr",
]
