#!/usr/bin/env python
# Test backend endpoints
import urllib.request
import json

try:
    # Test 1: HTTP GET
    print("[1] GET /api/runtime_metrics...")
    response = urllib.request.urlopen('http://localhost:8000/api/runtime_metrics', timeout=3)
    data = json.loads(response.read().decode('utf-8'))
    print(f"    OK: {data.get('ok')}")
    print(f"    WS connections: {data.get('ws', {}).get('total_connections')}")
    
    # Test 2: HTTP GET stations
    print("\n[2] GET /api/stations...")
    response = urllib.request.urlopen('http://localhost:8000/api/stations', timeout=3)
    data = json.loads(response.read().decode('utf-8'))
    print(f"    OK: {data.get('ok')}")
    print(f"    Stations: {len(data.get('stations', []))} found")
    if data.get('stations'):
        print(f"    First station: {data['stations'][0]}")
    
    # Test 3: Telnet-like WebSocket check
    print("\n[3] List all FastAPI routes...")
    response = urllib.request.urlopen('http://localhost:8000/openapi.json', timeout=3)
    openapi = json.loads(response.read().decode('utf-8'))
    paths = openapi.get('paths', {})
    print(f"    Found {len(paths)} routes:")
    for path in sorted(paths.keys()):
        methods = list(paths[path].keys())
        print(f"      {path}: {methods}")
        
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
