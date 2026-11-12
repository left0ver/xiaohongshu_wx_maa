import argparse
import json
import uuid
from pathlib import Path

import requests
from bs4 import BeautifulSoup


def verify(directory, base, password_file=None, card_index=0, config_file=None):
    directory.mkdir(parents=True, exist_ok=True)
    if password_file is not None and not (directory / "credentials.json").exists():
        credentials = {"username": "admin", "password": password_file.read_text().strip(), "server": base}
        (directory / "credentials.json").write_text(json.dumps(credentials), encoding="utf-8")
        (directory / "credentials.json").chmod(0o600)
    credentials = json.loads((directory / "credentials.json").read_text())
    session = requests.Session()
    login = session.get(base + "/login", timeout=15)
    login.raise_for_status()
    token = BeautifulSoup(login.text, "html.parser").select_one('input[name="_csrf"]')["value"]
    response = session.post(base + "/login", data={"username": credentials["username"], "password": credentials["password"], "_csrf": token}, timeout=15)
    response.raise_for_status()
    assert "/login" not in response.url
    if not credentials.get("apiKeyCreated"):
        settings = session.get(base + "/admin/client-ops", timeout=15)
        settings.raise_for_status()
        form = BeautifulSoup(settings.text, "html.parser").select_one('form[action="/admin/client-ops/api-keys"]')
        fields = {item["name"]: item.get("value", "") for item in form.select("input[name]")}
        fields["name"] = "视频号接入验收"
        created = session.post(base + "/admin/client-ops/api-keys", data=fields, timeout=15)
        created.raise_for_status()
        credentials["apiKey"] = BeautifulSoup(created.text, "html.parser").select_one("#generatedApiKey").text
        credentials["apiKeyCreated"] = True
        (directory / "credentials.json").write_text(json.dumps(credentials), encoding="utf-8")
    headers = {"X-API-Key": credentials["apiKey"]}
    cards_file = directory / "cards.json"
    if cards_file.exists():
        cards = json.loads(cards_file.read_text())
    else:
        response = session.post(base + "/api/v1/external/licenses", headers=headers, json={
            "softwareCode": "qiaomen", "platformCode": "WECHAT_CHANNELS", "planCode": "SMART",
            "validityDays": 7, "count": 2, "ownerPhone": "", "requestId": "wechat-acceptance-" + uuid.uuid4().hex}, timeout=15)
        response.raise_for_status()
        payload = response.json()
        cards = payload.get("licenses", payload.get("items"))
        if cards is None:
            raise ValueError("开卡响应未包含授权列表：" + ",".join(payload.keys()))
        cards_file.write_text(json.dumps(cards), encoding="utf-8")
        cards_file.chmod(0o600)
    page = session.get(base + "/admin/wechat/tasks", timeout=15)
    page.raise_for_status()
    dom = BeautifulSoup(page.text, "html.parser")
    chosen = next(item for item in dom.select('select[name="licenseId"] option')
                  if item.text.split(" · ")[0] == cards[card_index]["licenseKey"])
    page = session.get(base + "/admin/wechat/tasks", params={"licenseId": chosen["value"]}, timeout=15)
    page.raise_for_status()
    dom = BeautifulSoup(page.text, "html.parser")
    form = dom.select_one("form#wechatConfig")
    assert form is not None
    fields = {item["name"]: item.get("value", "") for item in form.select("input[name]")}
    config = {"task": "collect_channels", "options": {"entry": "search", "keyword": "家常菜", "count": 1,
              "videoFilter": "", "commentFilter": "", "commentOptions": {"unlimited": False, "visitProfiles": True,
              "maxPages": 1, "maxReplyExpansions": 0, "maxProfiles": 2}}}
    if config_file is not None:
        config = json.loads(config_file.read_text())
    selected_key = cards[card_index]["licenseKey"]
    before = session.get(base + "/api/v1/external/tasks", params={"licenseKey": selected_key}, headers=headers, timeout=15)
    before.raise_for_status()
    original_tasks = {item["taskId"] for item in before.json()["tasks"]}
    fields["configJson"] = json.dumps(config, ensure_ascii=False)
    saved = session.post(base + "/admin/configs", data=fields, timeout=15)
    saved.raise_for_status()
    after = BeautifulSoup(saved.text, "html.parser")
    restored = json.loads(after.select_one("#wechatInitial").text)
    assert restored == config
    selected_key = next(item for item in dom.select('select[name="licenseId"] option') if item.get("selected") is not None).text.split(" · ")[0]
    tasks = session.get(base + "/api/v1/external/tasks", params={"licenseKey": selected_key}, headers=headers, timeout=15)
    tasks.raise_for_status()
    assert {item["taskId"] for item in tasks.json()["tasks"]} == original_tasks
    bad = {**fields, "configJson": json.dumps({"task": "collect_channels", "options": {"count": 0}})}
    invalid = session.post(base + "/admin/configs", data=bad, timeout=15)
    invalid.raise_for_status()
    assert json.loads(BeautifulSoup(invalid.text, "html.parser").select_one("#wechatInitial").text) == config
    proof = {"cardIndex": card_index, "configurationSaved": True, "saveCreatedNoTasks": True, "invalidInputPreservedConfiguration": True,
             "licenseId": fields["licenseId"], "softwareId": fields["softwareId"], "server": base}
    (directory / f"service-validation-card{card_index}.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(proof, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--server", default="http://127.0.0.1:11098")
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--card", type=int, default=0)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    verify(args.directory, args.server, args.password_file, args.card, args.config)
