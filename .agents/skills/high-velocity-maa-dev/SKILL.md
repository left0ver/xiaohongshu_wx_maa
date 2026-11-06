---
name: high-velocity-maa-dev
description: >-
  High-efficiency agent engineering workflow for mobile UI automation (MAA / ADB / Shizuku),
  OCR & layout parsing, and structured data collection (WeChat Channels, Xiaohongshu, Douyin).
  Use this skill whenever developing, debugging, optimizing, or verifying mobile collector/interaction
  features, or when asked to work with high speed, evidence-driven debugging, and checklist discipline.
---

# High-Velocity Mobile Automation & Collector Development

Follow this workflow to achieve 3–5x faster iteration speed and near-100% first-pass accuracy on real Android devices when building or debugging mobile collection and interaction agents.

---

## 1. Core Principles (Why This Workflow Is Fast)

1. **Evidence First, Code Second**: Never guess why a UI step failed or why a field was misparsed, and never start by running a 2-minute live phone test just to "see what happens." Always inspect existing run artifacts (`result.json`, `events.jsonl`, `*.ocr.json`, `*.xml`, `*.png`) and SQLite rows first.
2. **1-Second Host Test Loop Over 120-Second Device Loop**: Extract real `.ocr.json` / `.xml` snapshots from the device into unit tests (`test_*.py`). Debug layout boundaries, regexes, and state machines in **~1s on the host** until all tests pass, then push to the phone once for end-to-end verification.
3. **Aggressive Parallel Tool Calls**: Batch independent file reads, grep searches, and ADB inspection commands into a single turn instead of serializing them across multiple turns.
4. **Checklist-Driven Truth**: Track progress in paired checklists—an **App Behavior Checklist** (`*-behavior-checklist.md`) and a **Data & Upload Master Checklist** (`*-checklist.md`). Only check off items when backed by real-device run IDs, timing metrics, and SQLite verification.

---

## 2. The 6-Step Execution Loop

### Step 1: Align on Dual Checklists
Before writing code for a new feature batch:
- Check `docs/*-behavior-checklist.md` (UI navigation, drawer handling, gestures, latency budgets, abnormal recovery) and `docs/*-checklist.md` (payload fields, `VIDEO → COMMENT → USER` foreign keys, deduplication keys, SQL acceptance).
- Identify the smallest self-contained batch (e.g., *Batch 1: Video + Author*, *Batch 2 & 3: Comments + Commenter Identity*, *Batch 4: Interactions & Recovery*).

### Step 2: Forensic Inspection of On-Device Artifacts
Before modifying any parser or UI flow, inspect the latest runs on the device (or local artifact dir):
```bash
# Always specify -s <serial> when multiple ADB devices/emulators may exist
adb -s <SERIAL> shell "ls -lt /data/local/tmp/<app-dir>/data/runs | head -n 10"
```
Read the three critical files from the most recent run(s) in parallel:
1. **`result.json`**: Check `status`, `error`, `missingFields`, `skippedProfiles`, and extracted payload values.
2. **`events.jsonl`**: Diff timestamps between consecutive steps to locate latency bottlenecks (e.g., repeated `UiDump` timeouts or full-screen OCR calls inside tight readiness loops) and see the exact last action before an exception.
3. **`*.ocr.json` & `.png` screenshots**: Pull the screenshot and OCR JSON for the exact failing step (`adb -s <SERIAL> pull ...`). Compare what OCR actually emitted (`text`, `box`) against the visual screenshot.

### Step 3: Architecture & Latency Optimization Rules
Apply these rules whenever touching UI recognition or navigation code (see [Android OCR & UI Pitfalls](./references/android-ocr-ui-pitfalls.md) for concrete patterns):
- **Negative-Cache Empty Accessibility Trees**: When an app (`UiDump`) returns an empty hierarchy (`<hierarchy />`), persist `hierarchy_unavailable = True` across clicks, swipes, and back presses instead of resetting it on every action and wasting 1.3s+ per step.
- **Tiered Readiness Probes (Template → Narrow ROI OCR → Full OCR)**: Inside time-budgeted readiness loops (e.g., 5s budget for `基础信息`), never fire a 3.5s full-screen OCR on the first transitional frame. Try 2–3 fast local template matches (~100ms each) first, then fall back to a narrow label-column ROI (`[0, h//4, 240, h//2]`).
- **Precise Click Geometry on Merged OCR Boxes**: OCR frequently merges `[Nickname][Tag/Region][Date]` into one wide bounding box. Clicking the box center or `min(w, 140)` on a 2-character nickname taps the region/date area (which triggers the reply keyboard instead of opening the user profile). Always click the leftmost character region (`min(w, 32)`, i.e., `x + 16`).
- **Pre-OCR Pixel Hygiene for Colored Badges**: When small colored icons next to text (such as WeChat Channels' orange butterfly badge `🦋`) are consistently misread by OCR as characters (`"W"`), mask that narrow RGB range to neutral gray in the snapshot before running OCR on comment drawers.
- **Self-Healing Entry & Modal Settle Timing**:
  - Give `KEYCODE_BACK` enough settle time (`~0.9–0.95s` on OriginOS/MIUI) when chaining consecutive `ui.back()` calls so the window manager does not swallow the second back press during modal dismiss animations.
  - Detect and recover from residual drawers/modals at the start of `ensure_feed` / `collect_visible_comments`, and allow multiple `restore_work_header` downward swipes when reopening a previously scrolled comment drawer.

### Step 4: Fast Host Unit-Test Verification (~1s)
1. Turn every real-world bug found in Step 2 into a deterministic unit test in `test_*.py` using the actual OCR boxes/texts from the device.
2. Run the full unit test suite on the host before touching the device:
   ```bash
   python3 -m unittest discover -s <collector_dir> -p "test_*.py"
   ```
3. Do not proceed to device testing until 100% of host unit tests pass.

### Step 5: Sync to Device & Run End-to-End Verification
1. Push updated files and run the unit test suite on the device runtime first to catch any environment/import differences (`PYTHONPATH`, `pure.zip`, native libraries):
   ```bash
   adb -s <SERIAL> push <collector_dir>/*.py /data/local/tmp/<app-dir>/pi/collector/
   adb -s <SERIAL> shell "/data/local/tmp/<app-dir>/pi/collector/run-phone.sh --test"
   ```
2. Run the live end-to-end collection/interaction flow on the target device:
   ```bash
   adb -s <SERIAL> shell "/data/local/tmp/<app-dir>/pi/collector/run-phone.sh --comments --no-launch"
   ```
3. If any edge case appears on the live feed (e.g., private account `申请关注`, enterprise certification `企业全称`, middle-dot bio expand `··展开`), immediately inspect that run's `.ocr.json` + `.png`, add a unit test, fix the code, and re-verify on the live device.

### Step 6: SQLite Integrity Check & Checklist Update
1. Verify the on-device SQLite database (`videos.sqlite3`) using `acceptance.sql` and foreign-key integrity queries:
   - Confirm `comment_drafts.parent_event_id == video_drafts.event_id`.
   - Confirm `user_drafts.parent_event_id == comment_drafts.event_id`.
   - Confirm `authorDouyinId` and `replyToDouyinId` are backfilled on comments after visiting commenter profiles.
   - Confirm re-running on the same item preserves the original `event_id`, `device_id`, and `collected_at` (idempotency).
2. Update `docs/*-behavior-checklist.md` and `docs/*-checklist.md` with the exact date, device model, Android version, run ID, latency numbers, and SQLite record counts.
