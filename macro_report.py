"""
macro_report.py - Bản tin vĩ mô 21:00 thứ 2-6, gửi qua Telegram.

Nội dung:
  1. Nhiệt kế Mỹ: DXY, US10Y, VIX, S&P 500, Nasdaq, S&P 500 futures (phiên Mỹ tối nay)
  2. Châu Á & hàng hoá: Nikkei, KOSPI, Hang Seng, Brent, WTI, Vàng
  3. Việt Nam: VN-Index (vs MA20), USD/VND, USD/CNY
  4. Vĩ mô Mỹ hàng tháng (nếu có FRED_API_KEY): Core PCE YoY, Non-farm Payrolls
  5. Sự kiện sắp tới: đáo hạn phái sinh VN30, NFP dự kiến, + danh sách tự thêm (EVENTS)
  6. Nhiệt kế điểm số (quy tắc cố định) + nhận định AI (nếu có ANTHROPIC_API_KEY)
  7. Đối chiếu: nhận định hôm trước vs VN-Index thực tế (lưu trong predictions.json)

Thứ 2-5: nhận định phiên mai. Thứ 6: nhận định tuần tới.
Tất cả chỉ để tham khảo, không phải khuyến nghị đầu tư.
"""

import json
import os
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

sys.stdout.reconfigure(line_buffering=True)

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
FRED_KEY = os.getenv("FRED_API_KEY", "")
AI_KEY = os.getenv("ANTHROPIC_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "") or "claude-haiku-4-5"
PRED_FILE = "predictions.json"
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Sự kiện tự thêm (ngày, mô tả). Ví dụ họp Fed, công bố PMI, CPI Mỹ...
EVENTS = [
    # ("2026-12-01", "PMI sản xuất Việt Nam (S&P Global)"),
]

# (nhãn, mã Yahoo Finance, kiểu hiển thị)
MARKETS = {
    "us": [("DXY", "DX-Y.NYB", "idx"), ("US10Y", "^TNX", "yield"), ("VIX", "^VIX", "idx"),
           ("S&P 500", "^GSPC", "idx"), ("Nasdaq", "^IXIC", "idx"), ("S&P fut (tối nay)", "ES=F", "idx")],
    "asia": [("Nikkei", "^N225", "idx"), ("KOSPI", "^KS11", "idx"), ("Hang Seng", "^HSI", "idx"),
             ("Brent", "BZ=F", "usd"), ("WTI", "CL=F", "usd"), ("Vàng", "GC=F", "usd")],
    "fx": [("USD/VND", "VND=X", "fx"), ("USD/CNY", "CNY=X", "fx")],
}


# ===================== LẤY DỮ LIỆU =====================
def get_json(url, params=None, headers=None, tries=2):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers or HEADERS, timeout=15)
            if r.status_code == 200:
                return r.json()
            print(f"HTTP {r.status_code}: {url}")
        except (requests.RequestException, ValueError) as e:
            print("Lỗi:", url, e)
        time.sleep(2 * (i + 1))
    return None


def yahoo(symbol):
    """Giá đóng cửa gần nhất và % thay đổi so với phiên trước (Yahoo Finance, API công khai)."""
    for host in ("query1", "query2"):
        d = get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}",
                     {"range": "1mo", "interval": "1d"})
        try:
            res = d["chart"]["result"][0]
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
            last = res["meta"].get("regularMarketPrice") or closes[-1]
            prev = closes[-2] if closes[-1] == last else closes[-1]
            return {"last": last, "chg": last - prev, "pct": (last / prev - 1) * 100}
        except (TypeError, KeyError, IndexError, ZeroDivisionError):
            continue
    return None


def vnindex():
    """VN-Index từ VNDirect: điểm, % ngày, vị trí so với MA20."""
    now = int(time.time())
    d = get_json("https://dchart-api.vndirect.com.vn/dchart/history",
                 {"resolution": "D", "symbol": "VNINDEX", "from": now - 86400 * 60, "to": now})
    if not d or d.get("s") != "ok":
        return None
    c, v = d["c"], d["v"]
    ma20 = statistics.mean(c[-20:])
    return {"last": c[-1], "pct": (c[-1] / c[-2] - 1) * 100, "ma20": ma20,
            "vol_vs_avg": v[-1] / statistics.mean(v[-21:-1]) if len(v) > 21 else None,
            "date": datetime.fromtimestamp(d["t"][-1], TZ).date().isoformat()}


def fred(series, units="lin"):
    """Giá trị mới nhất của chuỗi FRED (cần FRED_API_KEY miễn phí)."""
    if not FRED_KEY:
        return None
    d = get_json("https://api.stlouisfed.org/fred/series/observations",
                 {"series_id": series, "api_key": FRED_KEY, "file_type": "json",
                  "sort_order": "desc", "limit": 2, "units": units})
    try:
        obs = [o for o in d["observations"] if o["value"] != "."]
        return {"date": obs[0]["date"][:7], "value": float(obs[0]["value"]),
                "prev": float(obs[1]["value"]) if len(obs) > 1 else None}
    except (TypeError, KeyError, IndexError, ValueError):
        return None


