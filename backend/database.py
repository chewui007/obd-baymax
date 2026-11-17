"""SQLite (garage.db): engine, creación de tablas y datos semilla de la flota."""
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from backend.config import DB_PATH

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA journal_mode=WAL")  # lecturas concurrentes mientras se escribe telemetría
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


SEED_VEHICLES = [
    {
        "alias": "Meches C250",
        "make": "Mercedes-Benz",
        "model": "C250 W204",
        "year": 2013,
        "engine_code": "M271 EVO 1.8T",
        "obd_protocol": "ISO 15765-4 (CAN 11/500)",
        "aspiration": "TURBO",
        "displacement_l": 1.8,
        "curb_weight_kg": 1545,  # ~3,400 lb
        "drag_coef": 0.27,
        "frontal_area_m2": 2.2,
        "tire_diameter_m": 0.634,  # 225/45R17
        "final_drive": 2.65,
        "gear_ratios_json": json.dumps([4.38, 2.86, 1.92, 1.37, 1.00, 0.82, 0.73]),  # 7G-Tronic
        "redline_rpm": 6200,
    },
    {
        "alias": "La Runner",
        "make": "Toyota",
        "model": "4Runner 3rd Gen",
        "year": 2000,
        "engine_code": "3RZ-FE 2.7L",
        "obd_protocol": "ISO 9141-2",
        "aspiration": "NA",
        "displacement_l": 2.7,
        "curb_weight_kg": 1770,
        "drag_coef": 0.42,
        "frontal_area_m2": 2.9,
        "tire_diameter_m": 0.777,  # 265/70R16
        "final_drive": 4.10,
        "gear_ratios_json": json.dumps([2.804, 1.531, 1.000, 0.705]),  # A340
        "redline_rpm": 5200,
    },
    {
        "alias": "Pilot",
        "make": "Honda",
        "model": "Pilot",
        "year": 2010,
        "engine_code": "J35 3.5L V6",
        "obd_protocol": "ISO 15765-4 (CAN 11/500)",
        "aspiration": "NA",
        "displacement_l": 3.5,
        "curb_weight_kg": 2000,
        "drag_coef": 0.38,
        "frontal_area_m2": 3.0,
        "tire_diameter_m": 0.737,  # 235/65R17
        "final_drive": 4.31,
        "gear_ratios_json": json.dumps([2.697, 1.606, 1.071, 0.766, 0.612]),
        "redline_rpm": 6300,
    },
]

MAKE_SHORT = {"Mercedes-Benz": "MB", "Volkswagen": "VW", "Chevrolet": "Chevy"}


def init_db() -> None:
    from backend import models  # noqa: F401  (registra las tablas en el metadata)

    SQLModel.metadata.create_all(engine)
    _seed()


def _seed() -> None:
    from backend.models import DiagnosticLog, DiagnosticSession, DtcRecord, Vehicle

    with Session(engine) as s:
        if s.exec(select(Vehicle)).first():
            return
        created = {}
        for data in SEED_VEHICLES:
            v = Vehicle(**data)
            s.add(v)
            s.flush()
            created[v.alias] = v

        # Historial de ejemplo: la 4Runner tuvo un P0300 hace 2 meses (resuelto con bujías)
        runner = created["La Runner"]
        past = datetime.now(timezone.utc) - timedelta(days=60)
        sess = DiagnosticSession(
            vehicle_id=runner.id,
            started_at=past,
            ended_at=past + timedelta(minutes=25),
            connection_port="MOCK",
            notes="Historial de ejemplo (seed)",
        )
        s.add(sess)
        s.flush()
        dtc = DtcRecord(
            session_id=sess.id,
            vehicle_id=runner.id,
            dtc_code="P0300",
            description="Random/Multiple Cylinder Misfire Detected",
            status="CLEARED",
            freeze_frame_json=json.dumps(
                {"rpm": 2400, "speed_kph": 54, "coolant_temp_c": 90, "stft_b1": 3.1,
                 "ltft_b1": 5.4, "maf_gs": 14.8, "engine_load": 71, "throttle_pos": 42}
            ),
            detected_at=past,
            resolved_at=past + timedelta(days=1),
        )
        s.add(dtc)
        s.flush()
        s.add(
            DiagnosticLog(
                dtc_record_id=dtc.id,
                vehicle_id=runner.id,
                symptoms_described="Tironea al acelerar en subida, check engine parpadeando",
                ai_hypothesis="Fallo de encendido múltiple bajo carga: bujías desgastadas o cables de bujía con fuga.",
                actual_fix_applied="Cambio de bujías (Denso K16TR) + revisión de cables de bujía",
                verified_working=True,
                created_at=past + timedelta(days=1),
            )
        )
        s.commit()


def vehicle_to_dict(v) -> dict:
    d = v.model_dump() if hasattr(v, "model_dump") else v.dict()
    d["gear_ratios"] = json.loads(v.gear_ratios_json) if v.gear_ratios_json else []
    d["created_at"] = v.created_at.isoformat() if v.created_at else None
    engine_short = (v.engine_code or "").split(" ")[0]
    make = MAKE_SHORT.get(v.make, v.make)
    d["label"] = f"{v.year} {make} {v.model}" + (f" [{engine_short}]" if engine_short else "")
    return d


def load_vehicle_dict(vehicle_id: int):
    from backend.models import Vehicle

    with Session(engine) as s:
        v = s.get(Vehicle, vehicle_id)
        return vehicle_to_dict(v) if v else None
