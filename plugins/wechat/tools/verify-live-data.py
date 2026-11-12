import argparse
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import javaproperties
import pymysql
import requests
from bs4 import BeautifulSoup


def verify(directory, properties):
    credentials = json.loads((directory / "credentials.json").read_text())
    cards = json.loads((directory / "cards.json").read_text())
    base = credentials["server"]
    headers = {"X-API-Key": credentials["apiKey"]}
    with properties.open("rb") as source:
        settings = javaproperties.load(source)
    address = urlparse(settings["spring.datasource.url"].removeprefix("jdbc:"))
    connection = pymysql.connect(host=address.hostname, port=address.port or 3306,
                                 user=settings["spring.datasource.username"],
                                 password=settings["spring.datasource.password"],
                                 database=address.path.strip("/"), cursorclass=pymysql.cursors.DictCursor)
    session = requests.Session()
    login = session.get(base + "/login", timeout=15)
    login.raise_for_status()
    csrf = BeautifulSoup(login.text, "html.parser").select_one('input[name="_csrf"]')["value"]
    login = session.post(base + "/login", data={"username": credentials["username"],
                         "password": credentials["password"], "_csrf": csrf}, timeout=15)
    login.raise_for_status()
    assert "/login" not in login.url
    proofs = []
    with connection:
        for index, card in enumerate(cards):
            events = []
            cursor = None
            while True:
                response = session.get(base + "/api/v1/external/content-events", headers=headers,
                                       params={"licenseKey": card["licenseKey"], "limit": 2, "cursor": cursor}, timeout=15)
                response.raise_for_status()
                page = response.json()
                events.extend(page["items"])
                if not page["hasMore"]:
                    break
                assert page["nextCursor"] != cursor
                cursor = page["nextCursor"]
            ids = {event["eventId"] for event in events}
            assert len(ids) == len(events)
            assert all(event["parentEventId"] in ids for event in events if event["parentEventId"])
            assert all(event["payload"]["platform"] == "wechat" for event in events)
            with connection.cursor() as query:
                query.execute("SELECT event_id,event_type FROM content_discovery_event WHERE license_value=%s", (card["licenseKey"],))
                rows = query.fetchall()
                assert {row["event_id"] for row in rows} == ids
                query.execute("SELECT id,device_id FROM license_key WHERE license_value=%s", (card["licenseKey"],))
                license = query.fetchone()
                license_id = license["id"]
                query.execute("SELECT COUNT(*) AS count FROM diagnostic_report WHERE license_value=%s", (card["licenseKey"],))
                diagnostic_count = query.fetchone()["count"]
            status = session.post(base + "/api/v1/client/status", json={"softwareCode": "qiaomen",
                                  "licenseKey": card["licenseKey"], "machine": license["device_id"],
                                  "clientVersion": "0.2.0", "clientVersionCode": 2000, "clientApp": "WECHAT_CHANNELS"}, timeout=15)
            status.raise_for_status()
            assert status.json()["releaseChannel"] == "WECHAT_CHANNELS"
            page = session.get(base + "/admin/wechat/tasks", params={"licenseId": license_id}, timeout=15)
            page.raise_for_status()
            dom = BeautifulSoup(page.text, "html.parser")
            shown = dom.select("table tbody tr details")
            assert len(shown) == min(30, len(events))
            proofs.append({"cardIndex": index, "eventCounts": dict(Counter(item["eventType"] for item in events)),
                           "apiMatchesDatabase": True, "parentsPresent": True, "visibleRows": len(shown),
                           "diagnosticCount": diagnostic_count, "releaseChannel": status.json()["releaseChannel"]})
    (directory / "data-validation.json").write_text(json.dumps(proofs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(proofs, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("properties", type=Path)
    args = parser.parse_args()
    verify(args.directory, args.properties)
