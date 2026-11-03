"""Tiny CDP driver for the WebView shell (dev helper)."""
import json
import sys
import time
import urllib.request

import websocket

PORT = sys.argv[1] if len(sys.argv) > 1 else "9333"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
targets = json.load(opener.open("http://127.0.0.1:%s/json" % PORT, timeout=8))
pages = [t for t in targets if isinstance(t, dict) and t.get("type") == "page"]
ws = websocket.create_connection(pages[0]["webSocketDebuggerUrl"], timeout=30)


def evaluate(expr, await_promise=False, timeout=20):
    ws.send(json.dumps({"id": 9, "method": "Runtime.evaluate", "params": {
        "expression": expr, "returnByValue": True,
        "awaitPromise": await_promise}}))
    end = time.time() + timeout
    while time.time() < end:
        try:
            message = json.loads(ws.recv())
        except Exception:
            continue
        if message.get("id") == 9:
            result = message["result"]
            if result.get("exceptionDetails"):
                detail = result["exceptionDetails"]
                return "ERR " + str((detail.get("exception") or {}).get("description", ""))[:200]
            return result.get("result", {}).get("value")
    return "TIMEOUT"


for expr in sys.argv[2:]:
    # A leading "!" means "this expression is a promise, await it".
    if expr.startswith("!"):
        print(evaluate(expr[1:], await_promise=True))
    else:
        print(evaluate(expr))
ws.close()
