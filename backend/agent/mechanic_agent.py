"""Baymax Mecánico: motor de diagnóstico sobre Ollama local + motor determinista de telemetría.

El usuario nunca explica qué carro es ni qué lecturas tiene: todo se arma aquí y se inyecta
en el System Prompt (vehículo, historial de garage.db, telemetría en vivo, DTCs + freeze frame).
Si Ollama aún no tiene modelo descargado, el motor determinista cruza los sensores en vivo,
el freeze frame, el historial en garage.db y la evidencia descargada de DuckDuckGo para
entregar un reporte técnico completo sin quedarse mudo.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from typing import AsyncIterator, Dict, List, Optional

import httpx

from backend.agent import rag_manuals, web_researcher
from backend.config import OLLAMA_MODEL, OLLAMA_URL, PREFERRED_MODELS

log = logging.getLogger("baymax.agent")

FIELD_LABELS = {
    "rpm": ("RPM", ""),
    "speed_kph": ("Velocidad", " km/h"),
    "coolant_temp_c": ("ECT", " °C"),
    "intake_temp_c": ("IAT", " °C"),
    "stft_b1": ("STFT B1", " %"),
    "ltft_b1": ("LTFT B1", " %"),
    "map_kpa": ("MAP", " kPa"),
    "maf_gs": ("MAF", " g/s"),
    "throttle_pos": ("TPS", " %"),
    "engine_load": ("LOAD", " %"),
    "timing_advance": ("IGN ADV", " °"),
    "control_module_voltage": ("VBAT", " V"),
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Busca en internet (foros técnicos, TSBs, reportes de taller) fallas reales y arreglos "
                "para un código DTC o síntoma en un motor específico."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Ej: 'Toyota 3RZ-FE P0171 vacuum leak fix'"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_manuals",
            "description": (
                "Busca en los manuales de taller PDF locales (especificaciones, torques, "
                "procedimientos de prueba, diagramas)."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
]


def fmt_frame(frame: Optional[dict]) -> str:
    if not frame:
        return "N/D"
    parts = []
    for k, (label, unit) in FIELD_LABELS.items():
        v = frame.get(k)
        if v is not None:
            if k in ("stft_b1", "ltft_b1"):
                parts.append(f"{label}: {v:+.1f}{unit}")
            else:
                parts.append(f"{label}: {v}{unit}")
    return " | ".join(parts) or "N/D"


def build_system_prompt(ctx: dict) -> str:
    v = ctx.get("vehicle") or {}
    lines = [
        "Eres BAYMAX L1, ingeniero de diagnóstico automotriz (ASE Master L1) y calibrador de motores.",
        "Hablas español técnico, directo y conciso. Cero emojis y cero relleno conversacional.",
        "REGLAS:",
        "- Basa tus conclusiones exclusivamente en las lecturas de sensores, Freeze Frame, historial de garage.db y fuentes técnicas.",
        "- Cruza parámetros entre sí (STFT+LTFT vs MAF vs MAP vs RPM vs IAT vs Avance) para aislar la causa raíz.",
        "- Ordena las hipótesis por probabilidad (%) y entrega un protocolo de verificación paso a paso con valores de referencia (multímetro, vacuómetro, prueba de humo, presión).",
        "- Cita las fuentes web como [1], [2]... cuando las utilices. Nunca inventes URLs.",
        "",
        "=== VEHÍCULO ACTIVO ===",
        f"{v.get('year')} {v.get('make')} {v.get('model')} | Motor: {v.get('engine_code')} | "
        f"Aspiración: {v.get('aspiration')} | Protocolo: {v.get('obd_protocol')} | VIN: {v.get('vin') or 'N/D'}",
        "",
        "=== INTERFAZ OBD ===",
        f"{ctx.get('connection') or 'DESCONECTADO'}",
        "",
        "=== TELEMETRÍA EN VIVO (MODO 01) ===",
        fmt_frame(ctx.get("live_frame")),
        "",
        "=== MEMORIA DE AVERÍAS (MODO 03/07 + FREEZE FRAME MODO 02) ===",
    ]
    dtcs = ctx.get("active_dtcs") or []
    if dtcs:
        for d in dtcs:
            lines.append(f"- {d['code']} [{d.get('status')}] {d.get('description', '')}")
            if d.get("freeze_frame"):
                lines.append(f"  Freeze Frame: {fmt_frame(d['freeze_frame'])}")
    else:
        lines.append("Sin DTCs activos.")
    lines += ["", "=== HISTORIAL DE SERVICIO EN GARAGE.DB ==="]
    hist = ctx.get("history") or []
    if hist:
        for h in hist[:8]:
            lines.append(f"- {h}")
    else:
        lines.append("Sin registros previos.")
    return "\n".join(lines)


def format_sources_block(sources: List[dict], manuals: List[dict], known: List[dict]) -> str:
    out = []
    if sources:
        out.append("=== EVIDENCIA WEB (EXTRACCIÓN EN VIVO) ===")
        for i, s in enumerate(sources, start=1):
            out.append(f"[{i}] {s.get('title')} ({s.get('domain')})")
            for p in s.get("passages", [])[:2]:
                out.append(f"    > {p}")
    if known:
        out.append("=== SOLUCIONES VERIFICADAS EN BASE LOCAL ===")
        for k in known:
            tag = "VERIFICADO EN FLOTA" if k.get("kind") == "verified" else "ARCHIVO WEB"
            out.append(f"- [{tag}] {k['text'][:400]}")
    if manuals:
        out.append("=== MANUALES DE TALLER (FSM PDF) ===")
        for m in manuals:
            out.append(f"- {m['file']} (p.{m['page']}): {m['text'][:500]}")
    return "\n".join(out)


class MechanicAgent:
    def __init__(self):
        self.base = OLLAMA_URL.rstrip("/")
        self.model: Optional[str] = None
        self._spawn_attempted = False

    @staticmethod
    def _find_ollama_bin() -> Optional[str]:
        for path in (shutil.which("ollama"), "/usr/local/bin/ollama", "/opt/homebrew/bin/ollama"):
            if path:
                return path
        return None

    async def _try_autostart_daemon(self) -> bool:
        if self._spawn_attempted:
            return False
        binary = self._find_ollama_bin()
        if not binary:
            return False
        self._spawn_attempted = True
        try:
            subprocess.Popen(
                [binary, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            await asyncio.sleep(1.0)
            return True
        except Exception as e:
            log.debug("No se pudo iniciar ollama serve automáticamente: %s", e)
            return False

    # ------------------------------------------------------------ estado
    async def status(self) -> Dict:
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=2.0) as c:
                    r = await c.get(f"{self.base}/api/tags")
                    r.raise_for_status()
                    names = [m["name"] for m in r.json().get("models", [])]
                    self.model = self._pick_model(names)
                    hint = None if self.model else f"ollama pull {OLLAMA_MODEL}"
                    return {
                        "online": True,
                        "model": self.model,
                        "models": names,
                        "recommended": OLLAMA_MODEL,
                        "hint": hint,
                    }
            except Exception:
                if attempt == 0 and await self._try_autostart_daemon():
                    continue
        return {
            "online": False,
            "model": None,
            "models": [],
            "recommended": OLLAMA_MODEL,
            "hint": "Ejecuta: ollama serve",
        }

    async def pull_model(self, model_name: Optional[str] = None) -> AsyncIterator[dict]:
        target = model_name or OLLAMA_MODEL
        await self._try_autostart_daemon()
        yield {"type": "status", "message": f"Descargando modelo {target} desde Ollama Registry..."}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(1800.0, connect=5.0)) as c:
                async with c.stream("POST", f"{self.base}/api/pull", json={"name": target, "stream": True}) as r:
                    if r.status_code != 200:
                        body = (await r.aread()).decode(errors="ignore")
                        yield {"type": "error", "message": f"Ollama HTTP {r.status_code}: {body[:200]}"}
                        return
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        total = data.get("total") or 0
                        completed = data.get("completed") or 0
                        pct = round((completed / total) * 100, 1) if total else None
                        yield {
                            "type": "progress",
                            "status": data.get("status", ""),
                            "pct": pct,
                        }
            await self.status()
            yield {"type": "done", "model": self.model or target}
        except Exception as e:
            yield {"type": "error", "message": str(e)}

    @staticmethod
    def _pick_model(names: List[str]) -> Optional[str]:
        if not names:
            return None
        for wanted in [OLLAMA_MODEL] + PREFERRED_MODELS:
            for n in names:
                if n == wanted or n.startswith(wanted + ":") or n.split(":")[0] == wanted:
                    return n
        embed_like = ("embed", "nomic", "bge", "minilm")
        chat = [n for n in names if not any(e in n for e in embed_like)]
        return chat[0] if chat else None

    # ------------------------------------------------------------ LLM core
    async def _stream_chat(self, messages: List[dict], tools: Optional[list] = None) -> AsyncIterator[dict]:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": 0.25, "num_ctx": 8192},
        }
        if tools:
            payload["tools"] = tools
        tool_calls = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=5.0)) as c:
            async with c.stream("POST", f"{self.base}/api/chat", json=payload) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode(errors="ignore")
                    raise RuntimeError(f"Ollama {r.status_code}: {body[:300]}")
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    msg = chunk.get("message") or {}
                    if msg.get("content"):
                        yield {"token": msg["content"]}
                    if msg.get("tool_calls"):
                        tool_calls.extend(msg["tool_calls"])
                    if chunk.get("done"):
                        break
        if tool_calls:
            yield {"tool_calls": tool_calls}

    async def _ensure_model(self) -> Optional[str]:
        if not self.model:
            st = await self.status()
            if not st["online"]:
                return "DAEMON_OFFLINE"
            if not self.model:
                return "NO_MODEL_INSTALLED"
        return None

    # ------------------------------------------------------------ modos
    async def diagnose(self, ctx: dict, dtc: Optional[dict], symptoms: Optional[str] = None) -> AsyncIterator[dict]:
        vehicle = ctx.get("vehicle") or {}
        code = (dtc or {}).get("code")
        sources: List[dict] = []
        query = None

        if code and ctx.get("internet", True):
            query = web_researcher.build_dtc_query(vehicle, code, symptoms)
            yield {"type": "status", "message": f"WEB-SEARCH // QUERY: {query}"}
            res = await web_researcher.research_dtc(vehicle, code, symptoms)
            sources, query = res["sources"], res["query"]
            if sources:
                rag_manuals.store_web_solutions(vehicle, code, sources)
        elif symptoms and ctx.get("internet", True):
            query = f"{vehicle.get('make')} {vehicle.get('engine_code')} {symptoms} causes fix"
            yield {"type": "status", "message": f"WEB-SEARCH // QUERY: {query}"}
            sources = await web_researcher.research(query, [vehicle.get("engine_code", ""), vehicle.get("make", "")])

        rag_q = f"{vehicle.get('make')} {vehicle.get('model')} {vehicle.get('engine_code')} {code or ''} {symptoms or ''}"
        yield {"type": "status", "message": "RAG-INDEX // CONSULTANDO MANUALES FSM Y BASE LOCAL"}
        manuals = rag_manuals.search_manuals(rag_q, k=3)
        known = rag_manuals.search_known_solutions(rag_q, dtc_code=code, k=3)

        yield {
            "type": "sources",
            "query": query,
            "sources": sources,
            "manuals": [{"file": m["file"], "page": m["page"], "text": m["text"][:240]} for m in manuals],
            "known": [{"title": k.get("title"), "kind": k.get("kind"), "text": k["text"][:240]} for k in known],
        }

        task = []
        if dtc:
            task.append(f"Diagnostica el código {code} ({dtc.get('description', '')}) [{dtc.get('status')}].")
            if dtc.get("freeze_frame"):
                task.append(f"Freeze Frame Modo 02: {fmt_frame(dtc['freeze_frame'])}")
        if symptoms:
            task.append(f"Síntoma reportado en taller: {symptoms}")
        task.append(
            "Estructura obligatoria del dictamen:\n"
            "## 1. ANÁLISIS DE TELEMETRÍA Y FREEZE FRAME\n"
            "## 2. MATRIZ DE HIPÓTESIS (ORDENADAS POR PROBABILIDAD %)\n"
            "## 3. PROTOCOLO DE VERIFICACIÓN PASO A PASO (VALORES DE PRUEBA)\n"
            "## 4. REFACCIONES Y HERRAMIENTA REQUERIDA"
        )
        evidence = format_sources_block(sources, manuals, known)
        messages = [
            {"role": "system", "content": build_system_prompt(ctx)},
            {"role": "user", "content": "\n".join(task) + ("\n\n" + evidence if evidence else "")},
        ]
        async for ev in self._run(messages, sources, use_tools=False, ctx=ctx, dtc=dtc, symptoms=symptoms):
            yield ev

    async def chat(self, ctx: dict, history: List[dict], message: str) -> AsyncIterator[dict]:
        messages = [
            {
                "role": "system",
                "content": build_system_prompt(ctx)
                + "\n\nTienes herramientas: web_search (internet) y search_manuals (PDFs locales). "
                "Úsalas cuando necesites especificaciones o reportes adicionales.",
            }
        ]
        messages += [m for m in history[-10:] if m.get("role") in ("user", "assistant")]
        messages.append({"role": "user", "content": message})
        async for ev in self._run(messages, [], use_tools=ctx.get("internet", True), ctx=ctx, symptoms=message):
            yield ev

    async def analyze_run(self, ctx: dict, run_type: str, result: dict) -> AsyncIterator[dict]:
        if run_type == "dyno":
            curve = ", ".join(f"{c['rpm']}rpm={c['whp']}whp/{c['tq_lbft']}lbft" for c in result.get("curve", []))
            body = (
                f"Analiza este pull de Virtual Dyno en {result.get('gear')}ª marcha: pico {result.get('peak_whp')} WHP "
                f"@ {result.get('peak_whp_rpm')} RPM, {result.get('peak_tq_lbft')} lb-ft @ {result.get('peak_tq_rpm')} RPM. "
                f"Curva: {curve}. Resumen de sensores durante el pull: {json.dumps(result.get('sensor_summary', {}))}. "
                "Evalúa coherencia con curva OEM, caídas de par/potencia, heat soak, mezcla y avance."
            )
        else:
            checks = "\n".join(
                f"- {c['name']}: {c['status']} | {c['value']} | {c['detail']}" for c in result.get("checks", [])
            )
            body = (
                f"Resultado de Prueba Pre-Stage: veredicto {result.get('verdict')} "
                f"(score {result.get('score')}/100).\n{checks}\n"
                "Explica el impacto mecánico de cada hallazgo ante una reprogramación Stage 1/2 y el orden de corrección."
            )
        messages = [{"role": "system", "content": build_system_prompt(ctx)}, {"role": "user", "content": body}]
        async for ev in self._run(messages, [], use_tools=False, ctx=ctx, run_result=result):
            yield ev

    async def _run(
        self,
        messages: List[dict],
        sources: List[dict],
        use_tools: bool,
        ctx: Optional[dict] = None,
        dtc: Optional[dict] = None,
        symptoms: Optional[str] = None,
        run_result: Optional[dict] = None,
    ) -> AsyncIterator[dict]:
        err = await self._ensure_model()
        if err:
            text = self._engineering_fallback(err, ctx or {}, dtc, symptoms, sources, run_result)
            # Emitir en bloques para mantener experiencia fluida en consola
            for chunk in [text[i : i + 120] for i in range(0, len(text), 120)]:
                yield {"type": "token", "text": chunk}
                await asyncio.sleep(0.015)
            yield {"type": "final", "text": text, "sources": sources, "offline": True, "reason": err}
            return

        yield {"type": "model", "model": self.model}
        full = ""
        try:
            for _round in range(3):
                calls = []
                async for part in self._stream_chat(messages, TOOLS if use_tools else None):
                    if "token" in part:
                        full += part["token"]
                        yield {"type": "token", "text": part["token"]}
                    elif "tool_calls" in part:
                        calls = part["tool_calls"]
                if not calls:
                    break
                messages.append({"role": "assistant", "content": "", "tool_calls": calls})
                for call in calls:
                    fn = call.get("function", {})
                    name, args = fn.get("name"), fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"query": args}
                    q = args.get("query", "")
                    if name == "web_search":
                        yield {"type": "status", "message": f"WEB-SEARCH // QUERY: {q}"}
                        found = await web_researcher.research(q, q.split()[:4])
                        sources.extend(found)
                        yield {"type": "sources", "query": q, "sources": found, "manuals": [], "known": []}
                        content = format_sources_block(found, [], []) or "Sin resultados."
                    elif name == "search_manuals":
                        yield {"type": "status", "message": f"RAG-INDEX // QUERY: {q}"}
                        found_m = rag_manuals.search_manuals(q)
                        yield {
                            "type": "sources",
                            "query": q,
                            "sources": [],
                            "known": [],
                            "manuals": [
                                {"file": m["file"], "page": m["page"], "text": m["text"][:240]} for m in found_m
                            ],
                        }
                        content = format_sources_block([], found_m, []) or "No hay manuales indexados."
                    else:
                        content = f"Herramienta desconocida: {name}"
                    messages.append({"role": "tool", "content": content, "tool_name": name})
        except Exception as e:
            log.exception("Error con Ollama")
            msg = f"\n\n[ERROR OLLAMA: {e}]"
            full += msg
            yield {"type": "token", "text": msg}
        yield {"type": "final", "text": full, "sources": sources, "offline": False}

    # ------------------------------------------------------------ Motor determinista de respaldo
    @staticmethod
    def _engineering_fallback(
        err_code: str,
        ctx: dict,
        dtc: Optional[dict],
        symptoms: Optional[str],
        sources: List[dict],
        run_result: Optional[dict] = None,
    ) -> str:
        v = ctx.get("vehicle") or {}
        live = ctx.get("live_frame") or {}
        active_dtcs = ctx.get("active_dtcs") or []
        target_dtc = dtc or (active_dtcs[0] if active_dtcs else None)
        ff = (target_dtc or {}).get("freeze_frame") or live
        code = (target_dtc or {}).get("code") or "SÍNTOMA"
        engine_code = v.get("engine_code") or "MOTOR"
        veh_name = f"{v.get('year', '')} {v.get('make', '')} {v.get('model', '')} [{engine_code}]".strip()

        note = (
            f"`[MODO TELEMETRÍA DETERMINISTA + WEB RAG — Modelo LLM local ({OLLAMA_MODEL}) aún no descargado en Ollama]`"
            if err_code == "NO_MODEL_INSTALLED"
            else "`[MODO TELEMETRÍA DETERMINISTA + WEB RAG — Demonio Ollama sin respuesta en localhost:11434]`"
        )

        lines = [note, ""]

        if run_result:
            if "peak_whp" in run_result:
                lines += [
                    f"## 1. DICTAMEN DE VIRTUAL DYNO — {veh_name}",
                    f"- **Potencia medida a la rueda**: `{run_result.get('peak_whp')} WHP @ {run_result.get('peak_whp_rpm')} RPM`",
                    f"- **Par motor máximo**: `{run_result.get('peak_tq_lbft')} lb-ft @ {run_result.get('peak_tq_rpm')} RPM`",
                    f"- **Potencia estimada al cigüeñal (15% pérdida)**: `{run_result.get('est_crank_hp')} BHP`",
                    f"- **Ventana de sensores en WOT**: `{json.dumps(run_result.get('sensor_summary', {}))}`",
                ]
                return "\n".join(lines)
            if "verdict" in run_result:
                lines += [
                    f"## 1. DICTAMEN PRE-STAGE — {veh_name}",
                    f"- **Veredicto**: `{run_result.get('verdict')}` (`Score {run_result.get('score')}/100`)",
                    f"- **Resumen**: {run_result.get('summary')}",
                ]
                for c in run_result.get("checks", []):
                    if c.get("status") != "OK":
                        lines.append(f"- **[{c['status']}] {c['name']}** ({c['value']}): {c['detail']} -> *{c.get('fix') or ''}*")
                return "\n".join(lines)

        stft = ff.get("stft_b1") or 0.0
        ltft = ff.get("ltft_b1") or 0.0
        total_trim = stft + ltft
        rpm = ff.get("rpm") or 0
        map_kpa = ff.get("map_kpa") or 0
        maf_gs = ff.get("maf_gs") or 0
        iat = ff.get("intake_temp_c") or 0
        ect = ff.get("coolant_temp_c") or 0
        timing = ff.get("timing_advance") or 0

        lines += [
            f"## 1. ANÁLISIS DE TELEMETRÍA Y FREEZE FRAME — {veh_name}",
            f"- **Código / Evento Evaluado**: `{code}` {(target_dtc or {}).get('description', '')}",
            f"- **Instantánea de Sensores**: `RPM: {rpm} | ECT: {ect}°C | IAT: {iat}°C | MAP: {map_kpa} kPa | MAF: {maf_gs} g/s | IGN: {timing}°`",
            f"- **Correctores de Combustible**: `STFT B1: {stft:+.1f}% | LTFT B1: {ltft:+.1f}% | Total Trim: {total_trim:+.1f}%`",
        ]

        if code == "P0171" or total_trim > 12.0:
            lines += [
                "- **Diagnóstico de Telemetría**: La ECU está inyectando **`"
                + f"{total_trim:+.1f}%"
                + "`** de combustible adicional para mantener estequiometría (λ=1.0). "
                + (
                    f"Con `{rpm} RPM`, `MAP {map_kpa} kPa` y lectura baja de `MAF ({maf_gs} g/s)`, el patrón confirma **aire no medido entrando después del sensor MAF (fuga de vacío)** o sub-lectura del filamento del MAF."
                    if rpm < 1200
                    else "El empobrecimiento ocurre bajo carga, apuntando a restricción en suministro de combustible o caída de presión en riel."
                ),
                "",
                f"## 2. MATRIZ DE HIPÓTESIS PARA {engine_code}",
                "1. **[55%] Fuga de vacío post-MAF / mangueras de admisión o PCV agrietadas**: En el motor `"
                + engine_code
                + "` las mangueras de vacío detrás del cuerpo de aceleración y el codo de admisión se resecan por temperatura, metiendo aire falso en ralentí.",
                "2. **[30%] Sensor MAF contaminado (filamento con película de aceite/polvo)**: Sub-reporta gramos/segundo (`"
                + f"{maf_gs} g/s"
                + "`) obligando a la ECU a subir LTFT por encima de +15%.",
                "3. **[15%] Baja presión de riel de combustible / inyector parcialmente obstruido o sensor O2 B1S1 lento**.",
                "",
                "## 3. PROTOCOLO DE VERIFICACIÓN PASO A PASO",
                "1. **Prueba dinámica de Fuel Trims a 2,500 RPM sin carga**: Mantén el motor a `2,500 RPM` en neutral por 20 s observando el osciloscopio. Si `STFT + LTFT` baja hacia `0% .. +5%`, queda **100% confirmada una fuga de vacío**.",
                "2. **Inspección y prueba de humo en admisión**: Revisa fuelle entre MAF y mariposa, manguera de válvula PCV y puertos de vacío del múltiple.",
                "3. **Limpieza de MAF**: Desmonta el sensor MAF y limpia el filamento con limpiador específico de MAF (cero residuos). Verifica que en ralentí caliente a ~750 RPM el MAF marque `~2.7 - 3.5 g/s`.",
            ]
        elif code == "P0299" or ("TURBO" in (v.get("aspiration") or "") and map_kpa > 120):
            lines += [
                f"- **Diagnóstico de Telemetría**: Bajo demanda de carga (`RPM {rpm}`), la presión absoluta del múltiple alcanza `{map_kpa} kPa` con temperatura de admisión `IAT {iat}°C` y avance reducido a `{timing}°`. Hay pérdida de presión de sobrealimentación (underboost) acompañada de saturación térmica en el intercooler.",
                "",
                f"## 2. MATRIZ DE HIPÓTESIS PARA {engine_code}",
                "1. **[50%] Fuga de presión en circuito de carga (mangueras de intercooler / o-rings de acople rápido / Diverter Valve)**: Típico en `"
                + engine_code
                + "`; el diafragma de la válvula de recirculación (DV) o el tubo de carga pierde sello por encima de 10 psi.",
                "2. **[30%] Solenoide de control de boost (N75 / transductor de vacío) o línea de vacío de wastegate degradada**.",
                "3. **[20%] Juego axial/radial en varilla de actuador de wastegate del turbocargador**.",
                "",
                "## 3. PROTOCOLO DE VERIFICACIÓN PASO A PASO",
                "1. **Boost Leak Test (Prueba de presurización)**: Presuriza la admisión post-turbo a `15 psi (103 kPa manométricos)` con motor apagado y escucha fugas en intercooler, acoples y cuerpo de aceleración.",
                "2. **Inspección de Diverter Valve (válvula de alivio)**: Revisa que el diafragma o pistón no esté perforado.",
                "3. **Verificación de actuador de Wastegate**: Comprueba que la varilla cierre firme y mantenga vacío/presión sin caer.",
            ]
        elif code.startswith("P030"):
            cyl = code[-1] if code[-1].isdigit() and code != "P0300" else "múltiple"
            lines += [
                f"- **Diagnóstico de Telemetría**: Fallo de encendido detectado en cilindro `{cyl}` con `RPM {rpm}`, `MAP {map_kpa} kPa`, `STFT {stft:+.1f}%`.",
                "",
                f"## 2. MATRIZ DE HIPÓTESIS PARA {engine_code}",
                f"1. **[55%] Bobina de encendido (COP) o bujía del cilindro {cyl} con fuga dieléctrica**.",
                f"2. **[30%] Inyector del cilindro {cyl} sucio / desbalanceado o conector sulfatado**.",
                f"3. **[15%] Baja compresión mecánica o desajuste de punterías/válvulas en cilindro {cyl}**.",
                "",
                "## 3. PROTOCOLO DE VERIFICACIÓN PASO A PASO",
                f"1. **Intercambio de bobina (Swap Test)**: Intercambia la bobina del cilindro `{cyl}` con la del cilindro `#1`, borra DTCs y verifica si el código migra a `P0301`.",
                f"2. **Inspección de bujía del cilindro {cyl}**: Revisa holgura de electrodo, porcelana fisurada o contaminación por aceite/combustible.",
                "3. **Prueba de compresión en seco/húmedo**: Verifica que no exista variación mayor a `10-15%` entre cilindros.",
            ]
        else:
            lines += [
                f"- **Síntoma / Consulta**: `{symptoms or code}`",
                "",
                "## 2. PROTOCOLO TÉCNICO RECOMENDADO",
                "1. Monitorea en el osciloscopio la correlación entre `STFT/LTFT` y `RPM/MAP` pasando de ralentí (750 RPM) a carga parcial (2,500 RPM).",
                "2. Revisa las fuentes técnicas extraídas abajo para este código de motor específico.",
            ]

        hist = ctx.get("history") or []
        if hist:
            lines += ["", "## 4. CORRELACIÓN CON HISTORIAL EN GARAGE.DB"]
            for h in hist[:3]:
                lines.append(f"- `{h}`")

        if sources:
            lines += ["", "## 5. EVIDENCIA TÉCNICA EXTRAÍDA DE LA WEB"]
            for i, s in enumerate(sources, start=1):
                lines.append(f"- **[{i}] {s.get('title')}** (`{s.get('domain')}`)")
                for p in s.get("passages", [])[:1]:
                    lines.append(f"  > {p[:320]}")

        return "\n".join(lines)


agent = MechanicAgent()
