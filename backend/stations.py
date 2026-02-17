# backend/stations.py
# -*- coding: utf-8 -*-
from typing import Dict, List, Optional, TypedDict


class Station(TypedDict):
    id: str
    name: str
    lat: float
    lon: float
    alt: float
    mount: str


DEFAULT_STATION_ID = "KMIT6"

# Add new stations here. Keep IDs uppercase.
STATIONS: Dict[str, Station] = {
    "KMIT6": {
        "id": "KMIT6",
        "name": "KMITL Station (Urban)",
        "lat": 13.72782749,
        "lon": 100.77243502,
        "alt": 29.385,
        "mount": "KMIT6",
    },
    "KMIG": {
        "id": "KMIG",
        "name": "KMIG Station",
        "lat": 13.72782749,
        "lon": 100.77243502,
        "alt": 29.385,
        "mount": "KMIG",
    },
    "CHMA": {
        "id": "CHMA",
        "name": "CHMA Station",
        "lat": 18.83529555,
        "lon": 98.9699524,
        "alt": 303,
        "mount": "CHMA",
    },
    "CPN1": {
        "id": "CPN1",
        "name": "CPN1 Station",
        "lat": 10.72465820,
        "lon": 99.37435425,
        "alt": 9.805,
        "mount": "CPN1",
    },
}


def normalize_station_id(station_id: Optional[str]) -> Optional[str]:
    if station_id is None:
        return None
    sid = str(station_id).strip()
    return sid.upper() if sid else None


def list_stations() -> List[Station]:
    return list(STATIONS.values())


def get_station(station_id: Optional[str]) -> Optional[Station]:
    sid = normalize_station_id(station_id)
    if not sid:
        return None
    return STATIONS.get(sid)


def list_station_ids() -> List[str]:
    return list(STATIONS.keys())
