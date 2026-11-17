"""Hub de telemetría en tiempo real.

- Loop a TELEMETRY_HZ (10 Hz) que lee la fuente activa (ELM327 real o simulador).
- Broadcast por WebSocket a N clientes (dashboard, segunda pantalla, tablet en el taller).
- Muestreo a SQLite (telemetry_snapshots) cada SNAPSHOT_EVERY_S.
- Poll de DTCs + persistencia en dtc_records.
- Grabación de pulls (Virtual Dyno / Pre-Stage) y guardado en performance_runs.
- Monitoreo de Internet y Ollama para las pills de estado.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Set

from fastapi import WebSocket
from sqlmodel import Session, select

from backend.agent import web_researcher
from backend.agent.mechanic_agent import agent
from backend.analysis.prestage_check import run_prestage_check
from backend.analysis.virtual_dyno import compute_dyno
from backend.config import SNAPSHOT_EVERY_S, TELEMETRY_HZ
from backend.database import engine, load_vehicle_dict
from backend.hardware.base import FRAME_FIELDS, TelemetrySource
from backend.hardware.obd_mock import OBDMock
from backend.hardware.port_scanner import MOCK_PORT
from backend.models import (
    DiagnosticLog,
    DiagnosticSession,
    DtcRecord,
    PerformanceRun,
    TelemetrySnapshot,
)

log = logging.getLogger("baymax.hub")

BARO_KPA = 101.3
DTC_POLL_S = 5.0
STATUS_POLL_S = 15.0
OVERHEAT_C = 102.0


class TelemetryHub:
    def __init__(self) -> None:
        self.clients: Set[WebSocket] = set()
        self.source: Optional[TelemetrySource] = None
        self.vehicle: Optional[dict] = None
        self.session_id: Optional[int] = None
        self.conn_info: Optional[dict] = None
        self.connecting = False
        self.last_error: Optional[str] = None

        self.latest: Optional[dict] = None
        self.history: Deque[dict] = deque(maxlen=int(TELEMETRY_HZ * 60))  # 60 s de buffer
        self.dtcs: List[dict] = []
        self.freeze_frame: Dict[str, Any] = {}
        self._dtc_record_ids: Dict[str, int] = {}

        self.status: Dict[str, Any] = {"internet": None, "ollama": {"online": False, "model": None}}

        # grabación de corridas
        self.recording: Optional[dict] = None
        self.last_run: Optional[dict] = None

        self._io_lock = asyncio.Lock()
        self._tasks: List[asyncio.Task] = []
        self._last_snapshot = 0.0
        self._last_dtc_poll = 0.0
        self._t0 = time.monotonic()

    # ================================================================ ciclo de vida
    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._telemetry_loop(), name="telemetry"),
            asyncio.create_task(self._status_loop(), name="status"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.disconnect()

    # ================================================================ websockets
    async def register(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        await ws.send_json({"type": "hello", **self.snapshot_state()})

    def unregister(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, msg: dict) -> None:
        if not self.clients:
            return
        data = json.dumps(msg, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await asyncio.wait_for(ws.send_text(data), timeout=1.0)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def snapshot_state(self) -> dict:
        return {
            "vehicle": self.vehicle,
            "connection": self.connection_state(),
            "status": self.status,
            "dtcs": self.dtcs,
            "freeze_frame": self.freeze_frame,
            "recording": self._recording_public(),
            "last_run": self.last_run,
            "hz": TELEMETRY_HZ,
        }

    def connection_state(self) -> dict:
        connected = bool(self.source and self.source.is_connected())
        return {
            "connected": connected,
            "connecting": self.connecting,
            "session_id": self.session_id,
            "info": self.conn_info if connected else None,
            "error": self.last_error,
        }

    def _recording_public(self) -> Optional[dict]:
        if not self.recording:
            return None
        r = self.recording
        return {"run_type": r["run_type"], "gear": r.get("gear"), "samples": len(r["samples"]),
                "elapsed_s": round(time.monotonic() - r["started_mono"], 1), "phase": r.get("phase")}

    # ================================================================ vehículo / conexión
    async def select_vehicle(self, vehicle_id: int) -> dict:
        v = load_vehicle_dict(vehicle_id)
        if not v:
            raise ValueError(f"Vehículo {vehicle_id} no existe")
        was_connected = bool(self.source and self.source.is_connected())
        port = self.source.port if self.source else None
        self.vehicle = v
        if was_connected and port:
            await self.connect(port, vehicle_id)
        else:
            await self.broadcast({"type": "vehicle", "vehicle": v})
        return v

    async def connect(self, port: str, vehicle_id: Optional[int] = None) -> dict:
        if vehicle_id:
            v = load_vehicle_dict(vehicle_id)
            if not v:
                raise ValueError(f"Vehículo {vehicle_id} no existe")
            self.vehicle = v
        if not self.vehicle:
            raise ValueError("Selecciona un vehículo primero")

        await self.disconnect(broadcast=False)
        self.connecting, self.last_error = True, None
        await self.broadcast({"type": "connection", "connection": self.connection_state(), "vehicle": self.vehicle})

        if port == MOCK_PORT:
            src: TelemetrySource = OBDMock(self.vehicle)
        else:
            from backend.hardware.obd_reader import OBDReader  # import perezoso (python-OBD es pesado)
            src = OBDReader(port, vehicle=self.vehicle)

        try:
            async with self._io_lock:
                info = await asyncio.to_thread(src.connect)
            if not src.is_connected():
                raise RuntimeError((info or {}).get("error") or "El adaptador no respondió")
        except Exception as e:
            self.connecting = False
            self.last_error = str(e)
            log.warning("Conexión fallida en %s: %s", port, e)
            await self.broadcast({"type": "connection", "connection": self.connection_state()})
            raise RuntimeError(f"No se pudo conectar en {port}: {e}") from e

        self.source = src
        self.conn_info = info or src.info()
        self.connecting = False
        self.history.clear()
        self._dtc_record_ids = {}

        with Session(engine) as s:
            sess = DiagnosticSession(vehicle_id=self.vehicle["id"], connection_port=port)
            s.add(sess)
            s.commit()
            s.refresh(sess)
            self.session_id = sess.id

        await self.refresh_dtcs(force_broadcast=True)
        await self.broadcast({"type": "connection", "connection": self.connection_state(), "vehicle": self.vehicle})
        return self.connection_state()

    async def disconnect(self, broadcast: bool = True) -> None:
        src, self.source = self.source, None
        self.recording = None
        if src:
            try:
                async with self._io_lock:
                    await asyncio.to_thread(src.close)
            except Exception:
                log.exception("Error cerrando la fuente")
        if self.session_id:
            with Session(engine) as s:
                sess = s.get(DiagnosticSession, self.session_id)
                if sess and not sess.ended_at:
                    sess.ended_at = datetime.now(timezone.utc)
                    s.add(sess)
                    s.commit()
        self.session_id = None
        self.conn_info = None
        self.latest = None
        self.dtcs, self.freeze_frame = [], {}
        if broadcast:
            await self.broadcast({"type": "connection", "connection": self.connection_state()})
            await self.broadcast({"type": "dtcs", "dtcs": [], "freeze_frame": {}})

    # ================================================================ loop 10 Hz
    async def _telemetry_loop(self) -> None:
        period = 1.0 / TELEMETRY_HZ
        next_tick = time.monotonic()
        while True:
            next_tick += period
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Error en tick de telemetría")
                self.last_error = str(e)
            delay = next_tick - time.monotonic()
            if delay < -1.0:  # nos atrasamos mucho (cable lento): reajustar
                next_tick = time.monotonic()
                delay = 0
            await asyncio.sleep(max(0.0, delay))

    async def _tick(self) -> None:
        src = self.source
        if not src or not src.is_connected():
            return
        async with self._io_lock:
            raw = await asyncio.to_thread(src.read)
        now = time.monotonic()
        frame = {k: raw.get(k) for k in FRAME_FIELDS}
        frame["t"] = round(now - self._t0, 3)
        frame["ts"] = time.time()
        frame["boost_kpa"] = round(frame["map_kpa"] - BARO_KPA, 1) if frame.get("map_kpa") is not None else None
        frame["sim"] = {k[1:]: v for k, v in raw.items() if k.startswith("_")} or None
        frame["overheat"] = (frame.get("coolant_temp_c") or 0) > OVERHEAT_C
        self.latest = frame
        self.history.append(frame)

        if self.recording is not None:
            self.recording["samples"].append(frame)
            await self._check_auto_stop(frame)

        await self.broadcast({"type": "telemetry", "frame": frame, "recording": self._recording_public()})

        if now - self._last_snapshot >= SNAPSHOT_EVERY_S:
            self._last_snapshot = now
            await asyncio.to_thread(self._save_snapshot, frame)
        if now - self._last_dtc_poll >= DTC_POLL_S:
            self._last_dtc_poll = now
            await self.refresh_dtcs()

    def _save_snapshot(self, frame: dict) -> None:
        if not self.session_id:
            return
        with Session(engine) as s:
            s.add(TelemetrySnapshot(session_id=self.session_id, **{k: frame.get(k) for k in FRAME_FIELDS}))
            s.commit()

    # ================================================================ DTCs
    async def refresh_dtcs(self, force_broadcast: bool = False) -> List[dict]:
        src = self.source
        if not src or not src.is_connected():
            return []
        try:
            async with self._io_lock:
                dtcs = await asyncio.to_thread(src.get_dtcs)
                ff = await asyncio.to_thread(src.get_freeze_frame) if dtcs else {}
        except Exception as e:
            log.warning("No se pudieron leer DTCs: %s", e)
            return self.dtcs
        dtcs = [dict(d, freeze_frame=ff or None) for d in dtcs]
        changed = [(d["code"], d.get("status")) for d in dtcs] != [(d["code"], d.get("status")) for d in self.dtcs]
        self.dtcs, self.freeze_frame = dtcs, ff or {}
        await asyncio.to_thread(self._persist_dtcs, dtcs, ff)
        for d in self.dtcs:
            d["record_id"] = self._dtc_record_ids.get(d["code"])
        if changed or force_broadcast:
            await self.broadcast({"type": "dtcs", "dtcs": self.dtcs, "freeze_frame": self.freeze_frame})
        return self.dtcs

    def _persist_dtcs(self, dtcs: List[dict], ff: dict) -> None:
        if not self.session_id or not self.vehicle:
            return
        with Session(engine) as s:
            for d in dtcs:
                rid = self._dtc_record_ids.get(d["code"])
                if rid:
                    rec = s.get(DtcRecord, rid)
                    if rec and rec.status != d.get("status", "ACTIVE"):
                        rec.status = d.get("status", "ACTIVE")
                        rec.resolved_at = None
                        s.add(rec)
                    continue
                rec = DtcRecord(
                    session_id=self.session_id, vehicle_id=self.vehicle["id"], dtc_code=d["code"],
                    description=d.get("description"), status=d.get("status", "ACTIVE"),
                    freeze_frame_json=json.dumps(ff or {}),
                )
                s.add(rec)
                s.flush()
                self._dtc_record_ids[d["code"]] = rec.id
            s.commit()

    async def clear_dtcs(self) -> bool:
        src = self.source
        if not src or not src.is_connected():
            raise RuntimeError("Sin conexión OBD")
        async with self._io_lock:
            ok = await asyncio.to_thread(src.clear_dtcs)
        if ok and self._dtc_record_ids:
            with Session(engine) as s:
                for rid in self._dtc_record_ids.values():
                    rec = s.get(DtcRecord, rid)
                    if rec:
                        rec.status, rec.resolved_at = "CLEARED", datetime.now(timezone.utc)
                        s.add(rec)
                s.commit()
            self._dtc_record_ids = {}
        await self.refresh_dtcs(force_broadcast=True)
        return ok

    # ================================================================ simulador
    async def sim_control(self, action: str, **kwargs) -> dict:
        if not self.source:
            raise RuntimeError("Sin conexión")
        res = self.source.control(action, **kwargs)
        await self.broadcast({"type": "sim", "result": res})
        return res

    # ================================================================ corridas (Dyno / Pre-Stage)
    async def start_run(self, run_type: str, gear: Optional[int] = None) -> dict:
        if run_type not in ("dyno", "prestage"):
            raise ValueError("run_type debe ser 'dyno' o 'prestage'")
        if not self.source or not self.source.is_connected():
            raise RuntimeError("Conéctate al vehículo (o al simulador) primero")
        if self.recording:
            raise RuntimeError("Ya hay una corrida en curso")
        is_mock = self.source.kind == "mock"
        self.recording = {
            "run_type": run_type, "gear": gear, "samples": [], "started_at": datetime.now(timezone.utc),
            "started_mono": time.monotonic(), "auto": is_mock, "phase": "rec", "saw_pull": False,
        }
        if is_mock:
            if run_type == "dyno":
                self.source.control("pull", gear=gear)
                self.recording["phase"] = "pull"
            else:
                self.recording["task"] = asyncio.create_task(self._mock_prestage_script(gear))
        await self.broadcast({"type": "run_started", "recording": self._recording_public()})
        return self._recording_public()

    async def _mock_prestage_script(self, gear: Optional[int]) -> None:
        """Guion automático del simulador: mínimo caliente → crucero → pull a fondo."""
        try:
            rec = self.recording
            for phase, action, secs in (("idle", "idle", 10), ("cruise", "cruise", 8)):
                if self.recording is not rec:
                    return
                rec["phase"] = phase
                self.source.control(action)
                await asyncio.sleep(secs)
            if self.recording is rec:
                rec["phase"] = "pull"
                self.source.control("pull", gear=gear)
        except asyncio.CancelledError:
            pass

    async def _check_auto_stop(self, frame: dict) -> None:
        rec = self.recording
        if not rec:
            return
        if time.monotonic() - rec["started_mono"] > 120:
            await self.stop_run()
            return
        if not rec.get("auto") or rec.get("phase") != "pull":
            return
        mode = (frame.get("sim") or {}).get("mode")
        if mode == "pull":
            rec["saw_pull"] = True
        elif rec["saw_pull"]:
            await self.stop_run()

    async def stop_run(self) -> dict:
        rec, self.recording = self.recording, None
        if not rec:
            raise RuntimeError("No hay corrida en curso")
        if rec.get("task"):
            rec["task"].cancel()
        samples = rec["samples"]
        vehicle = self.vehicle or {}
        if rec["run_type"] == "dyno":
            result = await asyncio.to_thread(compute_dyno, samples, vehicle, rec.get("gear"))
            sens = self._sensor_summary(samples)
            result["sensor_summary"] = sens
        else:
            result = await asyncio.to_thread(run_prestage_check, samples, vehicle, self.dtcs)
            result.setdefault("ok", True)
        slim = [{k: s.get(k) for k in (*FRAME_FIELDS, "t", "boost_kpa")} for s in samples]
        with Session(engine) as s:
            run = PerformanceRun(
                session_id=self.session_id, vehicle_id=vehicle.get("id"), run_type=rec["run_type"],
                gear=result.get("gear") or rec.get("gear"), started_at=rec["started_at"],
                ended_at=datetime.now(timezone.utc), samples_json=json.dumps(slim), result_json=json.dumps(result, default=str),
            )
            s.add(run)
            s.commit()
            s.refresh(run)
            run_id = run.id
        self.last_run = {"id": run_id, "run_type": rec["run_type"], "result": result}
        await self.broadcast({"type": "run_result", **self.last_run})
        return self.last_run

    @staticmethod
    def _sensor_summary(samples: List[dict]) -> dict:
        wot = [s for s in samples if (s.get("throttle_pos") or 0) >= 75]
        if not wot:
            return {}

        def rng(key):
            vals = [s[key] for s in wot if s.get(key) is not None]
            return [min(vals), max(vals)] if vals else None

        return {"iat_c": rng("intake_temp_c"), "timing_deg": rng("timing_advance"), "map_kpa": rng("map_kpa"),
                "maf_gs": rng("maf_gs"), "coolant_c": rng("coolant_temp_c"), "voltage": rng("control_module_voltage")}

    # ================================================================ estado externo
    async def _status_loop(self) -> None:
        while True:
            await self.refresh_status()
            await asyncio.sleep(STATUS_POLL_S)

    async def refresh_status(self) -> dict:
        internet, ollama = await asyncio.gather(
            web_researcher.check_internet(), agent.status(), return_exceptions=True
        )
        self.status = {
            "internet": bool(internet) if not isinstance(internet, Exception) else False,
            "ollama": ollama if isinstance(ollama, dict) else {"online": False, "model": None},
        }
        await self.broadcast({"type": "status", "status": self.status, "connection": self.connection_state()})
        return self.status

    # ================================================================ contexto para Baymax
    def agent_context(self) -> dict:
        v = self.vehicle or {}
        conn = None
        if self.source and self.source.is_connected():
            i = self.conn_info or {}
            conn = f"{'SIMULADOR' if i.get('kind') == 'mock' else 'ELM327'} en {i.get('port')} | {i.get('protocol')}"
        return {
            "vehicle": v,
            "connection": conn,
            "live_frame": self.latest,
            "active_dtcs": self.dtcs,
            "history": self.vehicle_history_lines(v.get("id")) if v else [],
            "internet": bool(self.status.get("internet")),
        }

    @staticmethod
    def vehicle_history_lines(vehicle_id: Optional[int]) -> List[str]:
        if not vehicle_id:
            return []
        lines = []
        with Session(engine) as s:
            rows = s.exec(
                select(DiagnosticLog).where(DiagnosticLog.vehicle_id == vehicle_id)
                .order_by(DiagnosticLog.created_at.desc()).limit(8)
            ).all()
            for r in rows:
                code = None
                if r.dtc_record_id:
                    rec = s.get(DtcRecord, r.dtc_record_id)
                    code = rec.dtc_code if rec else None
                txt = f"{r.created_at:%Y-%m-%d} {code or 'síntoma'}"
                if r.symptoms_described:
                    txt += f" | síntomas: {r.symptoms_described}"
                txt += f" | hipótesis: {r.ai_hypothesis[:160].strip()}"
                if r.actual_fix_applied:
                    txt += f" | ARREGLO: {r.actual_fix_applied}" + (" (VERIFICADO)" if r.verified_working else " (sin verificar)")
                lines.append(txt)
        return lines


hub = TelemetryHub()
