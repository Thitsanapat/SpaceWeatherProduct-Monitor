#!/usr/bin/env python
# Test /ws/test endpoint directly
import asyncio
import websockets

async def test():
    for path in ["/ws/test", "/ws/realtime"]:
        print(f"\nTesting {path}...")
        try:
            uri = f"ws://localhost:8000{path}?station=KMIT6"
            async with websockets.connect(uri, ping_interval=None) as ws:
                msg = await asyncio.wait_for(ws.recv(), timeout=2)
                print(f"  ✓ Connected! Received: {msg}")
                return
        except Exception as e:
            print(f"  ✗ {type(e).__name__}: {e}")

asyncio.run(test())
