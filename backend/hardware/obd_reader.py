"""Conexión real al carro vía ELM327 / OBDLink usando python-OBD.

Usa obd.Async: un hilo interno del driver va rotando los PIDs "vigilados" y nosotros solo
leemos el último valor (lectura no bloqueante, ideal para el loop de 10 Hz).
Nota: un ELM327 barato entrega ~15-40 PIDs/s en CAN y bastante menos en ISO 9141-2
(la 4Runner 2000), así que cada PID individual se refresca más lento que 10 Hz; el
frontend interpola sin problema.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import obd

from backend.hardware.base import TelemetrySource
from backend.hardware.dtc_codes import describe

log = logging.getLogger("baymax.obd")
obd.logger.setLevel(logging.WARNING)

# Campo de frame -> nombre del comando python-OBD (Modo 01) | (unidad destino)
PID_MAP = {
    "rpm": ("RPM", "rpm"),
    "speed_kph": ("SPEED", "kph"),
    "coolant_temp_c": ("COOLANT_TEMP", "degC"),
    "intake_temp_c": ("INTAKE_TEMP", "degC"),
    "stft_b1": ("SHORT_FUEL_TRIM_1", "percent"),
    "ltft_b1": ("LONG_FUEL_TRIM_1", "percent"),
    "map_kpa": ("INTAKE_PRESSURE", "kilopascal"),
    "maf_gs": ("MAF", "gps"),
    "throttle_pos": ("THROTTLE_POS", "percent"),
    "engine_load": ("ENGINE_LOAD", "percent"),
    "timing_advance": ("TIMING_ADVANCE", "degree"),
    "control_module_voltage": ("CONTROL_MODULE_VOLTAGE", "volt"),
}


def _magnitude(resp, unit: str) -> Optional[float]:
    if resp is None or resp.is_null():
        return None
    val = resp.value
    try:
        if hasattr(val, "to"):
            try:
                val = val.to(unit)
            except Exception:  # unidad ya compatible o sin conversión
                pass
            return round(float(val.magnitude), 2)
        return round(float(val), 2)
    except (TypeError, ValueError):
        return None


class OBDReader(TelemetrySource):
    kind = "real"

    def __init__(self, port: str, baudrate: Optional[int] = None, vehicle: Optional[dict] = None):
        self.port = port
        self.baudrate = baudrate
        self.vehicle = vehicle or {}
        self.conn: Optional[obd.Async] = None
        self.watched: Dict[str, object] = {}

    def connect(self) -> dict:
        self.conn = obd.Async(portstr=self.port, baudrate=self.baudrate, fast=False, timeout=1.0)
        if not self.conn.is_connected():
            status = self.conn.status()
            self.conn.close()
            self.conn = None
            raise ConnectionError(
                f"No se pudo conectar al vehículo en {self.port} (estado: {status}). "
                "Verifica que el switch esté en ON / motor encendido."
            )
        for field, (cmd_name, _unit) in PID_MAP.items():
            cmd = getattr(obd.commands, cmd_name, None)
            if cmd is not None and self.conn.supports(cmd):
                self.conn.watch(cmd)
                self.watched[field] = cmd
        self.conn.start()
        log.info("OBD conectado en %s | protocolo %s | PIDs: %s",
                 self.port, self.conn.protocol_name(), list(self.watched))
        return self.info()

    def is_connected(self) -> bool:
        return bool(self.conn and self.conn.is_connected())

    def info(self) -> dict:
        proto = self.conn.protocol_name() if self.conn else None
        baud = None
        if proto:
            if "500" in proto:
                baud = "500kbps"
            elif "250" in proto:
                baud = "250kbps"
            elif "9141" in proto or "14230" in proto:
                baud = "10.4kbps"
        return {
            "kind": self.kind,
            "port": self.port,
            "protocol": proto,
            "baud": baud,
            "supported_pids": list(self.watched),
        }

    def read(self) -> Dict[str, Optional[float]]:
        frame: Dict[str, Optional[float]] = {k: None for k in PID_MAP}
        if not self.conn:
            return frame
        for field, cmd in self.watched.items():
            frame[field] = _magnitude(self.conn.query(cmd), PID_MAP[field][1])
        return frame

    # Los comandos de "una sola vez" requieren pausar el loop async
    def _query_once(self, cmd):
        if not self.conn or cmd is None:
            return None
        with self.conn.paused():
            return self.conn.query(cmd, force=True)

    def get_dtcs(self) -> List[dict]:
        out: List[dict] = []
        seen = set()
        for cmd_name, status in (("GET_DTC", "ACTIVE"), ("GET_CURRENT_DTC", "PENDING")):
            resp = self._query_once(getattr(obd.commands, cmd_name, None))
            if resp is None or resp.is_null():
                continue
            for code, desc in resp.value or []:
                if code in seen:
                    continue
                seen.add(code)
                out.append({"code": code, "description": desc or describe(code), "status": status})
        return out

    def get_freeze_frame(self) -> Dict[str, Optional[float]]:
        """Modo 02: python-OBD expone los mismos PIDs con prefijo DTC_."""
        frame: Dict[str, Optional[float]] = {}
        if not self.conn:
            return frame
        with self.conn.paused():
            for field, (cmd_name, unit) in PID_MAP.items():
                cmd = getattr(obd.commands, f"DTC_{cmd_name}", None)
                if cmd is None:
                    continue
                try:
                    frame[field] = _magnitude(self.conn.query(cmd, force=True), unit)
                except Exception:  # algunas ECUs no soportan Modo 02 completo
                    frame[field] = None
        return frame

    def clear_dtcs(self) -> bool:
        resp = self._query_once(getattr(obd.commands, "CLEAR_DTC", None))
        return resp is not None and not resp.is_null()

    def get_vin(self) -> Optional[str]:
        resp = self._query_once(getattr(obd.commands, "VIN", None))
        if resp is None or resp.is_null():
            return None
        vin = resp.value
        if isinstance(vin, (bytes, bytearray)):
            vin = vin.decode(errors="ignore")
        vin = str(vin).strip()
        return vin if len(vin) == 17 else None

    def close(self) -> None:
        if self.conn:
            try:
                self.conn.stop()
                self.conn.close()
            finally:
                self.conn = None
