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
        "lat": 10.724658199966608,
        "lon": 99.37435425038714,
        "alt": 9.804921923205256,
        "mount": "CPN1",
    },
    "STFD": {
        "id": "STFD",
        "name": "STFD Station",
        "lat": 13.735599539842081,
        "lon": 100.66111074062903,
        "alt": -3.7220604130998254,
        "mount": "STFD",
    },
    "CADT": {
        "id": "CADT",
        "name": "CADT Station",
        "lat": 11.65451041634143,
        "lon": 104.91158948032916,
        "alt": 22.47479969356209,
        "mount": "CADT",
    },
    "ITC0": {
        "id": "ITC0",
        "name": "ITC0 Station",
        "lat": 11.570515520009781,
        "lon": 104.89941276085693,
        "alt": 16.41299171280116,
        "mount": "ITC0",
    },
    "CM01": {
        "id": "CM01",
        "name": "CM01 Station",
        "lat": 18.80220443807562,
        "lon": 98.95525073367308,
        "alt": 305.79436383768916,
        "mount": "CM01",
    },
    "KKU0": {
        "id": "KKU0",
        "name": "KKU0 Station",
        "lat": 16.47207098592002,
        "lon": 102.82600956017403,
        "alt": 185.98654387611896,
        "mount": "KKU0",
    },
    "CHAN": {
        "id": "CHAN",
        "name": "CHAN Station",
        "lat": 12.610310300006226,
        "lon": 102.10241052237672,
        "alt": 7.146916316822171,
        "mount": "CHAN",
    },
    "CNBR": {
        "id": "CNBR",
        "name": "CNBR Station",
        "lat": 13.406018974730594,
        "lon": 100.9976519777507,
        "alt": -7.552253680303693,
        "mount": "CNBR",
    },
    "DPT9": {
        "id": "DPT9",
        "name": "DPT9 Station",
        "lat": 13.756781563350186,
        "lon": 100.57319989695353,
        "alt": 37.245827386155725,
        "mount": "DPT9",
    },
    "LPBR": {
        "id": "LPBR",
        "name": "LPBR Station",
        "lat": 14.800907452004195,
        "lon": 100.65124630750549,
        "alt": 8.837934108451009,
        "mount": "LPBR",
    },
    "NKNY": {
        "id": "NKNY",
        "name": "NKNY Station",
        "lat": 14.212002885890096,
        "lon": 101.20221131383627,
        "alt": -16.849362236447632,
        "mount": "NKNY",
    },
    "NKRM": {
        "id": "NKRM",
        "name": "NKRM Station",
        "lat": 14.992118855436544,
        "lon": 102.12946955615338,
        "alt": 151.7124607777223,
        "mount": "NKRM",
    },
    "NKSW": {
        "id": "NKSW",
        "name": "NKSW Station",
        "lat": 15.69063705552156,
        "lon": 100.11411206375129,
        "alt": 20.321971726603806,
        "mount": "NKSW",
    },
    "PJRK": {
        "id": "PJRK",
        "name": "PJRK Station",
        "lat": 11.811620838356756,
        "lon": 99.79634835004458,
        "alt": -12.50912114419043,
        "mount": "PJRK",
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
