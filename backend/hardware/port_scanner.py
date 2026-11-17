"""Detección de puertos seriales multiplataforma.

- Windows: COM1..COMx
- macOS:   /dev/cu.*  (se prefieren los 'cu' sobre los 'tty' porque no esperan DCD)
- Linux:   /dev/ttyUSB*, /dev/ttyACM*, /dev/rfcomm*
"""
from __future__ import annotations

import sys
from typing import List

from serial.tools import list_ports

MOCK_PORT = "MOCK"

# Chips típicos de cables ELM327 / OBDLink / clones
_OBD_HINTS = (
    "ch340", "ch341", "wch", "ftdi", "ft232", "pl2303", "prolific", "cp210",
    "silicon labs", "elm", "obd", "usbserial", "vlinker", "obdlink", "stn",
)
_IGNORE = ("bluetooth-incoming-port", "debug-console", "wlan-debug")


def list_serial_ports() -> List[dict]:
    ports = []
    for p in list_ports.comports():
        device = p.device
        low = f"{device} {p.description or ''} {p.manufacturer or ''} {p.hwid or ''}".lower()
        if any(x in low for x in _IGNORE):
            continue
        if sys.platform == "darwin" and device.startswith("/dev/tty."):
            # En Mac el mismo adaptador aparece como /dev/tty.X y /dev/cu.X -> usamos cu
            continue
        ports.append(
            {
                "device": device,
                "description": p.description or "",
                "manufacturer": p.manufacturer or "",
                "hwid": p.hwid or "",
                "likely_obd": any(h in low for h in _OBD_HINTS),
            }
        )
    ports.sort(key=lambda x: (not x["likely_obd"], x["device"]))
    return ports


if __name__ == "__main__":
    for port in list_serial_ports():
        flag = "★" if port["likely_obd"] else " "
        print(f"{flag} {port['device']:<35} {port['description']}")
