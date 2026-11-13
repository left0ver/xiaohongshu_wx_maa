import argparse
import json
import time
import uuid
from pathlib import Path

import requests


def dispatch(directory, card, action, config, wait, callback=None):
    credentials = json.loads((directory / "credentials.json").read_text())
    cards = json.loads((directory / "cards.json").read_text())
    base = credentials.get("server", "http://127.0.0.1:11098") + "/api/v1/external"
    headers = {"X-API-Key": credentials["apiKey"]}
    body = {"licenseKey": cards[card]["licenseKey"], "requestId": "wechat-check-" + uuid.uuid4().hex, "action": action}
    if config is not None:
        body["taskConfig"] = json.loads(config.read_text())
    if callback is not None:
        body["callbackUrl"] = callback
    response = requests.post(base + "/tasks", headers=headers, json=body, timeout=20)
    if not response.ok:
        raise RuntimeError(str(response.status_code) + " " + response.text)
    result = response.json()
    command = result["taskId"]
    evidence = directory / "tasks"
    evidence.mkdir(exist_ok=True)
    (evidence / (command + "-request.json")).write_text(json.dumps({key: value for key, value in body.items() if key != "licenseKey"}, ensure_ascii=False, indent=2))
    previous = None
    deadline = time.monotonic() + wait
    while True:
        (evidence / (command + "-result.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        state = (result["status"], result.get("resultMessage"))
        if state != previous:
            print(json.dumps({"commandId": command, "status": state[0], "message": state[1]}, ensure_ascii=False), flush=True)
            previous = state
        if result["status"] in {"SUCCESS", "FAILED", "EXPIRED"} or time.monotonic() >= deadline:
            break
        time.sleep(3)
        response = requests.get(base + "/tasks/" + command, headers=headers, timeout=15)
        response.raise_for_status()
        result = response.json()
    if wait and result["status"] != "SUCCESS":
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["START", "STOP", "SYNC_CONFIG", "WECHAT_CHANNELS_TASK"])
    parser.add_argument("--card", type=int, default=0)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--callback")
    parser.add_argument("--wait", type=int, default=600)
    parser.add_argument("--directory", type=Path, default=Path("data/laike-integration"))
    args = parser.parse_args()
    dispatch(args.directory, args.card, args.action, args.config, args.wait, args.callback)
