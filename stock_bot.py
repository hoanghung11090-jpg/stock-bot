"""
stock_bot.py - Bot Telegram cảnh báo biến động cổ phiếu (bản gọn, 1 file).

Cảnh báo khi:
  1. Giá vượt dải trên / thủng dải dưới Bollinger Bands (20 phiên, 2 độ lệch chuẩn)
  2. Khối lượng cao: > 1.5 lần TB20 (đã quy đổi theo thời gian trong phiên)
  3. Giá biến động mạnh: >= +2.5% hoặc <= -2.5% so với giá đóng cửa hôm trước
  4. Khối lượng cao + giá sập (ưu tiên cao nhất)
Kèm gợi ý hành động theo quy tắc kỹ thuật (chỉ để tham khảo).

Dữ liệu: API biểu đồ công khai của SSI, dự phòng VNDirect (không cần khoá). Đây là API không
chính thức, có thể thay đổi bất cứ lúc nào; có 2 nguồn để một bên lỗi thì bên kia thay.

Cài:   pip install requests tzdata
Chạy:  set TELEGRAM_BOT_TOKEN=...  &  set TELEGRAM_CHAT_ID=...  &  python stock_bot.py
"""

import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

sys.stdout.reconfigure(line_buffering=True)   # log hiện ngay trên GitHub Actions

# ===================== CẤU HÌNH =====================
SYMBOLS = ["VPB", "VPX", "TCB", "MBB", "SSI"]
POLL_SECONDS = 30
PRICE_PCT = 2.5          # % biến động mạnh
VOL_RATIO = 1.5          # lần so với TB20 đã quy đổi theo thời gian
BB_PERIOD, BB_STD = 20, 2
COOLDOWN_MIN = 15        # không lặp lại cùng cảnh báo trong 15 phút
SNAPSHOT_FILE = "daily_market_snapshot.json"

# >>> DÁN TOKEN BOT TELEGRAM VÀO GIỮA HAI DẤU NGOẶC KÉP DƯỚI ĐÂY <<<
TOKEN = ""
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or TOKEN
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")   # để trống: bot tự tìm khi anh nhắn /start cho bot
TZ = ZoneInfo("Asia/Ho_Chi_Minh")
SESSIONS = [((9, 0), (11, 30)), ((13, 0), (14, 45))]   # bỏ nghỉ trưa
TOTAL_MIN = 255                                          # 150 phút sáng + 105 phút chiều
# Nguồn dữ liệu công khai, không cần khoá. Thử lần lượt: nguồn đầu lỗi thì tự chuyển nguồn sau.
SOURCES = [
    ("SSI", "https://iboard-api.ssi.com.vn/statistics/charts/history", "1D"),
    ("VNDirect", "https://dchart-api.vndirect.com.vn/dchart/history", "D"),
]
HEADERS = {"User-Agent": "Mozilla/5.0"}

last_sent = {}   # key cảnh báo -> thời điểm gửi gần nhất
latest = {}      # mã -> dữ liệu mới nhất (dùng cho snapshot)


# ===================== DỮ LIỆU =====================
def fetch_daily(symbol, now):
    """
    Lấy nến ngày ~60 ngày gần nhất (nến cuối là phiên hôm nay). Giá đơn vị nghìn đồng.
    Thử từng nguồn trong SOURCES, mỗi nguồn tối đa 2 lần.
    """
    for name, url, res in SOURCES:
        params = {"resolution": res, "symbol": symbol,
                  "from": int((now - timedelta(days=60)).timestamp()), "to": int(now.timestamp())}
        for attempt in range(2):
            try:
                r = requests.get(url, params=params, headers=HEADERS, timeout=10)
                if r.status_code == 429:           # bị giới hạn: nghỉ rồi thử lại
                    time.sleep(15)
                    continue
                d = r.json()
                d = d.get("data", d)               # SSI bọc dữ liệu trong "data"
                if d.get("s") == "ok" and d.get("c"):
                    return d
            except (requests.RequestException, ValueError, AttributeError) as e:
                print(f"[{symbol}] {name} lỗi lần {attempt + 1}: {e}")
            time.sleep(2)
        print(f"[{symbol}] {name} không phản hồi, chuyển nguồn khác")
    return None


