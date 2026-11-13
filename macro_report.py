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

# Sự kiện tự thêm (ngày, mô tả). Ví dụ công bố PMI Việt Nam...
EVENTS = [
    # ("2026-12-01", "PMI sản xuất Việt Nam (S&P Global)"),
]

# Lịch Mỹ chính thức (giờ New York). Nguồn: federalreserve.gov (FOMC), bls.gov (CPI, NFP), bea.gov (PCE).
# Cập nhật khi hết danh sách: bot sẽ nhắc "cần cập nhật lịch".
US_CAL = {
    "Fed công bố lãi suất (FOMC)": ("14:00", ["2026-12-09", "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
                                             "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08"]),
    "CPI Mỹ": ("08:30", ["2026-12-10", "2027-01-13", "2027-02-11", "2027-03-10", "2027-04-13", "2027-05-12",
                         "2027-06-10", "2027-07-14", "2027-08-11", "2027-09-14", "2027-10-14", "2027-11-10",
                         "2027-12-10"]),
    "Non-farm Payrolls": ("08:30", ["2026-12-04", "2027-01-08", "2027-02-05", "2027-03-05", "2027-04-02",
                                    "2027-05-07", "2027-06-04", "2027-07-02", "2027-08-06", "2027-09-03",
                                    "2027-10-08", "2027-11-05", "2027-12-03"]),
    "Core PCE": ("08:30", ["2026-11-25", "2026-12-23"]),
}
NY = ZoneInfo("America/New_York")

# Lãi suất huy động 12 tháng: thay đổi chậm, cập nhật tay khi có biến động (nguồn: bảng lãi suất CafeF).
DEPOSIT_12M = {"updated": "2026-11-12", "big4": 6.8, "max_private": 7.0,
               "max_private_banks": "MBV, PGBank, Sacombank, VCBNeo, VIB"}

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


def vn_money_market():
    """
    Lãi suất qua đêm liên ngân hàng + bơm/hút ròng OMO của NHNN.
    NHNN không có API; lấy từ tiêu đề tin phiên OMO hằng ngày của Người Quan Sát
    (dạng "Phiên 12/11: Ngân hàng Nhà nước hút ròng 447 tỷ đồng, lãi suất neo mức 4,3%").
    """
    import re
    try:
        html = requests.get("https://nguoiquansat.vn/omo-ptag.html", headers=HEADERS, timeout=15).text
    except requests.RequestException as e:
        print("Lỗi lấy lãi suất VN:", e)
        return None
    for title in re.findall(r"Phiên \d{1,2}/\d{1,2}[^<>\"]{10,220}", html):
        m = re.search(r"Phiên (\d{1,2}/\d{1,2}).*?(hút|bơm) ròng (?:hơn |gần |khoảng )?([\d.,]+) tỷ", title)
        r = re.search(r"qua đêm[^%]*?(\d+(?:,\d+)?)%|lãi suất[^%]*?(\d+(?:,\d+)?)%", title)
        if m:
            rate = (r.group(1) or r.group(2)).replace(",", ".") if r else None
            return {"session": m.group(1), "omo": m.group(2), "omo_bn": m.group(3),
                    "overnight": float(rate) if rate else None}
    print("Không tìm thấy tin OMO")
    return None


# ===================== TIN CHÍNH SÁCH =====================
NEWS_FEEDS = [
    "https://vnexpress.net/rss/kinh-doanh.rss",
    "https://cafef.vn/tai-chinh-ngan-hang.rss",
    "https://cafef.vn/thi-truong-chung-khoan.rss",
    "https://cafef.vn/vi-mo-dau-tu.rss",
    "https://cafef.vn/doanh-nghiep.rss",
]

