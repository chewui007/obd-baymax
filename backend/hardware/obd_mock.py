"""EL SIMULADOR: genera telemetría realista sin cable, con física de verdad.

Cada carro de la flota tiene un perfil con su curva de torque, transmisión y una falla
"sembrada" para que puedas probar el agente y el dyno sin salir del escritorio:

- La Runner (3RZ-FE):  fuga de vacío -> P0171, trims altos en mínimo que bajan con carga.
- Meches C250 (M271):  fuga leve en intercooler/boost + heat soak + retardo por cascabeleo
                       -> P0299 pendiente. Ideal para la Prueba Pre-Stage.
- Pilot (J35):         falla de encendido cilindro 2 -> P0302, RPM inestables en mínimo.

Modos: idle | cruise | pull (acelerón a fondo en una marcha de ~2,000 RPM al corte).
Durante un pull la velocidad sale de F = m·a con pérdidas aerodinámicas y de rodadura,
así el Virtual Dyno recupera una curva de potencia coherente.
"""
from __future__ import annotations

import json
import math
import random
import time
from typing import Dict, List, Optional

from backend.hardware.base import TelemetrySource
from backend.hardware.dtc_codes import describe

G = 9.81
RHO_AIR = 1.2
BARO_KPA = 101.3
AMBIENT_C = 28.0

PROFILES = {
    "M271": {
        "idle_rpm": 720,
        "peak_torque_nm": 310,
        # (rpm, fracción del torque pico) – meseta típica de turbo chico
        "torque_curve": [(1000, 0.55), (1800, 0.85), (2200, 1.0), (4300, 1.0), (5200, 0.88), (6000, 0.72), (6500, 0.6)],
        "turbo": True,
        "boost_target_kpa": 195,     # absoluto (~13.5 psi)
        "boost_leak": 0.07,          # pierde 7% de boost en altas
        "intercooler_heat_rate": 2.2,  # °C/s a boost pleno (intercooler saturándose)
        "knock_prone": True,
        "vacuum_leak_gs": 0.0,
        "learned_ltft": 3.1,
        "misfire_cyl": None,
        "maf_scale": 1.0,
        "dtcs": [{"code": "P0299", "status": "PENDING"}],
        "freeze_frame": {"rpm": 4650, "speed_kph": 98, "coolant_temp_c": 93, "intake_temp_c": 54,
                         "stft_b1": 0.0, "ltft_b1": 3.1, "map_kpa": 172, "maf_gs": 118.4,
                         "throttle_pos": 94, "engine_load": 100, "timing_advance": 4.5,
                         "control_module_voltage": 13.9},
    },
    "3RZ": {
        "idle_rpm": 750,
        "peak_torque_nm": 240,
        "torque_curve": [(1000, 0.70), (2000, 0.84), (3000, 0.93), (4000, 1.0), (4600, 0.97), (5200, 0.86), (5600, 0.78)],
        "turbo": False,
        "vacuum_leak_gs": 0.85,      # aire no medido detrás del MAF
        "learned_ltft": 19.2,
        "knock_prone": False,
        "misfire_cyl": None,
        "maf_scale": 0.82,           # MAF ligeramente sucio
        "dtcs": [{"code": "P0171", "status": "ACTIVE"}],
        "freeze_frame": {"rpm": 750, "speed_kph": 0, "coolant_temp_c": 89, "intake_temp_c": 36,
                         "stft_b1": 14.5, "ltft_b1": 19.2, "map_kpa": 36, "maf_gs": 2.1,
                         "throttle_pos": 14, "engine_load": 24, "timing_advance": 9.5,
                         "control_module_voltage": 14.1},
    },
    "J35": {
        "idle_rpm": 680,
        "peak_torque_nm": 342,
        "torque_curve": [(1000, 0.66), (2000, 0.78), (3000, 0.86), (4000, 0.93), (4800, 1.0), (5600, 0.95), (6300, 0.84)],
        "turbo": False,
        "vacuum_leak_gs": 0.0,
        "learned_ltft": 1.6,
        "knock_prone": False,
        "misfire_cyl": 2,
        "maf_scale": 1.0,
        "dtcs": [{"code": "P0302", "status": "ACTIVE"}],
        "freeze_frame": {"rpm": 690, "speed_kph": 0, "coolant_temp_c": 91, "intake_temp_c": 38,
                         "stft_b1": 2.3, "ltft_b1": 1.6, "map_kpa": 31, "maf_gs": 4.9,
                         "throttle_pos": 13, "engine_load": 22, "timing_advance": 11.0,
                         "control_module_voltage": 14.2},
    },
}

