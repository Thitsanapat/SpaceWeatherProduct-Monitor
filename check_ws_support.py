#!/usr/bin/env python
# Check if WebSocket support is available in backend environment
import sys

try:
    print("Python version:", sys.version)
    print()
    
    print("[1] Check FastAPI...")
    import fastapi
    print(f"    ✓ FastAPI {fastapi.__version__}")
    
    print("[2] Check WebSocket support...")
    from fastapi import WebSocket
    print("    ✓ WebSocket import OK")
    
    print("[3] Check websockets library...")
    import websockets
    print(f"    ✓ websockets {websockets.__version__}")
    
    print("[4] Check if FastAPI app has WebSocket support...")
    from fastapi import FastAPI
    app = FastAPI()
    print(f"    ✓ FastAPI app created")
    print(f"    ✓ app.websocket method exists: {hasattr(app, 'websocket')}")
    
    # Try to register a test route
    @app.websocket("/test/ws")
    async def test_ws(ws: WebSocket):
        await ws.accept()
        await ws.send_text("hello")
    
    print("    ✓ WebSocket route decorated successfully")
    
    # Check routes
    print("[5] Check registered routes...")
    for route in app.routes:
        print(f"    {route.path}: {route.methods if hasattr(route, 'methods') else 'websocket'}")
        
except Exception as e:
    print(f"ERROR: {e}")
    import traceback
    traceback.print_exc()
