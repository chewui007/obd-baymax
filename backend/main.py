"""Servidor principal FastAPI + WebSockets de BAYMAX OBD.

Une hardware, telemetría a 10 Hz, base de datos SQLite (garage.db),
ChromaDB RAG, DuckDuckGo search y el Agente de diagnóstico con Ollama.
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlmodel import Session, select

from backend.agent import rag_manuals
from backend.agent.mechanic_agent import agent
from backend.config import FRONTEND_DIR, HOST, PORT
from backend.database import engine, init_db, vehicle_to_dict
from backend.hardware.port_scanner import MOCK_PORT, list_serial_ports
from backend.hub import hub
from backend.models import (
    DiagnosticLog,
    DiagnosticSession,
    DtcRecord,
    PerformanceRun,
    Vehicle,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("baymax.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Inicializando BAYMAX OBD Engine & garage.db...")
    init_db()
    await hub.start()
    # Seleccionar por defecto el primer vehículo y conectar el simulador de arranque
    with Session(engine) as s:
        first = s.exec(select(Vehicle)).first()
        if first:
            await hub.select_vehicle(first.id)
            try:
                await hub.connect(MOCK_PORT, first.id)
            except Exception as e:
                log.warning("No se pudo autoconectar MOCK al inicio: %s", e)
    # Refrescar estado inicial de internet y ollama
    await hub.refresh_status()
    yield
    log.info("Cerrando BAYMAX OBD Engine...")
    await hub.stop()


app = FastAPI(title="BAYMAX OBD Engine", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==================================================================== Modelos Pydantic
class ConnectRequest(BaseModel):
    port: str
    vehicle_id: Optional[int] = None


class VehicleCreate(BaseModel):
    alias: str
    make: str
    model: str
    year: int
    engine_code: Optional[str] = None
    obd_protocol: Optional[str] = None
    aspiration: str = "NA"
    displacement_l: Optional[float] = None
    curb_weight_kg: Optional[float] = None
    drag_coef: Optional[float] = None
    frontal_area_m2: Optional[float] = None
    tire_diameter_m: Optional[float] = None
    final_drive: Optional[float] = None
    gear_ratios_json: Optional[str] = None
    redline_rpm: Optional[int] = None


class DiagnoseRequest(BaseModel):
    dtc_code: Optional[str] = None
    symptoms: Optional[str] = None


class ChatRequest(BaseModel):
    message: str
    history: List[Dict[str, str]] = []


class FixRequest(BaseModel):
    dtc_code: Optional[str] = None
    dtc_record_id: Optional[int] = None
    symptoms: Optional[str] = None
    ai_hypothesis: str
    actual_fix: str
    verified_working: bool = True


class RunStartRequest(BaseModel):
    run_type: str  # "dyno" | "prestage"
    gear: Optional[int] = None


class MockControlRequest(BaseModel):
    action: str  # "idle" | "cruise" | "pull" | "overheat"
    gear: Optional[int] = None
    enabled: Optional[bool] = None


# ==================================================================== Endpoints Flota
@app.get("/api/vehicles")
def get_vehicles():
    with Session(engine) as s:
        vehicles = s.exec(select(Vehicle)).all()
        return [vehicle_to_dict(v) for v in vehicles]


@app.post("/api/vehicles")
def create_vehicle(req: VehicleCreate):
    with Session(engine) as s:
        v = Vehicle(**req.model_dump())
        s.add(v)
        s.commit()
        s.refresh(v)
        return vehicle_to_dict(v)


@app.get("/api/vehicles/{vehicle_id}")
def get_vehicle(vehicle_id: int):
    with Session(engine) as s:
        v = s.get(Vehicle, vehicle_id)
        if not v:
            raise HTTPException(status_code=404, detail="Vehículo no encontrado")
        return vehicle_to_dict(v)


@app.post("/api/vehicles/{vehicle_id}/select")
async def select_vehicle(vehicle_id: int):
    try:
        v = await hub.select_vehicle(vehicle_id)
        return {"ok": True, "vehicle": v}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/vehicles/{vehicle_id}/history")
def get_vehicle_history(vehicle_id: int):
    with Session(engine) as s:
        v = s.get(Vehicle, vehicle_id)
        if not v:
            raise HTTPException(status_code=404, detail="Vehículo no encontrado")
        logs = s.exec(
            select(DiagnosticLog)
            .where(DiagnosticLog.vehicle_id == vehicle_id)
            .order_by(DiagnosticLog.created_at.desc())
            .limit(25)
        ).all()
        dtcs = s.exec(
            select(DtcRecord)
            .where(DtcRecord.vehicle_id == vehicle_id)
            .order_by(DtcRecord.detected_at.desc())
            .limit(25)
        ).all()
        runs = s.exec(
            select(PerformanceRun)
            .where(PerformanceRun.vehicle_id == vehicle_id)
            .order_by(PerformanceRun.started_at.desc())
            .limit(15)
        ).all()

        return {
            "vehicle": vehicle_to_dict(v),
            "repairs": [
                {
                    "id": lg.id,
                    "symptoms": lg.symptoms_described,
                    "hypothesis": lg.ai_hypothesis,
                    "fix": lg.actual_fix_applied,
                    "verified": lg.verified_working,
                    "created_at": lg.created_at.strftime("%Y-%m-%d %H:%M") if lg.created_at else "",
                }
                for lg in logs
            ],
            "dtc_history": [
                {
                    "id": d.id,
                    "code": d.dtc_code,
                    "description": d.description,
                    "status": d.status,
                    "freeze_frame": json.loads(d.freeze_frame_json) if d.freeze_frame_json else {},
                    "detected_at": d.detected_at.strftime("%Y-%m-%d %H:%M") if d.detected_at else "",
                }
                for d in dtcs
            ],
            "runs": [
                {
                    "id": r.id,
                    "run_type": r.run_type,
                    "gear": r.gear,
                    "started_at": r.started_at.strftime("%Y-%m-%d %H:%M") if r.started_at else "",
                    "result": json.loads(r.result_json) if r.result_json else {},
                }
                for r in runs
            ],
        }


# ==================================================================== Endpoints Hardware / Conexión
@app.get("/api/ports")
def get_ports():
    ports = list_serial_ports()
    # Agregar la opción fija de Simulador al inicio
    options = [
        {
            "device": MOCK_PORT,
            "description": "SIMULADOR / MOCK (Física real sin cable)",
            "manufacturer": "Baymax Virtual Engine",
            "likely_obd": True,
        }
    ]
    options.extend(ports)
    return options


@app.post("/api/connect")
async def connect_hardware(req: ConnectRequest):
    try:
        res = await hub.connect(port=req.port, vehicle_id=req.vehicle_id)
        return {"ok": True, **res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/disconnect")
async def disconnect_hardware():
    await hub.disconnect()
    return {"ok": True}


@app.get("/api/status")
async def get_status():
    return hub.snapshot_state()


@app.post("/api/status/refresh")
async def refresh_status():
    st = await hub.refresh_status()
    return {"ok": True, "status": st}


# ==================================================================== Endpoints DTCs
@app.get("/api/dtcs")
async def get_dtcs():
    return {
        "dtcs": hub.dtcs,
        "freeze_frame": hub.freeze_frame,
    }


@app.post("/api/dtcs/refresh")
async def refresh_dtcs():
    dtcs = await hub.refresh_dtcs(force_broadcast=True)
    return {"ok": True, "dtcs": dtcs, "freeze_frame": hub.freeze_frame}


@app.post("/api/dtcs/clear")
async def clear_dtcs():
    ok = await hub.clear_dtcs()
    return {"ok": ok}


# ==================================================================== Agente IA (SSE Streams)
async def _sse_generator(async_iter):
    try:
        async for item in async_iter:
            payload = json.dumps(item, default=str)
            yield f"data: {payload}\n\n"
    except Exception as e:
        log.exception("Error en streaming del agente")
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"


@app.post("/api/agent/diagnose")
async def diagnose(req: DiagnoseRequest):
    ctx = hub.agent_context()
    dtc_obj = None
    if req.dtc_code:
        # Buscar en los DTCs detectados o armar uno
        match = next((d for d in hub.dtcs if d["code"].upper() == req.dtc_code.upper()), None)
        if match:
            dtc_obj = dict(match)
            dtc_obj["freeze_frame"] = hub.freeze_frame
        else:
            from backend.hardware.dtc_codes import describe
            dtc_obj = {
                "code": req.dtc_code.upper(),
                "description": describe(req.dtc_code),
                "status": "ACTIVE",
                "freeze_frame": hub.freeze_frame,
            }

    stream = agent.diagnose(ctx=ctx, dtc=dtc_obj, symptoms=req.symptoms)
    return StreamingResponse(
        _sse_generator(stream),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/agent/chat")
async def chat(req: ChatRequest):
    ctx = hub.agent_context()
    stream = agent.chat(ctx=ctx, history=req.history, message=req.message)
    return StreamingResponse(
        _sse_generator(stream),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/agent/fix")
async def save_fix(req: FixRequest):
    vehicle = hub.vehicle
    if not vehicle:
        raise HTTPException(status_code=400, detail="No hay vehículo activo seleccionado")

    with Session(engine) as s:
        # Buscar el registro dtc si se proporcionó código
        dtc_rec_id = req.dtc_record_id
        if not dtc_rec_id and req.dtc_code:
            rec = s.exec(
                select(DtcRecord)
                .where(DtcRecord.vehicle_id == vehicle["id"])
                .where(DtcRecord.dtc_code == req.dtc_code.upper())
                .order_by(DtcRecord.detected_at.desc())
            ).first()
            if rec:
                dtc_rec_id = rec.id

        diag = DiagnosticLog(
            dtc_record_id=dtc_rec_id,
            vehicle_id=vehicle["id"],
            symptoms_described=req.symptoms,
            ai_hypothesis=req.ai_hypothesis,
            actual_fix_applied=req.actual_fix,
            verified_working=req.verified_working,
        )
        s.add(diag)
        s.commit()
        s.refresh(diag)

        # Cierra el ciclo de aprendizaje en ChromaDB
        rag_manuals.store_verified_fix(
            vehicle=vehicle,
            dtc_code=req.dtc_code,
            fix=req.actual_fix,
            hypothesis=req.ai_hypothesis,
            log_id=diag.id,
        )

        return {"ok": True, "log_id": diag.id}


# ==================================================================== Virtual Dyno & Pre-Stage Runs
@app.post("/api/runs/start")
async def start_run(req: RunStartRequest):
    try:
        res = await hub.start_run(run_type=req.run_type, gear=req.gear)
        return {"ok": True, "recording": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/runs/stop")
async def stop_run():
    try:
        res = await hub.stop_run()
        return {"ok": True, "run": res}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/runs/history")
def get_runs_history():
    with Session(engine) as s:
        runs = s.exec(select(PerformanceRun).order_by(PerformanceRun.started_at.desc()).limit(20)).all()
        out = []
        for r in runs:
            out.append(
                {
                    "id": r.id,
                    "vehicle_id": r.vehicle_id,
                    "run_type": r.run_type,
                    "gear": r.gear,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                    "result": json.loads(r.result_json) if r.result_json else {},
                }
            )
        return out


@app.post("/api/runs/analyze")
async def analyze_run(run_id: int):
    with Session(engine) as s:
        r = s.get(PerformanceRun, run_id)
        if not r:
            raise HTTPException(status_code=404, detail="Corrida no encontrada")
        ctx = hub.agent_context()
        result = json.loads(r.result_json) if r.result_json else {}
        stream = agent.analyze_run(ctx=ctx, run_type=r.run_type, result=result)
        return StreamingResponse(
            _sse_generator(stream),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


# ==================================================================== Simulador Control & Ollama
@app.post("/api/mock/control")
async def control_mock(req: MockControlRequest):
    kwargs = {}
    if req.gear is not None:
        kwargs["gear"] = req.gear
    if req.enabled is not None:
        kwargs["enabled"] = req.enabled
    res = await hub.sim_control(req.action, **kwargs)
    return res


@app.get("/api/agent/context")
def get_agent_context():
    from backend.agent.mechanic_agent import build_system_prompt

    ctx = hub.agent_context()
    return {"context": ctx, "system_prompt": build_system_prompt(ctx)}


@app.post("/api/ollama/pull")
async def pull_ollama_model(model: Optional[str] = None):
    async def _pull_and_refresh():
        async for ev in agent.pull_model(model):
            yield ev
        await hub.refresh_status()

    return StreamingResponse(
        _sse_generator(_pull_and_refresh()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ==================================================================== RAG & Manuales
@app.get("/api/manuals/stats")
def get_manuals_stats():
    return rag_manuals.stats()


@app.post("/api/manuals/ingest")
async def ingest_manuals():
    report = rag_manuals.ingest_manuals()
    return {"ok": True, "report": report}


# ==================================================================== WebSockets
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await hub.register(ws)
    try:
        while True:
            # Mantener conexión viva y procesar comandos opcionales desde el cliente
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                cmd = msg.get("command")
                if cmd == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
            except Exception:
                pass
    except WebSocketDisconnect:
        hub.unregister(ws)
    except Exception:
        hub.unregister(ws)


# ==================================================================== Frontend Estático
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    async def serve_index():
        idx = FRONTEND_DIR / "index.html"
        if idx.exists():
            return FileResponse(str(idx))
        return {"message": "BAYMAX OBD Engine API activa. Frontend no encontrado en /frontend."}
else:
    @app.get("/")
    def serve_fallback():
        return {"message": "BAYMAX OBD Engine API activa. Directorio frontend/ no encontrado."}

