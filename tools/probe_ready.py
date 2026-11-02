"""Wait for the page to finish booting; dump state when it does not."""
import json
import sys
import time
import urllib.request

import websocket

PORT = sys.argv[1] if len(sys.argv) > 1 else "9333"
DEADLINE = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
start = time.time()
page = None
while time.time() - start < DEADLINE:
    try:
        targets = json.load(opener.open("http://127.0.0.1:%s/json" % PORT, timeout=2))
        pages = [t for t in targets if t.get("type") == "page"]
        if pages:
            page = pages[0]
            break
    except Exception:
        pass
    time.sleep(0.5)

if page is None:
    print("RESULT no-cdp-target")
    raise SystemExit(1)

ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=10)
ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
errors = []


def evaluate(expr):
    ws.send(json.dumps({"id": 9, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True}}))
    end = time.time() + 6
    while time.time() < end:
        try:
            message = json.loads(ws.recv())
        except Exception:
            continue
        if message.get("method") == "Runtime.exceptionThrown":
            detail = message["params"]["exceptionDetails"]
            errors.append(detail.get("text", ""))
        if message.get("id") == 9:
            result = message["result"]
            if result.get("exceptionDetails"):
                return "ERR " + str((result["exceptionDetails"].get("exception") or {}).get("description", ""))[:120]
            return result.get("result", {}).get("value")
    return None


ready = None
while time.time() - start < DEADLINE:
    rail = evaluate("(document.querySelector('#rail-version')||{}).textContent")
    done = evaluate("typeof boot !== 'undefined' ? boot._done : null")
    tries = evaluate("typeof boot !== 'undefined' ? boot._tries : null")
    if rail and rail.startswith("v") and rail != "v—":
        ready = "%.1fs" % (time.time() - start)
        print("RESULT ready after=%s tries=%s rail=%s errors=%d" % (ready, tries, rail, len(errors)))
        break
    time.sleep(1)

if ready is None:
    print("RESULT not-ready", {
        "readyState": evaluate("document.readyState"),
        "url": (page.get("url") or "")[-60:],
        "WR": evaluate("typeof window.WR"),
        "apiKeys": evaluate("Object.keys((window.pywebview||{}).api||{}).length"),
        "bootDone": evaluate("typeof boot !== 'undefined' ? boot._done : null"),
        "tries": evaluate("typeof boot !== 'undefined' ? boot._tries : null"),
        "rail": evaluate("(document.querySelector('#rail-version')||{}).textContent"),
        "status": evaluate("(document.querySelector('#status-text')||{}).textContent"),
        "bodyLen": evaluate("document.body ? document.body.innerText.length : -1"),
        "errors": errors[:4],
    })
ws.close()
