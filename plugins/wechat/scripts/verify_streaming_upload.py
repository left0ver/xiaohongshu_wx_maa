import argparse
import json
from collections import Counter
from pathlib import Path


def verify(directory):
    result = json.loads((directory / "collection.json").read_text(encoding="utf-8"))
    timeline = [json.loads(line) for line in (directory / "timings.jsonl").read_text(encoding="utf-8").splitlines()]
    sending = [(index, event) for index, event in enumerate(timeline) if event.get("kind") == "discoverySending"]
    receipts = {event["batchId"]: (index, event) for index, event in enumerate(timeline)
                if event.get("kind") == "discoveryAcknowledged"}
    packets = []
    all_events = []
    for index, event in sending:
        request = json.loads((directory / "uploads" / (event["batchId"] + "-request.json")).read_text(encoding="utf-8"))
        receipt_index, receipt = receipts[event["batchId"]]
        ids = {record["eventId"] for record in request["discoveryEvents"]}
        assert ids == set(receipt["discoveryAckIds"])
        assert index < receipt_index
        assert "licenseKey" not in request
        all_events.extend(request["discoveryEvents"])
        packets.append({"sentAt": event["sentAt"], "acknowledgedAt": receipt["acknowledgedAt"],
                        "request": request, "discoveryAckIds": receipt["discoveryAckIds"]})
    assert packets
    assert [event["eventType"] for event in packets[0]["request"]["discoveryEvents"]] == ["VIDEO"]
    by_id = {event["eventId"]: event for event in all_events}
    assert len(by_id) == len(all_events)
    counts = Counter(event["eventType"] for event in all_events)
    assert counts["VIDEO"] == result["processedCount"]
    expected_comments = sum(len(item["commentsReport"]["events"]) for item in result["videos"])
    expected_users = sum(len(item["commentsReport"]["userEvents"]) for item in result["videos"])
    assert counts["COMMENT"] == expected_comments
    assert counts["USER"] == expected_users
    for event in all_events:
        if event["eventType"] == "COMMENT":
            assert by_id[event["parentEventId"]]["eventType"] == "VIDEO"
        elif event["eventType"] == "USER":
            assert by_id[event["parentEventId"]]["eventType"] == "COMMENT"
        assert event["payload"]["platform"] == "wechat"
        assert not any("douyin" in key.lower() for key in event["payload"])
    for index, event in sending:
        if event["eventTypes"] == ["VIDEO"]:
            continue
        assert "VIDEO" not in event["eventTypes"]
        assert event["eventTypes"].count("COMMENT") <= 1
        assert event["eventTypes"].count("USER") <= 1
    profile_steps = [index for index, event in enumerate(timeline)
                     if event.get("kind") == "taskTiming" and event.get("taskName") == "collect_commenter_profile"
                     and event.get("outcome") == "succeeded"]
    user_ack_steps = [index for index, event in receipts.values() if "USER" in event["eventTypes"]]
    assert len(user_ack_steps) == expected_users
    assert len(profile_steps) == expected_users
    for position, (ack_index, profile_index) in enumerate(zip(sorted(user_ack_steps), profile_steps)):
        assert ack_index < profile_index
        if position:
            assert ack_index > profile_steps[position - 1]
    summary = {"eventCounts": dict(counts), "packetCount": len(packets), "allAcknowledged": True,
               "videoSentOnce": True, "incrementalPacketsVerified": True, "parentLinksVerified": True}
    (directory / "upload-batches.json").write_text(json.dumps(packets, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "upload-validation.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.directory), ensure_ascii=False, indent=2))