# ===================== LỊCH SỰ KIỆN =====================
def third_thursday(y, m):
    d = date(y, m, 1)
    first_thu = d + timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + timedelta(days=14)


def first_friday(y, m):
    d = date(y, m, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def upcoming_events(today, days=7):
    """Sự kiện trong `days` ngày tới: đáo hạn phái sinh VN30 (thứ 5 tuần 3), NFP dự kiến (thứ 6 đầu tháng)."""
    out = []
    for k in range(2):
        y, m = (today.year + (today.month + k - 1) // 12, (today.month + k - 1) % 12 + 1)
        out.append((third_thursday(y, m), "Đáo hạn phái sinh VN30"))
        out.append((first_friday(y, m), "Non-farm Payrolls Mỹ (dự kiến, công bố tối giờ VN)"))
    out += [(date.fromisoformat(d), t) for d, t in EVENTS]
    return sorted((d, t) for d, t in out if today < d <= today + timedelta(days=days))


# ===================== NHIỆT KẾ (QUY TẮC CỐ ĐỊNH) =====================
def score(data):
    """
    Cộng/trừ điểm theo quy tắc đơn giản, dễ kiểm chứng. Dương = thuận lợi cho VNI, âm = bất lợi.
    Đây là quy tắc tham khảo, không phải mô hình dự báo.
    """
    pts, why = 0, []

    def add(cond, p, text):
        nonlocal pts
        if cond:
            pts += p
            why.append(f"{'+' if p > 0 else ''}{p} {text}")

    g = lambda k: data.get(k) or {}
    add(g("DXY").get("pct", 0) >= 0.5, -1, "USD mạnh lên (áp lực tỷ giá, khối ngoại)")
    add(g("DXY").get("pct", 0) <= -0.5, +1, "USD yếu đi")
    add(g("US10Y").get("chg", 0) >= 0.08, -1, "Lợi suất Mỹ tăng mạnh")
    add(g("US10Y").get("chg", 0) <= -0.08, +1, "Lợi suất Mỹ giảm mạnh")
    add(g("VIX").get("last", 0) >= 25, -1, "VIX > 25 (thị trường toàn cầu sợ hãi)")
    add(0 < g("VIX").get("last", 99) < 15, +1, "VIX thấp (tâm lý ổn định)")
    us = g("S&P fut (tối nay)").get("pct", g("S&P 500").get("pct", 0))
    add(us >= 1, +1, "Chứng khoán Mỹ tăng mạnh")
    add(us <= -1, -1, "Chứng khoán Mỹ giảm mạnh")
    asia = [g(k).get("pct") for k in ("Nikkei", "KOSPI", "Hang Seng") if g(k).get("pct") is not None]
    if asia:
        a = statistics.mean(asia)
        add(a >= 1, +1, "Châu Á tăng đồng loạt")
        add(a <= -1, -1, "Châu Á giảm đồng loạt")
    add(g("USD/VND").get("pct", 0) >= 0.3, -1, "Tỷ giá USD/VND tăng nhanh")
    add(g("USD/CNY").get("pct", 0) >= 0.5, -1, "Nhân dân tệ mất giá")
    vni = g("VNINDEX")
    if vni:
        add(vni["last"] > vni["ma20"], +1, "VN-Index trên MA20 (xu hướng ngắn hạn tốt)")
        add(vni["last"] < vni["ma20"], -1, "VN-Index dưới MA20")
    label = "Tích cực" if pts >= 2 else "Tiêu cực" if pts <= -2 else "Trung tính"
    return pts, label, why


# ===================== AI NHẬN ĐỊNH (TUỲ CHỌN) =====================
def ai_view(data, pts, label, horizon):
    """Gọi Claude API viết nhận định ngắn. Không có key hoặc lỗi thì trả về None."""
    if not AI_KEY:
        return None
    prompt = (
        f"Dữ liệu thị trường (JSON): {json.dumps(data, ensure_ascii=False, default=str)}\n"
        f"Nhiệt kế quy tắc: {pts} điểm ({label}).\n\n"
        f"Viết nhận định ngắn bằng tiếng Việt về VN-Index cho {horizon}. Tối đa 6 dòng, không markdown phức tạp.\n"
        "- Nêu 2-3 yếu tố chính từ dữ liệu, kịch bản nghiêng về hướng nào và yếu tố có thể làm sai kịch bản.\n"
        "- Phân biệt rõ dữ liệu và nhận định. Không khuyến nghị mua/bán mã cụ thể. Không bịa số liệu ngoài JSON."
    )
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                          headers={"x-api-key": AI_KEY, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"},
                          json={"model": AI_MODEL, "max_tokens": 600,
                                "messages": [{"role": "user", "content": prompt}]})
        if r.status_code != 200:
            print("AI lỗi:", r.status_code, r.text[:300])
            return None
        return "".join(b.get("text", "") for b in r.json()["content"]).strip()
    except (requests.RequestException, KeyError, ValueError) as e:
        print("AI lỗi:", e)
        return None


# ===================== ĐỐI CHIẾU DỰ BÁO =====================
def load_preds():
    try:
        with open(PRED_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def check_yesterday(preds, vni):
    """So nhận định gần nhất (trước phiên hôm nay) với biến động VN-Index thực tế."""
    if not vni:
        return None
    earlier = [d for d in preds if d < vni["date"]]
    if not earlier:
        return None
    p = preds[max(earlier)]
    actual = "Tích cực" if vni["pct"] >= 0.3 else "Tiêu cực" if vni["pct"] <= -0.3 else "Trung tính"
    ok = "đúng" if actual == p["label"] else "sai"
    p["result"] = ok
    hit = [x for x in preds.values() if "result" in x]
    rate = f" | Tỷ lệ đúng {sum(x['result'] == 'đúng' for x in hit)}/{len(hit)}"
    return f"Nhận định {max(earlier)}: {p['label']} → VNI {vni['pct']:+.2f}% ({ok}){rate}"


# ===================== TIN NHẮN =====================
def fmt(label, x, kind):
    if not x:
        return f"{label}: n/a"
    if kind == "yield":
        return f"{label}: {x['last']:.2f}% ({x['chg'] * 100:+.0f}bp)"
    if kind == "fx":
        return f"{label}: {x['last']:,.2f} ({x['pct']:+.2f}%)"
    return f"{label}: {x['last']:,.2f} ({x['pct']:+.2f}%)"


def send(text):
    if not TOKEN or not CHAT_ID:
        print(text)
        return
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", timeout=15,
                          json={"chat_id": CHAT_ID, "text": chunk, "disable_web_page_preview": True})
        print("Telegram:", r.status_code)


def main():
    now = datetime.now(TZ)
    target = now.replace(hour=21, minute=0, second=0, microsecond=0)
    if timedelta(0) < target - now < timedelta(minutes=30):   # GitHub bật sớm thì chờ đúng 21:00
        time.sleep((target - now).total_seconds())
        now = datetime.now(TZ)
    today = now.date()
    friday = now.weekday() == 4
    horizon = "tuần giao dịch tới" if friday else "phiên giao dịch ngày mai"

    data, lines = {}, [f"📊 BẢN TIN VĨ MÔ {now:%d/%m/%Y} 21:00", ""]
    for title, key in (("🇺🇸 Mỹ", "us"), ("🌏 Châu Á & hàng hoá", "asia"), ("💱 Tỷ giá", "fx")):
        lines.append(title)
        for label, sym, kind in MARKETS[key]:
            x = yahoo(sym)
            data[label] = x
            lines.append("  " + fmt(label, x, kind))
            time.sleep(0.5)
        lines.append("")

    vni = vnindex()
    data["VNINDEX"] = vni
    if vni:
        pos = "trên" if vni["last"] > vni["ma20"] else "dưới"
        vol = f", KL {vni['vol_vs_avg']:.1f}x TB20" if vni["vol_vs_avg"] else ""
        lines += ["🇻🇳 Việt Nam", f"  VN-Index: {vni['last']:,.2f} ({vni['pct']:+.2f}%), {pos} MA20 {vni['ma20']:,.0f}{vol}", ""]

    if FRED_KEY:
        pce = fred("PCEPILFE", "pc1")
        nfp = fred("PAYEMS", "chg")
        data["CorePCE_yoy"], data["NFP_change_k"] = pce, nfp
        lines.append("📅 Vĩ mô Mỹ (tháng)")
        if pce:
            lines.append(f"  Core PCE {pce['date']}: {pce['value']:.2f}% YoY")
        if nfp:
            lines.append(f"  NFP {nfp['date']}: {nfp['value']:+,.0f}k việc làm")
        lines.append("")

    ev = upcoming_events(today)
    if ev:
        lines.append("🗓 Sự kiện 7 ngày tới")
        thu = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"]
        lines += [f"  {thu[d.weekday()]} {d:%d/%m}: {t}" for d, t in ev]
        lines.append("")

    pts, label, why = score(data)
    lines.append(f"🌡 Nhiệt kế ({horizon}): {label} ({pts:+d} điểm)")
    lines += [f"  {w}" for w in why] or ["  Không có tín hiệu nổi bật"]

    view = ai_view(data, pts, label, horizon)
    if view:
        lines += ["", "🤖 Nhận định AI", view]

    preds = load_preds()
    check = check_yesterday(preds, vni)
    if check:
        lines += ["", "🎯 " + check]
    preds[today.isoformat()] = {"label": label, "score": pts}
    with open(PRED_FILE, "w", encoding="utf-8") as f:
        json.dump(preds, f, ensure_ascii=False, indent=1)

    lines += ["", "Tham khảo, không phải khuyến nghị đầu tư."]
    send("\n".join(lines))


if __name__ == "__main__":
    main()
