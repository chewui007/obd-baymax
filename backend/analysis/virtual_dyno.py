"""Virtual Dyno: WHP y torque a partir de RPM/velocidad vs. tiempo (OBD-II PIDs 0C y 0D).

Física:
    v(t)     = RPM · π·D / (60 · relación_marcha · diferencial)   (más resolución que el PID 0D)
    a(t)     = dv/dt   (suavizado)
    F_rueda  = m_eff·a + ½·ρ·Cd·A·v² + Crr·m·g
    P_rueda  = F_rueda · v      ->  WHP = P / 745.7
    Torque   = WHP · 5252 / RPM  (lb-ft referido a RPM de motor, como un dyno de rodillos)

m_eff incluye la inercia rotacional (ruedas, flechas, volante): m·(1.04 + 0.0025·i²).
Haz el pull en calle plana, sin viento, una sola marcha, de ~2,000 RPM al corte.
"""
from __future__ import annotations

import math
from statistics import median
from typing import Dict, List, Optional

G = 9.81
RHO_AIR = 1.2
CRR = 0.012
DRIVER_KG = 80.0
DRIVETRAIN_LOSS = 0.15  # para estimar HP al cigüeñal


def _smooth(values: List[float], window: int) -> List[float]:
    if window <= 1:
        return values[:]
    half = window // 2
    out = []
    for i in range(len(values)):
        seg = values[max(0, i - half): i + half + 1]
        out.append(sum(seg) / len(seg))
    return out


def _detect_gear(samples: List[dict], ratios: List[float], final: float, tire_d: float) -> Optional[int]:
    pairs = [s["rpm"] / s["speed_kph"] for s in samples if (s.get("speed_kph") or 0) > 15 and s.get("rpm")]
    if not pairs or not ratios:
        return None
    observed = median(pairs)  # rpm por km/h
    best, best_err = None, 1e9
    for g, r in enumerate(ratios, start=1):
        expected = (r * final * 60) / (math.pi * tire_d * 3.6)
        err = abs(expected - observed) / expected
        if err < best_err:
            best, best_err = g, err
    return best if best_err < 0.12 else None


def _wot_window(samples: List[dict]) -> List[dict]:
    """Recorta al tramo a fondo con RPM subiendo."""
    wot = [s for s in samples if (s.get("throttle_pos") or 0) >= 75 and s.get("rpm")]
    if len(wot) < 8:
        wot = [s for s in samples if s.get("rpm")]
    # quedarse con el tramo más largo de RPM creciente
    best, cur = [], []
    for s in wot:
        if cur and (s["rpm"] < cur[-1]["rpm"] - 150 or s["t"] - cur[-1]["t"] > 1.0):
            if len(cur) > len(best):
                best = cur
            cur = []
        cur.append(s)
    if len(cur) > len(best):
        best = cur
    return best


def compute_dyno(samples: List[dict], vehicle: dict, gear: Optional[int] = None) -> Dict:
    ratios = vehicle.get("gear_ratios") or []
    final = float(vehicle.get("final_drive") or 0)
    tire_d = float(vehicle.get("tire_diameter_m") or 0)
    mass = float(vehicle.get("curb_weight_kg") or 0)
    cd = float(vehicle.get("drag_coef") or 0.33)
    area = float(vehicle.get("frontal_area_m2") or 2.3)

    if not (ratios and final and tire_d and mass):
        return {"ok": False, "error": "Faltan parámetros físicos del vehículo (peso, llanta, relaciones)."}

    window = _wot_window(samples)
    if len(window) < 8:
        return {"ok": False, "error": "Muy pocas muestras a fondo. Haz un pull más largo (2,000 RPM → corte)."}

    gear = gear or _detect_gear(window, ratios, final, tire_d)
    if not gear:
        return {"ok": False, "error": "No pude detectar la marcha. Indícala manualmente."}

    overall = ratios[gear - 1] * final
    m_total = mass + DRIVER_KG
    m_eff = m_total * (1.04 + 0.0025 * overall ** 2)

    t = [s["t"] for s in window]
    rpm = _smooth([float(s["rpm"]) for s in window], 3)
    v = [r * math.pi * tire_d / (60 * overall) for r in rpm]
    v_s = _smooth(v, 5)

    points = []
    for i in range(1, len(window) - 1):
        dt = t[i + 1] - t[i - 1]
        if dt <= 0:
            continue
        a = (v_s[i + 1] - v_s[i - 1]) / dt
        f = m_eff * a + 0.5 * RHO_AIR * cd * area * v_s[i] ** 2 + CRR * m_total * G
        whp = max(0.0, f * v_s[i] / 745.7)
        tq = whp * 5252 / rpm[i] if rpm[i] > 0 else 0.0
        points.append({"rpm": rpm[i], "whp": whp, "tq_lbft": tq})

    # binning cada 250 RPM para una curva limpia
    bins: Dict[int, List[dict]] = {}
    for p in points:
        bins.setdefault(int(round(p["rpm"] / 250.0) * 250), []).append(p)
    curve = []
    for b in sorted(bins):
        grp = bins[b]
        curve.append({
            "rpm": b,
            "whp": round(sum(x["whp"] for x in grp) / len(grp), 1),
            "tq_lbft": round(sum(x["tq_lbft"] for x in grp) / len(grp), 1),
        })
    # suavizado final ligero
    if len(curve) >= 5:
        sm_hp = _smooth([c["whp"] for c in curve], 3)
        sm_tq = _smooth([c["tq_lbft"] for c in curve], 3)
        for c, h, q in zip(curve, sm_hp, sm_tq):
            c["whp"], c["tq_lbft"] = round(h, 1), round(q, 1)

    if not curve:
        return {"ok": False, "error": "No se pudo calcular la curva."}

    peak_hp = max(curve, key=lambda c: c["whp"])
    peak_tq = max(curve, key=lambda c: c["tq_lbft"])
    duration = t[-1] - t[0]
    return {
        "ok": True,
        "gear": gear,
        "overall_ratio": round(overall, 3),
        "samples_used": len(window),
        "duration_s": round(duration, 2),
        "rpm_range": [int(rpm[0]), int(rpm[-1])],
        "speed_range_kph": [round(v[0] * 3.6), round(v[-1] * 3.6)],
        "curve": curve,
        "peak_whp": peak_hp["whp"],
        "peak_whp_rpm": peak_hp["rpm"],
        "peak_tq_lbft": peak_tq["tq_lbft"],
        "peak_tq_rpm": peak_tq["rpm"],
        "est_crank_hp": round(peak_hp["whp"] / (1 - DRIVETRAIN_LOSS), 1),
        "assumptions": {
            "mass_kg": m_total, "m_eff_kg": round(m_eff), "cd": cd, "frontal_area_m2": area,
            "crr": CRR, "drivetrain_loss": DRIVETRAIN_LOSS,
        },
    }