def analyze(symbol, d, now):
    """Tính các chỉ số từ nến ngày. Trả về dict hoặc None nếu chưa có nến hôm nay."""
    if datetime.fromtimestamp(d["t"][-1], TZ).date() != now.date():
        return None                                # chưa có dữ liệu phiên hôm nay
    closes, vols = d["c"], d["v"]
    price, vol, prev = closes[-1], vols[-1], closes[-2]
    window = closes[-BB_PERIOD:]                   # BB tính cả giá hiện tại (chuẩn TradingView)
    mid = statistics.mean(window)
    sd = statistics.pstdev(window)
    avg20 = statistics.mean(vols[-BB_PERIOD - 1:-1])   # TB20 không tính hôm nay
    frac = max(elapsed_minutes(now), 1) / TOTAL_MIN
    return {
        "symbol": symbol, "time": now.strftime("%H:%M:%S"),
        "price": price * 1000, "prev_close": prev * 1000,
        "pct": (price / prev - 1) * 100,
        "volume": vol, "avg_vol20": avg20,
        "vol_ratio": vol / (avg20 * frac) if avg20 else 0,   # so với KL kỳ vọng tới giờ này
        "bb_upper": (mid + BB_STD * sd) * 1000, "bb_lower": (mid - BB_STD * sd) * 1000,
    }


# ===================== TÍN HIỆU + GỢI Ý =====================
def signals(a, now):
    """Trả về list (key, mô tả, gợi ý). Mỗi tình huống chỉ ra 1 gợi ý chính."""
    vol_high = a["vol_ratio"] >= VOL_RATIO and elapsed_minutes(now) >= 15  # bỏ 15 phút đầu (nhiễu ATO)
    up, down = a["pct"] >= PRICE_PCT, a["pct"] <= -PRICE_PCT
    above, below = a["price"] > a["bb_upper"], a["price"] < a["bb_lower"]
    out = []
    if vol_high and down:
        out.append(("CRASH", "KL cao + giá sập",
                    "Bán tháo có khối lượng. Không bắt đáy. Đang giữ: xem lại điểm cắt lỗ."))
    elif vol_high and (up or above):
        out.append(("BREAKOUT", "Tăng mạnh có KL xác nhận",
                    "Đang giữ: tiếp tục giữ, nâng điểm chốt lời/cắt lỗ. Chưa có: không mua đuổi, chờ nhịp chỉnh."))
    else:
        if above:
            out.append(("BB_UP", "Vượt dải BB trên",
                        "KL chưa xác nhận, dễ là bẫy tăng. Không mua đuổi, theo dõi thêm."))
        if below:
            out.append(("BB_DOWN", "Thủng dải BB dưới",
                        "Quá bán ngắn hạn. Chưa vội bắt đáy, chờ phiên hồi kèm KL."))
        if up and not above:
            out.append(("UP", "Giá tăng mạnh", "Tăng nhưng KL thường. Theo dõi, chưa hành động."))
        if down and not below:
            out.append(("DOWN", "Giá giảm mạnh", "Giảm nhưng KL thường. Kiểm tra lại mức hỗ trợ gần nhất."))
        if vol_high and not (up or down or above or below):
            out.append(("VOL", "KL đột biến, giá chưa chạy",
                        "Có thể đang gom hoặc xả. Chờ giá phá hướng rồi mới quyết."))
    return out


def format_msg(a, sigs):
    """Tin Telegram ngắn gọn (Markdown)."""
    arrow = "▲" if a["pct"] >= 0 else "▼"
    lines = [f"*{a['symbol']}* {arrow} {a['price']:,.0f} ({a['pct']:+.2f}%)"]
    for _, title, advice in sigs:
        lines.append(f"• *{title}*\n  → {advice}")
    lines.append(f"`KL {a['volume']:,.0f} ({a['vol_ratio']:.1f}x) | "
                 f"BB {a['bb_lower']:,.0f}-{a['bb_upper']:,.0f}`")
    lines.append(f"_{a['time']} · gợi ý kỹ thuật, không phải khuyến nghị đầu tư_")
    return "\n".join(lines)


# ===================== TELEGRAM =====================
def find_chat_id():
    """Tự lấy chat id từ tin nhắn gần nhất gửi cho bot (anh chỉ cần nhắn /start cho bot 1 lần)."""
    global CHAT_ID
    if os.path.exists("chat_id.txt"):
        CHAT_ID = open("chat_id.txt").read().strip()
        return
    print("Mở Telegram, nhắn /start cho bot của anh. Đang chờ...")
    while not CHAT_ID:
        try:
            r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=10).json()
            msgs = [u["message"] for u in r.get("result", []) if "message" in u]
            if msgs:
                CHAT_ID = str(msgs[-1]["chat"]["id"])
                open("chat_id.txt", "w").write(CHAT_ID)
                send("Bot đã kết nối. Sẽ báo khi có biến động: " + ", ".join(SYMBOLS))
                return
            if not r.get("ok"):
                print("Token sai, kiểm tra lại:", r.get("description"))
        except requests.RequestException as e:
            print("Lỗi mạng:", e)
        time.sleep(3)


def send(text):
    if not TOKEN or not CHAT_ID:
        print("(chưa cấu hình Telegram)\n" + text + "\n")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", timeout=10,
                      json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"})
    except requests.RequestException as e:
        print("Lỗi gửi Telegram:", e)