# Tin doanh nghiệp trong danh mục: mã -> mẫu nhận diện trong tiêu đề (regex, chữ thường)
WATCHLIST_NEWS = {
    "VPX": [r"\bvpx\b", r"vpbanks", r"chứng khoán vpbank"],
    "VPB": [r"\bvpb\b", r"vpbank(?!s)"],
    "TCB": [r"\btcb\b", r"techcombank"],
    "MBB": [r"\bmbb\b", r"\bmb ?bank\b", r"ngân hàng quân đội", r"^mb\b"],
    "SSI": [r"\bssi\b"],
}
# Tin có từ khoá này mới được đưa vào (chữ thường)
NEWS_KEYWORDS = ["nhnn", "ngân hàng nhà nước", "lãi suất", "tỷ giá", "thủ tướng", "chính phủ", "nghị định",
                 "thông tư", "công văn", "ủy ban chứng khoán", "ubcknn", "nâng hạng", "ftse", "msci", "fed",
                 "khối ngoại", "margin", "tín dụng", "đầu tư công", "thuế", "gdp", "cpi", "pmi", "vn-index",
                 "thanh tra", "room", "trái phiếu", "omo", "tín phiếu"]
NEWS_EXCLUDE = ["ưu đãi", "đồng hành", "khuyến mãi", "top50", "top 50", "privilege", "giá vàng"]


def fetch_feed_items(now, hours=24):
    """Đọc tất cả RSS một lần: trả về [(thời gian, tiêu đề, link)] trong `hours` giờ qua."""
    import re
    from email.utils import parsedate_to_datetime
    items, seen = [], set()
    for url in NEWS_FEEDS:
        try:
            xml = requests.get(url, headers=HEADERS, timeout=15).text
        except requests.RequestException as e:
            print("Lỗi RSS:", url, e)
            continue
        for block in re.findall(r"<item>(.*?)</item>", xml, re.S):
            t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
            l = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", block, re.S)
            d = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
            if not t:
                continue
            title = re.sub(r"\s+", " ", t.group(1)).strip()
            if title.lower() in seen:
                continue
            try:
                when = parsedate_to_datetime(d.group(1).strip()).astimezone(TZ) if d else now
            except (TypeError, ValueError):
                when = now
            if now - when <= timedelta(hours=hours):
                seen.add(title.lower())
                items.append((when, title, l.group(1).strip() if l else ""))
    return sorted(items, reverse=True)


def watchlist_news(items, limit=10):
    """Tin có nhắc mã/tên doanh nghiệp trong danh mục. Trả về [(mã, thời gian, tiêu đề, link)]."""
    import re
    out = []
    for when, title, link in items:
        low = title.lower()
        if any(k in low for k in NEWS_EXCLUDE):
            continue
        for sym, pats in WATCHLIST_NEWS.items():
            if any(re.search(p, low) for p in pats):
                out.append((sym, when, title, link))
                break
    return out[:limit]


def policy_news_from(items, limit=8):
    """Lọc tin chính sách/vĩ mô theo từ khoá."""
    return [(w, t, l) for w, t, l in items
            if any(k in t.lower() for k in NEWS_KEYWORDS) and not any(k in t.lower() for k in NEWS_EXCLUDE)][:limit]


def policy_news(now, hours=24, limit=8):
    """(Giữ lại để tương thích) Tiêu đề tin chính sách/vĩ mô trong `hours` giờ qua."""
    import re
    from email.utils import parsedate_to_datetime
    items, seen = [], set()
    for url in NEWS_FEEDS:
        try:
            xml = requests.get(url, headers=HEADERS, timeout=15).text
        except requests.RequestException as e:
            print("Lỗi RSS:", url, e)
            continue
        for block in re.findall(r"<item>(.*?)</item>", xml, re.S):
            t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
            l = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", block, re.S)
            d = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
            if not t:
                continue
            title = re.sub(r"\s+", " ", t.group(1)).strip()
            low = title.lower()
            if low in seen or not any(k in low for k in NEWS_KEYWORDS) or any(k in low for k in NEWS_EXCLUDE):
                continue
            try:
                when = parsedate_to_datetime(d.group(1).strip()).astimezone(TZ) if d else now
            except (TypeError, ValueError):
                when = now
            if now - when > timedelta(hours=hours):
                continue
            seen.add(low)
            items.append((when, title, l.group(1).strip() if l else ""))
    return sorted(items, reverse=True)[:limit]


# ===================== LỊCH SỰ KIỆN =====================
def third_thursday(y, m):
    d = date(y, m, 1)
    first_thu = d + timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + timedelta(days=14)


