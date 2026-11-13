# Stock Bot

Bot Telegram theo dõi VPB, VPX, TCB, MBB, SSI. Bot quét 30 giây/lần trong giờ giao dịch, thứ 2–6, và chạy trên máy chủ GitHub nên không cần bật máy tính.

Bot cảnh báo các trường hợp sau, kèm gợi ý kỹ thuật:

- Giá vượt dải Bollinger Bands
- Khối lượng cao
- Giá tăng/giảm mạnh
- Khối lượng cao kèm giá sập

Dữ liệu lấy từ SSI, dự phòng VNDirect.

## Cài đặt (1 lần, khoảng 5 phút)

1. **Tạo repo:** vào GitHub, chọn New repository, đặt tên `stock-bot`, chọn **Public** (để miễn phí số phút chạy). Sau đó upload 3 thứ: `stock_bot.py`, `README.md` và thư mục `.github`.
2. **Lấy Chat ID:** nhắn `/start` cho bot trên Telegram. Sau đó mở trình duyệt, vào
   `https://api.telegram.org/bot<TOKEN>/getUpdates` (thay `<TOKEN>` bằng token của anh).
   Tìm đoạn `"chat":{"id":123456789`, dãy số đó là Chat ID.
3. **Thêm Secrets:** trong repo, vào Settings → Secrets and variables → Actions → New repository secret. Thêm 2 secret:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. **Chạy thử:** vào tab Actions → Stock Bot → Run workflow. Trong giờ giao dịch, bot sẽ bắt đầu quét. Ngoài giờ, bot thoát ngay, như vậy là bình thường.

Sau đó bot tự chạy mỗi ngày thứ 2–6.

## Lưu ý

- **Giờ chạy:** GitHub có thể bật bot trễ vài phút. Bot được hẹn 8:40 và 12:40 để bù khoảng trễ đó.
- **Ngày nghỉ lễ:** không có dữ liệu thì bot không gửi gì.
- **Snapshot cuối phiên:** lưu ở `daily_market_snapshot.json` và thư mục `snapshots/`.
- **Repo bị tạm ngưng:** GitHub tự tắt lịch chạy nếu repo không có hoạt động trong 60 ngày. Snapshot được commit mỗi ngày nên repo luôn có hoạt động, thường không bị tắt.
- **Đổi ngưỡng hoặc danh mục:** sửa phần CẤU HÌNH ở đầu file `stock_bot.py`.
