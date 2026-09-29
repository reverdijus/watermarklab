"""Non-blocking system metrics sampler (GPU via nvidia-smi, else psutil; MPS memory)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Any

NVIDIA_SMI_TIMEOUT_S = 5


class SystemSampler:
    """Collects ``gpu_util``, ``gpu_mem_gb``, ``cpu_percent``, ``ram_gb``, ``proc_ram_gb``."""

    def __init__(self) -> None:
        self.nvidia_smi = shutil.which("nvidia-smi")
        self._psutil: Any = None
        self._process: Any = None
        try:
            import psutil

            self._psutil = psutil
            self._process = psutil.Process()
            psutil.cpu_percent(interval=None)  # prime: the first call always returns 0.0
        except Exception:
            self._psutil = None

    def _nvidia(self) -> dict[str, float]:
        if not self.nvidia_smi:
            return {}
        try:
            out = subprocess.run(
                [self.nvidia_smi, "--query-gpu=utilization.gpu,memory.used",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=NVIDIA_SMI_TIMEOUT_S, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        utils: list[float] = []
        mems: list[float] = []
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            try:
                utils.append(float(parts[0]))
                mems.append(float(parts[1]))
            except ValueError:
                continue
        if not utils:
            return {}
        return {"gpu_util": sum(utils) / len(utils), "gpu_mem_gb": sum(mems) / 1024.0}

    @staticmethod
    def _mps() -> dict[str, float]:
        torch = sys.modules.get("torch")  # never import torch from the sampler thread
        if torch is None:
            return {}
        try:
            if torch.backends.mps.is_available():
                return {"gpu_mem_gb": torch.mps.current_allocated_memory() / 1024**3}
        except Exception:
            pass
        return {}

    def sample(self) -> dict[str, float]:
        """Return one sample (missing sources are simply omitted)."""
        values: dict[str, float] = {}
        values.update(self._nvidia())
        if "gpu_mem_gb" not in values:
            values.update(self._mps())
        if self._psutil is not None:
            try:
                values["cpu_percent"] = float(self._psutil.cpu_percent(interval=None))
                values["ram_gb"] = self._psutil.virtual_memory().used / 1024**3
                values["proc_ram_gb"] = self._process.memory_info().rss / 1024**3
            except Exception:
                pass
        return values
