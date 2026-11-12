import argparse
import copy
import json
from pathlib import Path

import requests


def verify(directory, command, card):
    credentials = json.loads((directory / "credentials.json").read_text())
    cards = json.loads((directory / "cards.json").read_text())
    request = json.loads((directory / "tasks" / (command + "-request.json")).read_text())
    request["licenseKey"] = cards[card]["licenseKey"]
    address = credentials["server"] + "/api/v1/external/tasks"
    headers = {"X-API-Key": credentials["apiKey"]}
    repeated = requests.post(address, json=request, headers=headers, timeout=15)
    repeated.raise_for_status()
    assert repeated.json()["taskId"] == command
    changed = copy.deepcopy(request)
    changed["taskConfig"]["options"]["count"] += 1
    response = requests.post(address, json=changed, headers=headers, timeout=15)
    assert response.status_code == 400
    wrong_device = copy.deepcopy(request)
    wrong_device["licenseKey"] = cards[1 - card]["licenseKey"]
    response = requests.post(address, json=wrong_device, headers=headers, timeout=15)
    assert response.status_code == 400
    result = {"commandId": command, "duplicateReturnedOriginal": True,
              "changedParametersRejected": True, "anotherLicenseRejected": True}
    (directory / "request-validation.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("command")
    parser.add_argument("--card", type=int, default=1)
    args = parser.parse_args()
    verify(args.directory, args.command, args.card)
