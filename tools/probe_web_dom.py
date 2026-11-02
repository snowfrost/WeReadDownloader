"""Probe the running WebView shell over CDP (dev-only helper)."""
import json
import sys
import time
import urllib.request

import websocket

PORT = sys.argv[1] if len(sys.argv) > 1 else "9333"
EXPRS = sys.argv[2:] or [
    "Object.keys(window.pywebview.api||{}).length",
    "document.querySelector('#rail-version').textContent",
    "String(boot._done)+'/'+String(boot._tries)",
    "document.querySelector('#status-text').textContent",
]

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
targets = json.load(opener.open("http://127.0.0.1:%s/json" % PORT, timeout=8))
page = [t for t in targets if t.get("type") == "page"][0]
ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=15)


def evaluate(expr, await_promise=False):
    ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True,
                                   "awaitPromise": await_promise}}))
    payload = json.loads(ws.recv())["result"]
    error = payload.get("exceptionDetails")
    if error:
        return "ERR " + str(error.get("text")) + " " + str(
            (error.get("exception") or {}).get("description", ""))[:160]
    return payload.get("result", {}).get("value")


for expr in EXPRS:
    print(expr[:52], "=>", evaluate(expr))
ws.close()
