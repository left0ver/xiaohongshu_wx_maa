# Android OCR & UI Automation Pitfalls Reference

Concrete patterns and fixes discovered during real-device verification on WeChat Channels (`微信视频号`) and Xiaohongshu (`小红书`).

---

## 1. Empty Accessibility Tree (`<hierarchy />`) Latency Trap
- **Symptom**: Single-video collection takes ~245s even though only ~15 UI actions occur.
- **Root Cause**: When an app renders inside a custom surface/WebView where `uiautomator dump` returns `<hierarchy />` (`rootChildren=0`), resetting `self.hierarchy_unavailable = False` on every `click()`, `back()`, or `swipe()` forces the framework to wait for the full `UiDump` timeout (1.3s–2.0s) before falling back to OCR on *every single step*.
- **Fix**: Once an empty accessibility tree is confirmed for the target surface, keep `self.hierarchy_unavailable = True` across navigation actions so `capture()` jumps straight to screenshot + template/OCR.

---

## 2. Transitional Frame Burning the Readiness Budget
- **Symptom**: `ValueError: Author basic information was not ready within 5 seconds` even though the `基础信息` page visibly opens on screen.
- **Root Cause**: At `t = 0.5s` after tapping `更多信息`, the new page is still mid-slide animation. If the readiness probe immediately triggers a full-screen OCR (`~3.5s` on device CPU), the first check fails at `t = 4.0s`, leaving almost no budget for a second check before the 5.0s deadline.
- **Fix**:
  1. Run up to 3 fast local template matches (`BatchBasicTitle` + `BatchChannelsId`, `~100ms` each with `0.12s` sleep) to let the slide-in transition finish cheaply.
  2. When falling back to OCR inside `basic_info_ready`, crop to the left label column ROI (`[0, height // 4, 240, height // 2]`) instead of full screen, cutting OCR latency from `~3.5s` to `<0.6s`.

---

## 3. Consecutive `KEYCODE_BACK` Swallowed by Window Animation
- **Symptom**: After reading `基础信息`, calling `ui.back()` twice (`0.6s` sleep) closes `基础信息` but leaves the underlying Author Profile Drawer open. The next step (`collect_visible_comments`) mistakes a video card's like count on the profile grid for the feed's comment icon and taps into another video.
- **Root Cause**: On Android 12 / OriginOS / MIUI, dismissing a secondary page/dialog takes ~0.8s to return input focus to the underlying drawer; a second `KEYCODE_BACK` sent at `0.6s` is dropped by the window manager.
- **Fix**:
  1. Use a `0.95s` settle delay after `KEYCODE_BACK`.
  2. Add a defensive guard at the start of `collect_visible_comments` and `ensure_feed`: if the screen still shows profile drawer markers (`正在观看`, or video grid card titles near `y > height - 42`), execute an extra `ui.back()` before looking for feed controls.

---

## 4. Short Nickname Click Opening Reply Keyboard Instead of Profile
- **Symptom**: Clicking a 4–6 character commenter nickname opens their profile card/drawer, but clicking a 2-character nickname (`曹明`, `ZY`, `Clark`) opens the `回复 xxx：` input keyboard.
- **Root Cause**: WeChat renders `曹明 辽宁 8月9日` on a single header row. OCR merges the entire string `"曹明辽宁8月9日"` into one bounding box (`w ≈ 240px`). In WeChat's comment drawer, only the nickname text itself opens the user's profile; tapping the region or date text to its right triggers "Reply to comment". Taking `min(w, 140)` clicks at `x + 70`, which lands on `"辽宁"` for a 2-character name (`2 chars ≈ 52px`).
- **Fix**: Restrict `_header_name_box` click width to `min(w, 32)` so the click always lands at `x + 16` (the center of the very first character of the nickname).

---

## 5. Decorative Badges & Icons Misread by OCR
- **Orange Channels Butterfly (`🦋` → `"W"`)**: WeChat Channels draws a small orange butterfly icon next to commenters who have their own Channels account. OCR reads this orange icon as `"W"`, `"V"`, or `"v"`, corrupting `"豪豪看吧"` into `"豪豪看吧W"`.
  - **Fix**: In `ocr_snapshot`, when `label.startswith("comments")`, mask narrow orange pixels (`R > 210, 95 <= G <= 185, B < 65, R - G > 55`) in the comment nickname region (`y > 260, 100 <= x <= 760`) to neutral background gray (`242, 242, 242`) before running OCR.
- **Like Thumb Icon (`👍` → `"凸"`)**: Right-aligned comment like counts (`"9凸"`) can be mistaken for short comment text if only pure digits are filtered.
  - **Fix**: Match `r"\d+\s*[赞凸♡♥]?$"` at `x >= width * 0.72` as right-side like metadata.
- **In-Drawer Ads (`广告`)**: WeChat inserts promotional rows with a right-side `广告` badge inside the comment drawer.
  - **Fix**: Detect `广告` at `x >= width * 0.72` on the same vertical row as a header and skip that entry and its body lines until the next comment header.

---

## 6. Variant UI Labels Across Account Types
Always support these real-world label variants in layout and hierarchy parsers:
- **Author / Commenter Profile Action Buttons**: Public accounts show `关注` / `已关注` + `私信`; private Channels accounts (`已设置私密账号`) show `申请关注` + `私信`.
- **Enterprise vs. Personal Certification**: Personal verified accounts show `认证信息` or `认证`; enterprise accounts show `企业全称` in `基础信息`.
- **Folded Bio & Comment Expand Tokens**: Ellipsis before `展开` / `全文` may be ASCII dots (`...`), Unicode ellipsis (`…` / `⋯`), or Chinese middle dots (`··展开`). Match `[.…⋯·]+\s*(?:展开|全文)$`.
- **Reopened Scrolled Comment Drawer**: Reopening a comment drawer on the same video preserves its previous scroll offset; allow up to 3 `restore_work_header` downward swipes before raising `WorkHeaderMissing`.
