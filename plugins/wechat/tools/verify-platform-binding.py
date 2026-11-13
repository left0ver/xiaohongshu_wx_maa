import argparse
import json
import uuid
from pathlib import Path

import requests


def verify(directory):
    credentials = json.loads((directory / "credentials.json").read_text())
    cards = json.loads((directory / "cards.json").read_text())
    base = credentials["server"]
    headers = {"X-API-Key": credentials["apiKey"]}
    path = directory / "douyin-validation-card.json"
    if not path.exists():
        response = requests.post(base + "/api/v1/external/licenses", headers=headers, json={
            "softwareCode": "qiaomen", "platformCode": "DOUYIN", "planCode": "SMART",
            "validityDays": 1, "count": 1, "ownerPhone": "", "requestId": "platform-check-" + uuid.uuid4().hex}, timeout=15)
        response.raise_for_status()
        card = response.json()["licenses"][0]
        path.write_text(json.dumps(card))
        path.chmod(0o600)
    douyin = json.loads(path.read_text())
    request = {"softwareCode": "qiaomen", "machine": "c6e32678e88a6c93", "licenseKey": cards[0]["licenseKey"]}
    cases = [
        ("legacyClientRejectedWechatLicense", request, 403),
        ("wechatClientRejectedDouyinLicense", {**request, "clientApp": "WECHAT_CHANNELS", "licenseKey": douyin["licenseKey"]}, 403),
        ("anotherDeviceRejected", {**request, "clientApp": "WECHAT_CHANNELS", "machine": "a200b52c0b26719b"}, 409),
        ("boundWechatClientAccepted", {**request, "clientApp": "WECHAT_CHANNELS"}, 200),
    ]
    result = {}
    for name, body, code in cases:
        response = requests.post(base + "/api/v1/licenses/verify", json=body, timeout=15)
        actual = response.json()["code"]
        assert response.status_code == code, (name, response.status_code)
        assert actual == code, (name, actual)
        result[name] = True
    (directory / "platform-validation.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    verify(parser.parse_args().directory)
