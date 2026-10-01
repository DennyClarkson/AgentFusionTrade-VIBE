"""Start the Python application on loopback. No trading starts on launch."""
import argparse
import threading
import webbrowser

import uvicorn
import httpx

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        with httpx.Client(timeout=1,trust_env=False) as client:
            existing=client.get("http://127.0.0.1:8787/openapi.json")
        if existing.status_code==200 and existing.json().get("info",{}).get("title")=="AgentTradeFusion":
            if not args.no_browser: webbrowser.open("http://127.0.0.1:8787")
            print("AgentTradeFusion is already running at http://127.0.0.1:8787")
            raise SystemExit(0)
    except (httpx.HTTPError,ValueError):
        pass
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:8787")).start()
    uvicorn.run("fusion.app:create_app", factory=True, host="127.0.0.1", port=8787, log_level="info")
