#!/usr/bin/env python
# Quick WebSocket connection test
import sys
import time
import urllib.request
import json

try:
    import websockets
    print("[TEST] Testing backend connectivity...")
    
    # Test HTTP first
    print("[1] Testing HTTP GET /api/runtime_metrics...")
    try:
        response = urllib.request.urlopen('http://localhost:8000/api/runtime_metrics', timeout=3)
        body = response.read().decode('utf-8')
        print(f"    HTTP Status: {response.status}")
        print(f"    Response: {body[:100]}")
    except Exception as e:
        print(f"    HTTP Error: {e}")
        
    # Test WebSocket
    print("[2] Testing WebSocket /ws/realtime...")
    import asyncio
    async def test_ws():
        try:
            async with websockets.connect('ws://localhost:8000/ws/realtime?station=KMIT6', ping_interval=None) as ws:
                print("    WebSocket connected!")
                msg = await asyncio.wait_for(ws.recv(), timeout=2)
                print(f"    First message: {msg}")
                await ws.close()
                return True
        except asyncio.TimeoutError:
            print("    WebSocket timeout (no message received)")
            return False
        except Exception as e:
            print(f"    WebSocket Error: {e}")
            return False
    
    result = asyncio.run(test_ws())
    
except ImportError as e:
    print(f"Missing module: {e}")
    print("Installing websockets...")
    import subprocess
    subprocess.run([sys.executable, '-m', 'pip', 'install', 'websockets'], check=False)
