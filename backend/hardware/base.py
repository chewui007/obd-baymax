"""Interfaz común para fuentes de telemetría (cable real o simulador)."""
from __future__ import annotations

from typing import Dict, List, Optional

# Campos estándar de un frame de telemetría (unidades métricas)
FRAME_FIELDS = (
    "rpm",                    # PID 0C
    "speed_kph",              # PID 0D
    "coolant_temp_c",         # PID 05
    "intake_temp_c",          # PID 0F (IAT)
    "stft_b1",                # PID 06 (%)
    "ltft_b1",                # PID 07 (%)
    "map_kpa",                # PID 0B
    "maf_gs",                 # PID 10
    "throttle_pos",           # PID 11 (%)
    "engine_load",            # PID 04 (%)
    "timing_advance",         # PID 0E (° antes de PMS)
    "control_module_voltage", # PID 42 (V)
)


class TelemetrySource:
    """Contrato que implementan OBDReader (real) y OBDMock (simulador)."""

    kind: str = "base"
    port: str = ""

    def connect(self) -> dict:  # bloqueante; se llama desde un hilo
        raise NotImplementedError

    def is_connected(self) -> bool:
        raise NotImplementedError

    def read(self) -> Dict[str, Optional[float]]:
        raise NotImplementedError

    def get_dtcs(self) -> List[dict]:
        """[{code, description, status: ACTIVE|PENDING}]"""
        raise NotImplementedError

    def get_freeze_frame(self) -> Dict[str, Optional[float]]:
        raise NotImplementedError

    def clear_dtcs(self) -> bool:
        raise NotImplementedError

    def get_vin(self) -> Optional[str]:
        return None

    def info(self) -> dict:
        return {"kind": self.kind, "port": self.port}

    def control(self, action: str, **kwargs) -> dict:
        """Comandos extra (solo simulador): modo idle / cruise / pull."""
        return {"ok": False, "error": "No soportado por esta fuente"}

    def close(self) -> None:
        pass
