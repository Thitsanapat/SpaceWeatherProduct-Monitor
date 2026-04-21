#!/usr/bin/env python
# Direct WebSocket test with better error handling
import asyncio
import websockets
import json

async def test_websocket():
    print("[WebSocket Test]")
    uri = "ws://localhost:8000/ws/realtime?station=KMIT6"
    print(f"Connecting to: {uri}")
    
    try:
        async with websockets.connect(uri, ping_interval=None) as ws:
            print("✓ Connected!")
            
            # Wait for first message with timeout
            msg = await asyncio.wait_for(ws.recv(), timeout=3)
            print(f"✓ Received: {msg}")
            
            # Send ping
            await ws.send(json.dumps({"type": "ping"}))
            pong = await asyncio.wait_for(ws.recv(), timeout=3)
            print(f"✓ Pong: {pong}")
            
            await ws.close()
            return True
            
    except asyncio.TimeoutError:
        print("✗ Timeout (no response from server)")
        return False
    except websockets.exceptions.InvalidStatusException as e:
        print(f"✗ Invalid status: {e.status} {e.headers}")
        return False
    except Exception as e:
        print(f"✗ Error: {type(e).__name__}: {e}")
        return False

asyncio.run(test_websocket())
