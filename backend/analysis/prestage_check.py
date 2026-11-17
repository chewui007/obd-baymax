"""Validador Pre-Stage: ¿el motor está sano para una repro Stage 1/2?

Analiza un log de estrés (idealmente: 1 min en mínimo caliente + un pull a fondo) y revisa:
fuel trims, retardo de chispa por cascabeleo (PID 0E), heat soak (IAT), boost/MAP,
temperatura de refrigerante, voltaje y DTCs activos.
"""
from __future__ import annotations

from statistics import mean
from typing import Dict, List, Optional

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def _vals(samples, key, cond=None):
    return [s[key] for s in samples if s.get(key) is not None and (cond is None or cond(s))]


def _check(name, status, value, detail, fix=None):
    return {"name": name, "status": status, "value": value, "detail": detail, "fix": fix}


def _knock_events(wot: List[dict]) -> List[dict]:
    """Caídas de avance ≥3° mientras las RPM suben a fondo = retardo por cascabeleo."""
    events = []
    for prev, cur in zip(wot, wot[1:]):
        if prev.get("timing_advance") is None or cur.get("timing_advance") is None:
            continue
        drop = prev["timing_advance"] - cur["timing_advance"]
        if drop >= 3 and cur["rpm"] >= prev["rpm"] - 50:
            events.append({"rpm": cur["rpm"], "drop_deg": round(drop, 1), "iat": cur.get("intake_temp_c")})
    return events


