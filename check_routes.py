#!/usr/bin/env python
# Test if ANY WebSocket endpoint appears
import urllib.request
import json

try:
    response = urllib.request.urlopen('http://localhost:8000/openapi.json', timeout=3)
    openapi = json.loads(response.read().decode('utf-8'))
    paths = openapi.get('paths', {})
    print("All registered routes:")
    for path in sorted(paths.keys()):
        methods = list(paths[path].keys())
        print(f"  {path}: {methods}")
    
    if '/ws/test' in paths:
        print("\n✓ /ws/test IS registered!")
    elif '/ws/realtime' in paths:
        print("\n✓ /ws/realtime IS registered!")
    else:
        print("\n✗ No WebSocket endpoints found")
        
except Exception as e:
    print(f"Error: {e}")
