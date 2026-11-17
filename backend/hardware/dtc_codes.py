"""Tabla mínima de descripciones DTC (fallback cuando python-OBD no trae la descripción)."""

DTC_DESCRIPTIONS = {
    "P0010": "Camshaft Position Actuator Circuit (Bank 1)",
    "P0016": "Crankshaft/Camshaft Position Correlation (Bank 1 Sensor A)",
    "P0101": "Mass Air Flow Circuit Range/Performance",
    "P0113": "Intake Air Temperature Sensor Circuit High",
    "P0128": "Coolant Thermostat (Below Regulating Temperature)",
    "P0171": "System Too Lean (Bank 1)",
    "P0172": "System Too Rich (Bank 1)",
    "P0174": "System Too Lean (Bank 2)",
    "P0217": "Engine Overtemperature Condition",
    "P0234": "Turbocharger Overboost Condition",
    "P0299": "Turbocharger/Supercharger Underboost",
    "P0300": "Random/Multiple Cylinder Misfire Detected",
    "P0301": "Cylinder 1 Misfire Detected",
    "P0302": "Cylinder 2 Misfire Detected",
    "P0303": "Cylinder 3 Misfire Detected",
    "P0304": "Cylinder 4 Misfire Detected",
    "P0305": "Cylinder 5 Misfire Detected",
    "P0306": "Cylinder 6 Misfire Detected",
    "P0325": "Knock Sensor 1 Circuit (Bank 1)",
    "P0335": "Crankshaft Position Sensor A Circuit",
    "P0401": "Exhaust Gas Recirculation Flow Insufficient",
    "P0420": "Catalyst System Efficiency Below Threshold (Bank 1)",
    "P0440": "Evaporative Emission Control System Malfunction",
    "P0455": "EVAP System Leak Detected (Large Leak)",
    "P0505": "Idle Air Control System Malfunction",
    "P0507": "Idle Control System RPM Higher Than Expected",
    "P0562": "System Voltage Low",
    "P2004": "Intake Manifold Runner Control Stuck Open (Bank 1)",
    "P2187": "System Too Lean at Idle (Bank 1)",
}


def describe(code: str) -> str:
    return DTC_DESCRIPTIONS.get(code.upper(), "Descripción no disponible (código específico del fabricante)")