DEFAULT_VEHICLE = {
    "engine_code": "GENERIC", "displacement_l": 2.0, "curb_weight_kg": 1400, "drag_coef": 0.32,
    "frontal_area_m2": 2.2, "tire_diameter_m": 0.65, "final_drive": 4.0,
    "gear_ratios": [3.5, 2.0, 1.4, 1.0, 0.8], "redline_rpm": 6000,
}


def _profile_for(vehicle: dict) -> dict:
    code = (vehicle.get("engine_code") or "").upper()
    for key, prof in PROFILES.items():
        if key in code:
            return json.loads(json.dumps(prof))  # copia profunda
    return json.loads(json.dumps(PROFILES["J35"] | {"misfire_cyl": None, "dtcs": []}))


def _interp(points, x):
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def default_pull_gear(gear_ratios: List[float]) -> int:
    return 3 if len(gear_ratios) >= 6 else 2


class OBDMock(TelemetrySource):
    kind = "mock"

    def __init__(self, vehicle: Optional[dict] = None):
        self.port = "MOCK"
        v = dict(DEFAULT_VEHICLE)
        v.update({k: val for k, val in (vehicle or {}).items() if val not in (None, [], "")})
        self.v = v
        self.p = _profile_for(v)
        self.ratios: List[float] = v["gear_ratios"]
        self.final = float(v["final_drive"])
        self.tire_d = float(v["tire_diameter_m"])
        self.mass = float(v["curb_weight_kg"]) + 80.0  # + conductor
        self.redline = int(v["redline_rpm"])
        self.disp_l = float(v["displacement_l"])

        self._connected = False
        self._dtcs = [dict(d, description=describe(d["code"])) for d in self.p["dtcs"]]
        self._cleared_at: Optional[float] = None
        self._overheat = False

        # Estado dinámico
        self.mode = "idle"
        self.gear = 1
        self.v_ms = 0.0
        self.rpm = float(self.p["idle_rpm"])
        self.throttle = 0.0
        self.boost_frac = 0.0
        self.coolant = 84.0
        self.iat = AMBIENT_C + 8
        self.knock_retard = 0.0
        self.pull_phase: Optional[str] = None
        self.pull_gear = default_pull_gear(self.ratios)
        self._phase_t = 0.0
        self._t = 0.0
        self._last = time.monotonic()

    # ------------------------------------------------------------------ API
    def connect(self) -> dict:
        time.sleep(0.6)  # simula el handshake ATZ / ATSP0 del ELM327
        self._connected = True
        self._last = time.monotonic()
        return self.info()

    def is_connected(self) -> bool:
        return self._connected

    def info(self) -> dict:
        return {
            "kind": self.kind,
            "port": self.port,
            "protocol": self.v.get("obd_protocol") or "ISO 15765-4 (CAN 11/500)",
            "baud": "500kbps" if "CAN" in (self.v.get("obd_protocol") or "CAN") else "10.4kbps",
            "mode": self.mode,
            "pull_gear": self.pull_gear,
            "gears": len(self.ratios),
        }

    def control(self, action: str, **kwargs) -> dict:
        if action in ("idle", "cruise"):
            self.mode, self.pull_phase = action, None
        elif action == "pull":
            gear = int(kwargs.get("gear") or self.pull_gear)
            self.pull_gear = max(1, min(gear, len(self.ratios)))
            self.mode, self.pull_phase, self._phase_t = "pull", "prep", 0.0
        elif action == "overheat":
            self._overheat = bool(kwargs.get("enabled", not self._overheat))
        else:
            return {"ok": False, "error": f"Acción desconocida: {action}"}
        return {"ok": True, "mode": self.mode, "pull_gear": self.pull_gear, "overheat": self._overheat}

    def get_dtcs(self) -> List[dict]:
        # Si la falla sigue presente, el código regresa como PENDING ~45 s después de borrar
        if self._cleared_at and time.monotonic() - self._cleared_at > 45 and not self._dtcs:
            self._dtcs = [dict(d, status="PENDING", description=describe(d["code"])) for d in self.p["dtcs"]]
            self._cleared_at = None
        dtcs = list(self._dtcs)
        if self._overheat and self.coolant > 108 and not any(d["code"] == "P0217" for d in dtcs):
            dtcs.append({"code": "P0217", "status": "ACTIVE", "description": describe("P0217")})
        return dtcs

    def get_freeze_frame(self) -> Dict[str, Optional[float]]:
        return dict(self.p["freeze_frame"])

    def clear_dtcs(self) -> bool:
        self._dtcs = []
        self._cleared_at = time.monotonic()
        return True

    def close(self) -> None:
        self._connected = False

    # ------------------------------------------------------------ simulación
    def _overall(self, gear: int) -> float:
        return self.ratios[gear - 1] * self.final

    def _rpm_from_speed(self, v_ms: float, gear: int) -> float:
        return v_ms * 60.0 * self._overall(gear) / (math.pi * self.tire_d)

    def _speed_from_rpm(self, rpm: float, gear: int) -> float:
        return rpm * math.pi * self.tire_d / (60.0 * self._overall(gear))

    def _auto_gear(self, v_ms: float, target_rpm: float = 1900) -> int:
        best = 1
        for g in range(1, len(self.ratios) + 1):
            if self._rpm_from_speed(v_ms, g) >= target_rpm:
                best = g
        return best

    def _engine_torque(self, rpm: float) -> float:
        frac = _interp(self.p["torque_curve"], rpm)
        tq = self.p["peak_torque_nm"] * frac * self.throttle
        if self.p["turbo"]:
            tq *= 0.55 + 0.45 * self.boost_frac
        tq *= 1.0 - 0.015 * self.knock_retard  # cada grado de retardo cuesta ~1.5%
        return tq

    def _step(self, dt: float) -> None:
        p = self.p
        self._t += dt
        self._phase_t += dt
        idle = p["idle_rpm"]

        if self.mode == "idle":
            self.throttle += (0.0 - self.throttle) * min(1, dt * 4)
            self.v_ms = max(0.0, self.v_ms - 3.0 * dt)
            self.gear = max(1, self._auto_gear(self.v_ms))
            target = max(idle, self._rpm_from_speed(self.v_ms, self.gear)) if self.v_ms > 2 else idle
            self.rpm += (target - self.rpm) * min(1, dt * 3)

        elif self.mode == "cruise":
            target_v = 60 / 3.6 + 1.5 * math.sin(self._t / 7)
            self.v_ms += max(-2.5, min(2.0, (target_v - self.v_ms) * 0.6)) * dt
            self.gear = self._auto_gear(self.v_ms)
            self.throttle += (0.18 + 0.04 * math.sin(self._t / 3) - self.throttle) * min(1, dt * 2)
            self.rpm += (max(idle, self._rpm_from_speed(self.v_ms, self.gear)) - self.rpm) * min(1, dt * 5)

        elif self.mode == "pull":
            g = self.pull_gear
            if self.pull_phase == "prep":
                # rodar estable a ~2,000 RPM en la marcha del pull
                target_v = self._speed_from_rpm(2000, g)
                self.v_ms += (target_v - self.v_ms) * min(1, dt * 1.8)
                self.gear = g
                self.throttle += (0.2 - self.throttle) * min(1, dt * 3)
                self.rpm = self._rpm_from_speed(self.v_ms, g)
                if self._phase_t > 2.5 and abs(self.rpm - 2000) < 60:
                    self.pull_phase, self._phase_t = "wot", 0.0
            elif self.pull_phase == "wot":
                self.gear = g
                self.throttle += (1.0 - self.throttle) * min(1, dt * 12)
                ov = self._overall(g)
                f_wheel = self._engine_torque(self.rpm) * ov * 0.85 / (self.tire_d / 2)
                f_aero = 0.5 * RHO_AIR * self.v["drag_coef"] * self.v["frontal_area_m2"] * self.v_ms ** 2
                f_roll = 0.012 * self.mass * G
                m_eff = self.mass * (1.04 + 0.0025 * ov ** 2)
                self.v_ms += (f_wheel - f_aero - f_roll) / m_eff * dt
                self.rpm = self._rpm_from_speed(self.v_ms, g)
                if self.rpm >= self.redline - 80 or self._phase_t > 25:
                    self.pull_phase, self._phase_t = "lift", 0.0
            elif self.pull_phase == "lift":
                self.throttle += (0.0 - self.throttle) * min(1, dt * 10)
                self.v_ms = max(0.0, self.v_ms - 2.2 * dt)
                self.rpm = max(idle, self._rpm_from_speed(self.v_ms, self.gear))
                if self._phase_t > 3.0:
                    self.mode, self.pull_phase = "cruise", None

        # --- turbo
        if p["turbo"]:
            spool_target = self.throttle * max(0.0, min(1.0, (self.rpm - 1500) / 1000))
            self.boost_frac += (spool_target - self.boost_frac) * min(1, dt * 2.5)
        else:
            self.boost_frac = 0.0

        # --- temperaturas
        cool_target = 91.0 + 4.0 * self.throttle
        if self._overheat:
            cool_target = 112.0
        self.coolant += (cool_target - self.coolant) * min(1, dt * (0.05 if not self._overheat else 0.08))
        base_iat = AMBIENT_C + max(3.0, 9.0 - self.v_ms * 0.25)
        heat = p.get("intercooler_heat_rate", 0.0) * self.boost_frac * self.throttle
        self.iat += (heat - (self.iat - base_iat) * 0.08) * dt

        # --- cascabeleo (retardo de chispa)
        if p["knock_prone"] and self.throttle > 0.8 and self.boost_frac > 0.6:
            chance = 0.02 + max(0.0, self.iat - 40) * 0.012
            if random.random() < chance * dt * 10:
                self.knock_retard = min(8.0, self.knock_retard + random.uniform(2.5, 5.0))
        self.knock_retard = max(0.0, self.knock_retard - 2.0 * dt)

    # --------------------------------------------------------------- lectura
    def read(self) -> Dict[str, Optional[float]]:
        now = time.monotonic()
        dt = min(0.25, now - self._last)
        self._last = now
        # sub-pasos para que la integración sea estable aunque el loop se atrase
        steps = max(1, int(dt / 0.01))
        for _ in range(steps):
            self._step(dt / steps)

        p = self.p
        rpm = self.rpm
        if p.get("misfire_cyl") and self.mode == "idle" and self.v_ms < 1:
            if random.random() < 0.25:
                rpm -= random.uniform(30, 80)  # tirón del cilindro muerto
        rpm += random.uniform(-6, 6)

        # MAP
        if p["turbo"]:
            leak = p.get("boost_leak", 0) * max(0.0, (self.rpm - 3800) / 2000)
            boost_kpa = (p["boost_target_kpa"] - BARO_KPA) * self.boost_frac * (1 - leak)
            map_kpa = 30 + (BARO_KPA - 30) * min(1, self.throttle * 1.6) + max(0.0, boost_kpa)
        else:
            map_kpa = 30 + (98 - 30) * min(1.0, self.throttle * 1.5)
            if p["vacuum_leak_gs"]:
                map_kpa += 5 * (1 - self.throttle)
        map_kpa += random.uniform(-0.6, 0.6)

        # MAF real vs medido
        ve = 0.6 + 0.3 * min(1, self.throttle * 1.4)
        density = RHO_AIR * (map_kpa / BARO_KPA) * (298 / (273 + self.iat))
        maf_true = rpm / 120 * (self.disp_l / 1000) * ve * density * 1000
        maf_meas = max(0.5, maf_true * p["maf_scale"] - p["vacuum_leak_gs"])

        # Fuel trims (lazo cerrado salvo a fondo)
        osc = 3.2 * math.sin(self._t * 2 * math.pi * 1.1) + random.uniform(-0.8, 0.8)
        needed = (maf_true / max(0.5, maf_meas) - 1) * 100
        ltft = p["learned_ltft"]
        if self.throttle > 0.85:
            stft = 0.0  # lazo abierto (enriquecimiento de potencia)
        else:
            stft = max(-25.0, min(25.0, needed - ltft + osc))

        # Avance de chispa (PID 0E)
        if self.throttle < 0.08:
            timing = 10 + random.uniform(-1.5, 1.5)
        elif self.throttle > 0.8:
            timing = (6 if p["turbo"] else 16) + (rpm - 2000) / 450
        else:
            timing = 26 + (rpm - 1500) / 400 - self.throttle * 10
        timing -= self.knock_retard + max(0.0, self.iat - 45) * 0.15

        if p["turbo"]:
            load = min(100.0, 18 + 82 * min(1, self.throttle * 1.2) * (0.7 + 0.3 * self.boost_frac))
        else:
            load = min(100.0, map_kpa / 98 * 100 * 0.95)

        return {
            "rpm": round(max(0.0, rpm)),
            "speed_kph": round(self.v_ms * 3.6),  # OBD reporta enteros
            "coolant_temp_c": round(self.coolant, 1),
            "intake_temp_c": round(self.iat, 1),
            "stft_b1": round(stft, 1),
            "ltft_b1": round(ltft + random.uniform(-0.2, 0.2), 1),
            "map_kpa": round(map_kpa, 1),
            "maf_gs": round(maf_meas, 2),
            "throttle_pos": round(12 + self.throttle * 84, 1),
            "engine_load": round(load, 1),
            "timing_advance": round(timing, 1),
            "control_module_voltage": round(14.15 - 0.25 * self.throttle + random.uniform(-0.05, 0.05), 2),
            "_mode": self.mode,
            "_pull_phase": self.pull_phase,
            "_gear": self.gear,
        }
