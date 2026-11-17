#!/usr/bin/env python3
"""每週更新 index.html 住宿表的房價（資料來源：樂天旅遊 VacantHotelSearch API）。

需要的環境變數（放在 GitHub repo 的 Secrets）：
  RAKUTEN_APP_ID      樂天 Web Service 的應用程式 ID（applicationId）
  RAKUTEN_ACCESS_KEY  樂天 Web Service 的 Access Key（2026 年新版 API 需要）
可選：
  RAKUTEN_REFERER     在樂天登錄的網站網址，預設 https://hby0413.github.io/
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "index.html")
STATE = os.path.join(ROOT, "scripts", "last_prices.json")

APP_ID = os.environ.get("RAKUTEN_APP_ID", "").strip()
ACCESS_KEY = os.environ.get("RAKUTEN_ACCESS_KEY", "").strip()
REFERER = os.environ.get("RAKUTEN_REFERER", "https://hby0413.github.io/").strip()

# 新版（2026 年改版後）與舊版網址；依序嘗試，第一個成功的就用。
ENDPOINTS = [
    "https://openapi.rakuten.co.jp/engine/api/Travel/VacantHotelSearch/20170426",
    "https://app.rakuten.co.jp/services/api/Travel/VacantHotelSearch/20170426",
]

HOTELS = [
    {"key": "lacer", "hotelNo": "193523", "row": "LACER OKINAWA", "checkin": "2027-02-01", "checkout": "2027-02-02", "nights": 1},
    {"key": "okuma", "hotelNo": "17980", "row": "Okuma Private", "checkin": "2027-02-02", "checkout": "2027-02-05", "nights": 3,
     "room_has": "メインコテージ", "room_not": "メゾネット"},
    {"key": "rihga", "hotelNo": "136808", "row": "Rihga Royal Gran", "checkin": "2027-02-05", "checkout": "2027-02-07", "nights": 2},
]
ADULTS = 3
TPE = timezone(timedelta(hours=8))


def log(*a):
    print(*a, flush=True)


def http_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def call_api(hotel, page=1):
    params = {
        "applicationId": APP_ID,
        "format": "json",
        "formatVersion": "2",
        "hotelNo": hotel["hotelNo"],
        "checkinDate": hotel["checkin"],
        "checkoutDate": hotel["checkout"],
        "adultNum": str(ADULTS),
        "roomNum": "1",
        "searchPattern": "1",
        "hits": "30",
        "page": str(page),
    }
    if ACCESS_KEY:
        params["accessKey"] = ACCESS_KEY
    headers = {"Referer": REFERER, "Origin": REFERER.rstrip("/"), "User-Agent": "okinawa-trip-price-bot/1.0"}
    if ACCESS_KEY:
        headers["Authorization"] = "Bearer " + ACCESS_KEY
    last_err = None
    for ep in ENDPOINTS:
        url = ep + "?" + urllib.parse.urlencode(params)
        try:
            data = http_json(url, headers)
            log(f"  OK  {ep.split('/')[2]} hotelNo={hotel['hotelNo']} page={page}")
            return data
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            # 404 + not_found 代表這天沒有空房，不是網址錯誤
            if e.code == 404 and "not_found" in body:
                log(f"  {ep.split('/')[2]}: 查無空房 (404 not_found)")
                return {}
            last_err = f"{ep.split('/')[2]} HTTP {e.code}: {body}"
            log("  FAIL", last_err)
        except Exception as e:  # noqa: BLE001
            last_err = f"{ep.split('/')[2]} {type(e).__name__}: {e}"
            log("  FAIL", last_err)
    raise RuntimeError(last_err or "all endpoints failed")


def iter_rooms(node):
    """在回傳的 JSON 裡找出 (roomBasicInfo, dailyCharge) 配對，相容 formatVersion 1/2。"""
    if isinstance(node, dict):
        if "roomBasicInfo" in node and "dailyCharge" in node:
            yield node["roomBasicInfo"], node["dailyCharge"]
        for v in node.values():
            yield from iter_rooms(v)
    elif isinstance(node, list):
        pending = None
        for item in node:
            if isinstance(item, dict) and "roomBasicInfo" in item and "dailyCharge" not in item:
                pending = item["roomBasicInfo"]
                continue
            if isinstance(item, dict) and "dailyCharge" in item and "roomBasicInfo" not in item and pending is not None:
                yield pending, item["dailyCharge"]
                pending = None
                continue
            yield from iter_rooms(item)


def fetch_rooms(hotel):
    rooms, page = [], 1
    while page <= 5:
        data = call_api(hotel, page)
        if not data:
            break
        rooms += list(iter_rooms(data))
        info = data.get("pagingInfo") or {}
        if isinstance(info, list):
            info = info[0] if info else {}
        if int(info.get("pageCount", 1) or 1) <= page:
            break
        page += 1
    out = []
    for rb, dc in rooms:
        total = dc.get("total") or 0
        try:
            total = int(float(total))
        except (TypeError, ValueError):
            continue
        if total <= 0:
            continue
        out.append({
            "room": rb.get("roomName", ""), "plan": rb.get("planName", ""),
            "breakfast": str(rb.get("withBreakfastFlag", "0")) == "1",
            "dinner": str(rb.get("withDinnerFlag", "0")) == "1",
            "total": total,
        })
    if hotel.get("room_has"):
        out = [r for r in out if hotel["room_has"] in r["room"] and hotel.get("room_not", "\0") not in r["room"]]
    return out


def twd_rate():
    try:
        d = http_json("https://open.er-api.com/v6/latest/JPY")
        r = float(d["rates"]["TWD"])
        log(f"  JPY→TWD 匯率 {r:.4f}")
        return r, True
    except Exception as e:  # noqa: BLE001
        log("  匯率查詢失敗，改用 0.20：", e)
        return 0.20, False


def fmt_yen(n):
    return f"¥{n:,}"


def fmt_twd(n):
    return f"NT${int(round(n, -2)):,}"


def change_note(key, total, state):
    old = state.get(key)
    if not old:
        return ""
    pct = (total - old) / old * 100
    if abs(pct) >= 10:
        return f"；⚠️ 比上次{'漲' if pct > 0 else '跌'} {abs(pct):.0f}%"
    return ""


def replace_price_cell(html, row_marker, new_cell):
    rows = list(re.finditer(r"<tr>.*?</tr>", html, re.S))
    for m in rows:
        if row_marker in m.group(0):
            row = m.group(0)
            cells = list(re.finditer(r"<td>.*?</td>", row, re.S))
            if len(cells) < 4:
                return html, False
            last = cells[-1]
            new_row = row[: last.start()] + new_cell + row[last.end():]
            return html[: m.start()] + new_row + html[m.end():], True
    return html, False


def mark_stale(html, row_marker, today):
    for m in re.finditer(r"<tr>.*?</tr>", html, re.S):
        if row_marker in m.group(0):
            row = m.group(0)
            row2 = re.sub(r"（本週樂天查無空房[^）]*）", "", row)
            row2 = re.sub(r"(</span>)(</td></tr>)$", rf"（本週樂天查無空房，沿用上次價格）\1\2", row2, count=1)
            return html.replace(row, row2)
    return html


def main():
    if not APP_ID:
        log("缺少 RAKUTEN_APP_ID，請在 GitHub repo 的 Settings → Secrets and variables → Actions 加入。")
        return 1
    html = open(HTML, encoding="utf-8").read()
    state = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
    rate, live_rate = twd_rate()
    today = datetime.now(TPE).strftime("%Y/%m/%d")
    ok = 0
    summary = []
    for h in HOTELS:
        log(f"查詢 {h['key']}（hotelNo {h['hotelNo']}，{h['checkin']}→{h['checkout']}，大人 {ADULTS}）")
        try:
            rooms = fetch_rooms(h)
        except Exception as e:  # noqa: BLE001
            log("  錯誤：", e)
            rooms = None
        if not rooms:
            html = mark_stale(html, h["row"], today)
            summary.append(f"{h['key']}: 查無資料")
            continue
        ok += 1
        src = "樂天旅遊"
        if h["key"] == "okuma":
            bf = [r for r in rooms if r["breakfast"] and not r["dinner"]]
            hb = [r for r in rooms if r["dinner"]]
            best_bf = min(bf, key=lambda r: r["total"]) if bf else min(rooms, key=lambda r: r["total"])
            line1 = f"含早 3 晚 {fmt_yen(best_bf['total'])}（約 {fmt_twd(best_bf['total'] * rate)}）"
            extra = ""
            if hb:
                best_hb = min(hb, key=lambda r: r["total"])
                extra = f"含早晚餐 3 晚 {fmt_yen(best_hb['total'])}（約 {fmt_twd(best_hb['total'] * rate)}）；"
            note = f"{extra}{src}最低價方案：{best_bf['plan'][:30]}{change_note('okuma', best_bf['total'], state)}"
            cell = f'<td>{line1}<br><span class="muted">{note}</span></td>'
            state["okuma"] = best_bf["total"]
            if hb:
                state["okuma_hb"] = min(r["total"] for r in hb)
        else:
            best = min(rooms, key=lambda r: r["total"])
            n = h["nights"]
            line1 = f"{n} 晚 {fmt_yen(best['total'])}（約 {fmt_twd(best['total'] * rate)}）" if n > 1 else f"{fmt_yen(best['total'])}（約 {fmt_twd(best['total'] * rate)}）"
            note = f"{src}，{best['room'][:24]}{change_note(h['key'], best['total'], state)}"
            cell = f'<td>{line1}<br><span class="muted">{note}</span></td>'
            state[h["key"]] = best["total"]
        html, done = replace_price_cell(html, h["row"], cell)
        log("  更新" if done else "  找不到住宿表的列", "→", re.sub("<[^>]+>", " ", cell))
        summary.append(f"{h['key']}: {re.sub('<[^>]+>', ' ', cell)}")
    if ok == 0:
        log("三間都查不到，不更新網頁。")
        return 1
    rate_note = "" if live_rate else "，匯率以 0.20 估算"
    html = re.sub(r'(<div[^>]*id="price-updated"[^>]*>)[^<]*(</div>)',
                  rf"\g<1>房價最後更新：{today}（樂天旅遊，每週一凌晨自動更新{rate_note}）\2", html, count=1)
    html = html.replace("<th>2 月實際房價（三人一間）</th>", "<th>2 月實際房價（三人一間・樂天旅遊）</th>")
    open(HTML, "w", encoding="utf-8").write(html)
    json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    log("\n".join(["", "本週結果："] + summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
