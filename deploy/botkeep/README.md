# TradingAgents VN trên Botkeep

Một tiến trình chạy liên tục gồm:

- **Trang web**: nhập mã cổ phiếu, xem hàng đợi và đọc báo cáo.
- **Bot Telegram**: các lệnh `/phantich`, `/nhanh`, `/theodoi`, `/trangthai`.
- **Báo cáo tự động** cho danh sách theo dõi, sau giờ đóng cửa mỗi ngày giao dịch.

Mỗi lần chỉ phân tích một mã để vừa gói free (2 GB RAM, 2 GB ổ đĩa). Hành động và mọi mức giá do hệ thống định lượng chốt. Đầy đủ các agent AI (4 analyst, Bull/Bear, Trader, 3 risk, Portfolio Manager) diễn giải quanh quyết định đó, và Output Guard kiểm tra lại bằng code.

## 1. Tạo gói upload

Trên máy có mã nguồn:

```bash
python scripts/build_botkeep_bundle.py
```

Lệnh này tạo thư mục `dist/botkeep/` và file `dist/tradingagents-vn-botkeep.zip`. Script báo lỗi nếu vượt giới hạn upload của Botkeep (250 file, 4 MB). Gói chỉ gồm `tradingagents/`, `vnbot/`, `main_botkeep.py`, `requirements.txt` và `.env.example`; test, ảnh, tài liệu và CLI được bỏ ra.

## 2. Tạo server Python trên Botkeep

1. Tạo workload **Python**, chọn phiên bản 3.11 trở lên. Nên cấp khoảng 1,5 GB RAM nếu gói Founder cho phép chia tài nguyên.
2. Upload file zip (hoặc nội dung thư mục `dist/botkeep/`) rồi giải nén vào thư mục gốc của server.
3. Đặt file khởi động là `main_botkeep.py` và file requirements là `requirements.txt`.
4. Copy `.env.example` thành `.env` và điền:
   - **AI**: `ROUTER9_BASE_URL`, `ROUTER9_API_KEY`, `ROUTER9_MODEL`. Nên thêm `GROQ_API_KEY` và `OPENROUTER_API_KEY` làm dự phòng; `XAI_API_KEY` (Grok) tuỳ chọn.
   - **Telegram**: `TELEGRAM_TOKEN` (tạo bằng @BotFather) và `TELEGRAM_ALLOWED_IDS`. Chưa biết id thì cứ khởi động bot, gửi `/id` cho bot, rồi điền vào sau.
   - **Web**: `WEB_PASSWORD` (nên đặt) và `PUBLIC_URL` (địa chỉ Botkeep cấp cho server).
5. Bấm Start. Log phải có dòng `AI provider chain: router9 -> groq -> ...`.

## 3. Kiểm tra sau khi chạy

- Mở trang web, bấm **Chỉ quyết định hệ thống** với mã `FPT`. Nếu ra giá và hành động trong vài giây, tức là server gọi được DNSE và VCI.
  - Nếu báo lỗi kết nối, có thể máy chủ Botkeep (ở nước ngoài) bị DNSE hoặc VCI chặn. Khi đó không chạy được trên host này.
- Gửi `/phantich FPT` cho bot. Quyết định hệ thống tới trước, báo cáo đầy đủ (kèm file HTML) tới sau vài phút.
- `/trangthai` cho biết nhà cung cấp AI nào đang dùng được hoặc đang tạm nghỉ.

## Cách tầng AI dự phòng (giống bot Lan Anh)

Mỗi lượt gọi đi theo `PROVIDER_ORDER`: 9Router → Groq → OpenRouter → Grok.

- Nhà cung cấp lỗi kết nối, timeout, lỗi máy chủ hoặc từ chối yêu cầu thì bị bỏ qua, chuyển sang nhà cung cấp kế tiếp.
- Bị 429 (hết quota) thì nghỉ theo `Retry-After`, hoặc `LLM_QUOTA_COOLDOWN_SEC` nếu không có.
- 9Router lỗi thì nghỉ `ROUTER9_RETRY_SEC` giây rồi mới thử lại.
- Nếu tất cả đang nghỉ, vẫn thử lần lượt từng cái thay vì báo lỗi ngay.

## Giới hạn cần biết

- Một lượt phân tích đầy đủ gọi AI khoảng 15–20 lần và mất vài phút. Model miễn phí có thể chậm hoặc bị giới hạn tốc độ.
- Mỗi lúc chỉ chạy một mã; các yêu cầu khác xếp hàng.
- Khởi động lại server khi đang phân tích thì lượt đó bị đánh dấu lỗi; gửi lại là được.
- Cache giá cũ hơn `CACHE_MAX_AGE_DAYS` ngày và các báo cáo vượt `KEEP_REPORTS` bản được tự xoá để không đầy 2 GB.
- Thông tin chỉ mang tính tham khảo, không phải khuyến nghị đầu tư.