def first_friday(y, m):
    d = date(y, m, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def upcoming_events(today, days=7):
    """Sự kiện VN trong `days` ngày tới: đáo hạn phái sinh VN30 (thứ 5 tuần 3) + danh sách EVENTS."""
    out = []
    for k in range(2):
        y, m = (today.year + (today.month + k - 1) // 12, (today.month + k - 1) % 12 + 1)
        out.append((third_thursday(y, m), "Đáo hạn phái sinh VN30"))
    out += [(date.fromisoformat(d), t) for d, t in EVENTS]
    return sorted((d, t) for d, t in out if today < d <= today + timedelta(days=days))


def us_events(now):
    """Lần công bố kế tiếp của từng sự kiện Mỹ, đổi sang giờ Việt Nam."""
    out = []
    for name, (hhmm, days) in US_CAL.items():
        h, mi = map(int, hhmm.split(":"))
        nxt = [datetime.fromisoformat(d).replace(hour=h, minute=mi, tzinfo=NY).astimezone(TZ) for d in days]
        nxt = [t for t in nxt if t > now]
        out.append((nxt[0] if nxt else None, name))
    return sorted(out, key=lambda x: (x[0] is None, x[0] or now))


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
    mm = data.get("VN_money_market") or {}
    add((mm.get("overnight") or 0) >= 5, -1, "Lãi suất qua đêm ≥ 5% (thanh khoản hệ thống căng)")
    add(mm.get("omo") == "bơm", +1, "NHNN bơm ròng qua OMO")
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
        "- Xem watchlist_news_titles (tin doanh nghiệp VPB, VPX, TCB, MBB, SSI): nêu tác động có thể có.\n"
        "- Xem cả policy_news_titles (tin chính sách VN/quốc tế trong ngày), nêu tin nào có thể ảnh hưởng.\n"
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

    mm = vn_money_market()
    data["VN_money_market"] = mm
    data["VN_deposit_12m"] = DEPOSIT_12M
    lines.append("🏦 Lãi suất VN")
    if mm:
        on = f"{mm['overnight']:.2f}%" if mm["overnight"] is not None else "n/a"
        lines.append(f"  Qua đêm liên NH: {on} (tin phiên {mm['session']})")
        lines.append(f"  OMO phiên {mm['session']}: NHNN {mm['omo']} ròng {mm['omo_bn']} tỷ")
    else:
        lines.append("  Qua đêm liên NH: chưa lấy được")
    d = DEPOSIT_12M
    lines.append(f"  Huy động 12T: Big4 {d['big4']}%, cao nhất {d['max_private']}% (cập nhật {d['updated'][8:]}/{d['updated'][5:7]})")
    lines.append("")

    feed = fetch_feed_items(now)
    wl = watchlist_news(feed)
    data["watchlist_news_titles"] = [f"{sym}: {t}" for sym, _, t, _ in wl]
    lines.append("📌 Tin cổ phiếu danh mục 24h")
    if wl:
        for sym, w, t, link in wl:
            lines.append(f"  • [{sym}] {t}")
            if link:
                lines.append(f"    {link}")
    else:
        lines.append("  Không có tin mới về VPB, VPX, TCB, MBB, SSI")
    lines.append("")

    news = policy_news_from(feed)
    data["policy_news_titles"] = [t for _, t, _ in news]
    lines.append("📰 Tin chính sách & vĩ mô 24h")
    if news:
        for w, t, link in news:
            lines.append(f"  • {w:%H:%M} {t}")
            if link:
                lines.append(f"    {link}")
    else:
        lines.append("  Không có tin đáng chú ý")
    lines.append("")

    thu = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"]
    lines.append("🇺🇸 Lịch sự kiện Mỹ (giờ VN)")
    for t, name in us_events(now):
        if t is None:
            lines.append(f"  {name}: cần cập nhật lịch")
            continue
        soon = " ⚠️" if t - now <= timedelta(days=7) else ""
        lines.append(f"  {thu[t.weekday()]} {t:%d/%m %H:%M}: {name}{soon}")
    lines.append("")

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
        lines.append("🗓 Sự kiện VN 7 ngày tới")
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
