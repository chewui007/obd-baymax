"""Modelos SQLModel de garage.db.

Implementa las 5 tablas del esquema base (vehicles, diagnostic_sessions, dtc_records,
diagnostics_log, telemetry_snapshots) + extensiones marcadas con [EXT] necesarias para
Virtual Dyno y Pre-Stage Check (parámetros físicos del carro, IAT, avance, velocidad) y
la tabla performance_runs para guardar los pulls.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Vehicle(SQLModel, table=True):
    __tablename__ = "vehicles"

    id: Optional[int] = Field(default=None, primary_key=True)
    vin: Optional[str] = Field(default=None, max_length=17, unique=True)
    alias: str = Field(max_length=50)
    make: str = Field(max_length=50)
    model: str = Field(max_length=50)
    year: int
    engine_code: Optional[str] = Field(default=None, max_length=50)
    obd_protocol: Optional[str] = Field(default=None, max_length=50)
    # [EXT] Parámetros físicos para Virtual Dyno / Pre-Stage
    aspiration: str = Field(default="NA", max_length=10)  # "NA" | "TURBO"
    displacement_l: Optional[float] = None
    curb_weight_kg: Optional[float] = None
    drag_coef: Optional[float] = None
    frontal_area_m2: Optional[float] = None
    tire_diameter_m: Optional[float] = None
    final_drive: Optional[float] = None
    gear_ratios_json: Optional[str] = None  # ej. "[4.38, 2.86, 1.92, 1.37, 1.0]"
    redline_rpm: Optional[int] = None
    created_at: datetime = Field(default_factory=utc_now)


class DiagnosticSession(SQLModel, table=True):
    __tablename__ = "diagnostic_sessions"

    id: Optional[int] = Field(default=None, primary_key=True)
    vehicle_id: int = Field(foreign_key="vehicles.id", index=True)
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: Optional[datetime] = None
    connection_port: Optional[str] = Field(default=None, max_length=50)
    notes: Optional[str] = None


class DtcRecord(SQLModel, table=True):
    __tablename__ = "dtc_records"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="diagnostic_sessions.id", index=True)
    vehicle_id: int = Field(foreign_key="vehicles.id", index=True)
    dtc_code: str = Field(max_length=10, index=True)
    description: Optional[str] = None  # [EXT]
    status: str = Field(default="ACTIVE", max_length=20)  # ACTIVE | PENDING | CLEARED
    freeze_frame_json: str
    detected_at: datetime = Field(default_factory=utc_now)
    resolved_at: Optional[datetime] = None


class DiagnosticLog(SQLModel, table=True):
    __tablename__ = "diagnostics_log"

    id: Optional[int] = Field(default=None, primary_key=True)
    dtc_record_id: Optional[int] = Field(default=None, foreign_key="dtc_records.id")
    vehicle_id: int = Field(foreign_key="vehicles.id", index=True)
    symptoms_described: Optional[str] = None
    ai_hypothesis: str
    web_sources_json: Optional[str] = None
    actual_fix_applied: Optional[str] = None
    verified_working: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class TelemetrySnapshot(SQLModel, table=True):
    __tablename__ = "telemetry_snapshots"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="diagnostic_sessions.id", index=True)
    timestamp: datetime = Field(default_factory=utc_now)
    rpm: Optional[float] = None
    coolant_temp_c: Optional[float] = None
    stft_b1: Optional[float] = None
    ltft_b1: Optional[float] = None
    map_kpa: Optional[float] = None
    maf_gs: Optional[float] = None
    throttle_pos: Optional[float] = None
    control_module_voltage: Optional[float] = None
    # [EXT]
    speed_kph: Optional[float] = None
    intake_temp_c: Optional[float] = None
    engine_load: Optional[float] = None
    timing_advance: Optional[float] = None


class PerformanceRun(SQLModel, table=True):
    """[EXT] Pulls de Virtual Dyno y Pruebas de Estrés Pre-Stage."""

    __tablename__ = "performance_runs"

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: Optional[int] = Field(default=None, foreign_key="diagnostic_sessions.id")
    vehicle_id: int = Field(foreign_key="vehicles.id", index=True)
    run_type: str = Field(max_length=20)  # "dyno" | "prestage"
    gear: Optional[int] = None
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: Optional[datetime] = None
    samples_json: str = "[]"
    result_json: str = "{}"
    notes: Optional[str] = None