def run_prestage_check(samples: List[dict], vehicle: dict, active_dtcs: Optional[List[dict]] = None) -> Dict:
    turbo = (vehicle.get("aspiration") or "NA").upper() == "TURBO"
    checks = []

    idle = [s for s in samples if (s.get("throttle_pos") or 100) < 20 and 500 < (s.get("rpm") or 0) < 1100]
    cruise = [s for s in samples if 20 <= (s.get("throttle_pos") or 0) < 75]
    wot = [s for s in samples if (s.get("throttle_pos") or 0) >= 75 and (s.get("rpm") or 0) > 1800]

    # 1) DTCs
    dtcs = [d for d in (active_dtcs or []) if d.get("status") in ("ACTIVE", "PENDING")]
    if dtcs:
        codes = ", ".join(d["code"] for d in dtcs)
        checks.append(_check("Códigos de falla", FAIL, codes,
                             "Hay DTCs activos/pendientes. Ninguna repro sobre un motor con fallas.",
                             "Diagnostica y repara cada código antes de cualquier Stage."))
    else:
        checks.append(_check("Códigos de falla", OK, "Ninguno", "Sin DTCs activos ni pendientes."))

    # 2) Fuel trims en lazo cerrado (mínimo + crucero)
    closed = idle + cruise
    if closed:
        total = [s["stft_b1"] + s["ltft_b1"] for s in closed if s.get("stft_b1") is not None and s.get("ltft_b1") is not None]
        if total:
            avg = mean(total)
            idle_tot = [s["stft_b1"] + s["ltft_b1"] for s in idle if s.get("stft_b1") is not None and s.get("ltft_b1") is not None]
            cruise_tot = [s["stft_b1"] + s["ltft_b1"] for s in cruise if s.get("stft_b1") is not None and s.get("ltft_b1") is not None]
            pattern = ""
            if idle_tot and cruise_tot and mean(idle_tot) - mean(cruise_tot) > 6:
                pattern = " Patrón: mucho peor en mínimo que con carga → típico de FUGA DE VACÍO."
            elif idle_tot and cruise_tot and mean(cruise_tot) - mean(idle_tot) > 6:
                pattern = " Patrón: empeora con carga → típico de ENTREGA DE COMBUSTIBLE (bomba/filtro/inyectores)."
            st = FAIL if abs(avg) > 15 else WARN if abs(avg) > 8 else OK
            checks.append(_check(
                "Fuel trims (STFT+LTFT)", st, f"{avg:+.1f}%",
                ("Mezcla compensada fuera de rango." if st != OK else "Mezcla en rango.") + pattern,
                "Prueba de humo para fugas de vacío/PCV, limpia MAF, revisa presión de combustible." if st != OK else None,
            ))
    else:
        checks.append(_check("Fuel trims (STFT+LTFT)", WARN, "Sin datos", "Graba al menos 30 s en mínimo caliente."))

    # 3) Retardo de chispa / cascabeleo
    if wot:
        events = _knock_events(wot)
        ta = _vals(wot, "timing_advance")
        min_ta = min(ta) if ta else None
        st = FAIL if len(events) >= 3 else WARN if events else OK
        detail = f"{len(events)} eventos de retardo ≥3° durante el pull."
        if events:
            worst = max(events, key=lambda e: e["drop_deg"])
            detail += f" Peor: -{worst['drop_deg']}° a {int(worst['rpm'])} RPM (IAT {worst['iat']} °C)."
        checks.append(_check(
            "Avance de chispa / cascabeleo (PID 0E)", st,
            f"mín {min_ta:.1f}°" if min_ta is not None else "n/d", detail,
            "Usa gasolina de mayor octanaje, revisa bujías (rango térmico), carbonización (inyección directa) "
            "y temperatura de admisión ANTES de pedir más avance/boost." if st != OK else None,
        ))
    else:
        checks.append(_check("Avance de chispa / cascabeleo (PID 0E)", WARN, "Sin pull",
                             "No hay tramo a fondo (>75% acelerador) en el log."))

    # 4) Heat soak (IAT)
    iat_all = _vals(samples, "intake_temp_c")
    iat_wot = _vals(wot, "intake_temp_c")
    if iat_wot:
        rise = iat_wot[-1] - iat_wot[0]
        peak = max(iat_wot)
        lim_rise, lim_peak = (8, 55) if turbo else (6, 50)
        st = FAIL if (rise > lim_rise * 1.6 or peak > lim_peak + 10) else WARN if (rise > lim_rise or peak > lim_peak) else OK
        checks.append(_check(
            "Temperatura de admisión (IAT)", st, f"+{rise:.1f} °C (pico {peak:.0f} °C)",
            "Subida de IAT durante el pull." + (" El intercooler se satura (heat soak)." if turbo and st != OK else ""),
            ("Intercooler de mayor capacidad / revisar que el radiador del IC no esté tapado." if turbo
             else "Revisa toma de aire (que no jale aire caliente del cofre).") if st != OK else None,
        ))
    elif iat_all:
        checks.append(_check("Temperatura de admisión (IAT)", OK, f"{max(iat_all):.0f} °C", "Sin pull; IAT en reposo."))

    # 5) Boost / MAP
    map_wot = _vals(wot, "map_kpa")
    if map_wot:
        peak = max(map_wot)
        if turbo:
            tail = [s["map_kpa"] for s in wot if s.get("map_kpa") is not None][-max(3, len(map_wot) // 5):]
            taper = peak - mean(tail)
            boost_psi = (peak - 101.3) * 0.145
            st = FAIL if taper > 25 else WARN if taper > 12 else OK
            checks.append(_check(
                "Boost (MAP)", st, f"pico {peak:.0f} kPa abs (~{boost_psi:.1f} psi), cae {taper:.0f} kPa al final",
                "El boost se cae en altas RPM." if st != OK else "Boost estable hasta el corte.",
                "Prueba de presión (boost leak test) a mangueras del intercooler, abrazaderas, diverter valve y "
                "actuador de wastegate. Un Stage 1 sobre una fuga = turbo sobre-revolucionado." if st != OK else None,
            ))
        else:
            st = FAIL if peak < 85 else WARN if peak < 92 else OK
            checks.append(_check(
                "MAP a fondo (NA)", st, f"{peak:.0f} kPa",
                "En aspirado el MAP a fondo debe acercarse a la presión atmosférica.",
                "Revisa filtro de aire, cuerpo de aceleración y que la mariposa abra 100%." if st != OK else None,
            ))

    # 6) Refrigerante
    ect = _vals(samples, "coolant_temp_c")
    if ect:
        mx = max(ect)
        st = FAIL if mx > 105 else WARN if mx > 100 or mx < 80 else OK
        checks.append(_check("Temperatura de refrigerante", st, f"máx {mx:.0f} °C",
                             "Fuera de rango de operación." if st != OK else "Operación normal.",
                             "Termostato, ventilador, nivel de anticongelante, bomba de agua." if st != OK else None))

    # 7) Voltaje
    volt = _vals(samples, "control_module_voltage", lambda s: (s.get("rpm") or 0) > 600)
    if volt:
        mn = min(volt)
        st = FAIL if mn < 12.8 else WARN if mn < 13.3 else OK
        checks.append(_check("Voltaje de carga", st, f"mín {mn:.2f} V",
                             "Alternador no sostiene carga." if st != OK else "Carga correcta.",
                             "Prueba alternador, batería y tierras." if st != OK else None))

    fails = sum(c["status"] == FAIL for c in checks)
    warns = sum(c["status"] == WARN for c in checks)
    score = max(0, 100 - fails * 25 - warns * 8)
    verdict = "NO-GO" if fails else "PRECAUCIÓN" if warns else "GO"
    msg = {
        "NO-GO": "NO le metas Stage todavía: hay fallas que con más boost/avance pueden romper el motor.",
        "PRECAUCIÓN": "Apto con reservas: corrige las advertencias antes de pedir el mapa.",
        "GO": "Motor sano para Stage 1. Guarda este log como línea base para comparar después.",
    }[verdict]
    return {
        "ok": True,
        "verdict": verdict,
        "score": score,
        "summary": msg,
        "checks": checks,
        "counts": {"idle": len(idle), "cruise": len(cruise), "wot": len(wot), "total": len(samples)},
        "knock_events": _knock_events(wot) if wot else [],
    }
