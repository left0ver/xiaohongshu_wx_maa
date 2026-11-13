import argparse
import json
import subprocess
from pathlib import Path

import requests
from websockets.sync.client import connect


def evaluate(serial, port, expression):
    package = "top.natsuu.mta.wechat_channels.integration"
    pid = subprocess.check_output(["adb", "-s", serial, "shell", "pidof", package], text=True).strip()
    sockets = subprocess.check_output(["adb", "-s", serial, "shell", "cat", "/proc/net/unix"], text=True)
    socket = "webview_devtools_remote_" + pid
    if "@" + socket not in sockets:
        raise RuntimeError("手机 WebView 调试连接尚未准备完成")
    subprocess.run(["adb", "-s", serial, "forward", "tcp:" + str(port), "localabstract:" + socket], check=True, stdout=subprocess.DEVNULL)
    pages = requests.get(f"http://127.0.0.1:{port}/json", timeout=10).json()
    page = next(item for item in pages if item.get("type") == "page" and "webSocketDebuggerUrl" in item)
    with connect(page["webSocketDebuggerUrl"], max_size=4_000_000) as connection:
        connection.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": expression, "returnByValue": True, "awaitPromise": True}}))
        while True:
            response = json.loads(connection.recv(timeout=45))
            if response.get("id") != 1:
                continue
            if "error" in response or "exceptionDetails" in response.get("result", {}):
                raise RuntimeError(json.dumps(response, ensure_ascii=False))
            return response["result"]["result"].get("value")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("serial")
    parser.add_argument("action", choices=["inspect", "authorize", "status", "prepare", "stop", "snapshot", "permission", "disconnect", "reconnect"])
    parser.add_argument("--port", type=int, default=19222)
    parser.add_argument("--card", type=int, default=0)
    parser.add_argument("--directory", type=Path, default=Path("data/laike-integration"))
    args = parser.parse_args()
    expressions = {
        "inspect": "({text:document.body.innerText,inputs:Array.from(document.querySelectorAll('input')).map(e=>({type:e.type,placeholder:e.placeholder}))})",
        "status": "window.__TAURI_INTERNALS__.invoke('wechat_status')",
        "prepare": "window.__TAURI_INTERNALS__.invoke('retry_preparation')",
        "stop": "window.__TAURI_INTERNALS__.invoke('stop_run')",
        "snapshot": "window.__TAURI_INTERNALS__.invoke('get_preparation_status')",
        "permission": "(window.__TAURI_INTERNALS__.invoke('request_privileged_access'), 'requested')",
        "disconnect": "window.__TAURI_INTERNALS__.invoke('wechat_account', {config:{disconnect:true}})",
        "reconnect": "window.__TAURI_INTERNALS__.invoke('set_privileged_backend', {backend:'shizuku'})",
    }
    if args.action == "authorize":
        cards = json.loads((args.directory / "cards.json").read_text())
        credentials = json.loads((args.directory / "credentials.json").read_text())
        value = {"server": credentials.get("server", "http://127.0.0.1:11098"), "softwareCode": "qiaomen", "licenseKey": cards[args.card]["licenseKey"]}
        expression = "window.__TAURI_INTERNALS__.invoke('wechat_account'," + json.dumps({"config": value}) + ")"
    else:
        expression = expressions[args.action]
    print(json.dumps(evaluate(args.serial, args.port, expression), ensure_ascii=False))