# ===================== GIỜ GIAO DỊCH =====================
def elapsed_minutes(now):
    """Số phút giao dịch đã trôi qua trong ngày (không tính nghỉ trưa)."""
    total = 0
    for (h1, m1), (h2, m2) in SESSIONS:
        start = now.replace(hour=h1, minute=m1, second=0, microsecond=0)
        end = now.replace(hour=h2, minute=m2, second=0, microsecond=0)
        if now > start:
            total += (min(now, end) - start).total_seconds() / 60
    return total


def in_session(now):
    t = (now.hour, now.minute)
    return now.weekday() < 5 and any(s <= t < e for s, e in SESSIONS)


# ===================== VÒNG LẶP =====================
def scan(now, notify=True):
    for sym in SYMBOLS:
        d = fetch_daily(sym, now)
        a = analyze(sym, d, now) if d else None
        if not a:
            continue
        latest[sym] = a
        fresh = []
        for s in signals(a, now):
            key = f"{sym}:{s[0]}"
            if now - last_sent.get(key, datetime.min.replace(tzinfo=TZ)) >= timedelta(minutes=COOLDOWN_MIN):
                last_sent[key] = now
                fresh.append(s)
        if fresh and notify:
            send(format_msg(a, fresh))
        time.sleep(0.5)   # giãn cách giữa các mã, tránh bị chặn


def save_snapshot(now):
    """Lưu dữ liệu cuối phiên ra JSON (đầu vào cho phân tích AI)."""
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": now.date().isoformat(), "generated_at": now.isoformat(timespec="seconds"),
                   "symbols": latest}, f, ensure_ascii=False, indent=2)
    print("Đã lưu", SNAPSHOT_FILE)


def run_github_shift():
    """
    Chế độ GitHub Actions: chạy 1 ca rồi thoát.
      - Ca sáng: chờ tới 9:00, quét 30 giây/lần tới 11:30 rồi thoát.
      - Ca chiều: chờ tới 13:00, quét tới 14:45, chờ 15:05 lưu snapshot + gửi tóm tắt rồi thoát.
    """
    print("GitHub Actions - bắt đầu ca:", datetime.now(TZ).strftime("%d/%m %H:%M"))
    if datetime.now(TZ).weekday() >= 5:
        print("Cuối tuần, thoát.")
        return
    announced = False
    while True:
        now = datetime.now(TZ)
        t = (now.hour, now.minute)
        if in_session(now):
            scan(now)
            if not announced:                       # tin báo bot còn sống, 1 lần mỗi ca
                announced = True
                if latest:
                    body = "\n".join(f"{s}: {a['price']:,.0f} ({a['pct']:+.2f}%)" for s, a in latest.items())
                    send(f"Bot bắt đầu ca {now:%H:%M}\n{body}")
                else:
                    send(f"Bot bắt đầu ca {now:%H:%M} nhưng CHƯA lấy được dữ liệu (nguồn lỗi hoặc ngày nghỉ)")
            print(now.strftime("%H:%M:%S"), {s: round(a["pct"], 2) for s, a in latest.items()})
            time.sleep(POLL_SECONDS)
        elif t >= (15, 5):                          # chốt phiên
            scan(now, notify=False)
            save_snapshot(now)
            send_summary(now)
            return
        elif (11, 30) <= t < (12, 30):              # hết ca sáng
            print("Hết phiên sáng, thoát.")
            return
        else:                                       # trước giờ mở cửa / chờ 15:05
            time.sleep(20)


def send_summary(now):
    """Tin tóm tắt cuối ngày."""
    if not latest:
        return                                      # ngày nghỉ lễ: không có dữ liệu
    lines = [f"*Chốt phiên {now:%d/%m}*"]
    for sym, a in latest.items():
        lines.append(f"{sym}: {a['price']:,.0f} ({a['pct']:+.2f}%) · KL {a['vol_ratio']:.1f}x")
    send("\n".join(lines))


def main():
    if not TOKEN:
        print("Chưa có token. Mở stock_bot.py, dán token vào dòng TOKEN rồi chạy lại.")
        if not os.getenv("GITHUB_ACTIONS"):
            input("Nhấn Enter để thoát...")
        return
    if os.getenv("GITHUB_ACTIONS"):
        if not CHAT_ID:
            print("Thiếu secret TELEGRAM_CHAT_ID")
            return
        run_github_shift()
        return
    if not CHAT_ID:
        find_chat_id()
    print("Bot chạy:", ", ".join(SYMBOLS))
    snap_done = None
    while True:
        now = datetime.now(TZ)
        if in_session(now):
            scan(now)
            time.sleep(POLL_SECONDS)
        elif now.weekday() < 5 and (now.hour, now.minute) >= (15, 5) and snap_done != now.date():
            scan(now, notify=False)  # chỉ lấy số chốt phiên, không gửi cảnh báo
            save_snapshot(now)
            snap_done = now.date()
            last_sent.clear()
        else:
            time.sleep(60)


if __name__ == "__main__":
    main()
