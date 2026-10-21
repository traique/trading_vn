# TradingAgents VN trên Botkeep

Hướng dẫn chạy TradingAgents VN liên tục trên Botkeep (hoặc bất kỳ máy chủ Python nào luôn bật). Một tiến trình `main_botkeep.py` gồm:

- **Trang web**: nhập mã cổ phiếu, xem hàng đợi, đọc báo cáo HTML của các agent, sửa danh sách theo dõi.
- **Bot Telegram**: `/phantich`, `/phanbien`, `/nhanh`, `/theodoi`, `/trangthai`, `/id`.
- **Báo cáo tự động** cho danh sách theo dõi sau giờ đóng cửa mỗi ngày giao dịch (ngày lễ tự bỏ qua).
- **Bot giao dịch** chạy ba chiến lược song song (Phòng thủ - mặc định, Quant Engine và Động lượng VN30), mỗi chiến lược một tài khoản ảo, kèm (tuỳ chọn) lệnh đề xuất cho tài khoản thật. **Mỗi đêm bot tự hiệu chỉnh ngưỡng** của Quant Engine và Động lượng (Phòng thủ giữ bộ đã kiểm định). Không bao giờ gửi lệnh tới CTCK.

Hành động (BUY / HOLD / WATCH / SELL / NO_TRADE) và mọi mức giá do **Quant Engine** chốt bằng code. Các agent AI (4 analyst, Bull/Bear, Trader, 3 risk, Portfolio Manager) diễn giải quanh quyết định đó, rồi **Output Guard** kiểm tra lại bằng code. Mỗi lần chỉ phân tích một mã để vừa gói free (2 GB RAM, 2 GB ổ đĩa).

---

## 1. Chuẩn bị

| Cần có | Lấy ở đâu |
|---|---|
| Ít nhất một nhà cung cấp AI | 9Router (endpoint kiểu OpenAI), hoặc key Groq / OpenRouter / xAI |
| Bot Telegram (tuỳ chọn) | Chat với [@BotFather](https://t.me/BotFather) → `/newbot` → lấy token |
| Mật khẩu trang web | Tự đặt, **bắt buộc** nếu muốn dùng trang web |

Dữ liệu thị trường (DNSE, VCI, CafeF, Google News) không cần key.

## 2. Tạo gói upload

Trên máy có mã nguồn (Python 3.11+):

```bash
git clone https://github.com/traique/trading_vn.git
cd trading_vn
python scripts/build_botkeep_bundle.py
```

Lệnh tạo `dist/botkeep/` và `dist/tradingagents-vn-botkeep.zip` (~100 file, ~230 KB). Script báo lỗi nếu vượt giới hạn upload của Botkeep (250 file, 4 MB). Gói chỉ gồm `tradingagents/`, `vnbot/`, `main_botkeep.py`, `requirements.txt`, `.env.example`, README này và LICENSE; test, ảnh, CLI được bỏ ra.

## 3. Tạo server trên Botkeep

1. Tạo workload **Python 3.11 trở lên**. Nếu gói cho phép chia tài nguyên, cấp khoảng 1,5 GB RAM.
2. Upload file zip rồi giải nén vào **thư mục gốc** của server (hoặc upload nội dung `dist/botkeep/`).
3. File khởi động: `main_botkeep.py`. File requirements: `requirements.txt`.
4. Copy `.env.example` thành `.env` (cùng thư mục với `main_botkeep.py`) và điền tối thiểu:

```env
# AI - ít nhất một dòng có key
ROUTER9_BASE_URL=https://<9router-cua-ban>/v1
ROUTER9_API_KEY=...
ROUTER9_MODEL=gpt-4o-mini
GROQ_API_KEY=...            # dự phòng, nên có
OPENROUTER_API_KEY=...      # dự phòng, nên có

# Web
WEB_PASSWORD=mat-khau-dai-kho-doan
PUBLIC_URL=https://<ten-server>.botkeep...   # địa chỉ Botkeep cấp

# Telegram
TELEGRAM_TOKEN=123456:ABC...
TELEGRAM_ALLOWED_IDS=        # để trống lúc đầu, xem bước 4
```

5. Bấm **Start**. Log khởi động cần có:
   - `AI provider chain: router9 -> groq -> ...` (nếu thấy `No AI provider configured` là thiếu key).
   - Nếu thấy `WEB_PASSWORD is not set` thì trang web đang khoá: thêm mật khẩu rồi khởi động lại.

Botkeep tự đặt `SERVER_PORT`; chạy nơi khác thì dùng `PORT` (mặc định 8080).

## 4. Lấy Telegram id và mở quyền

1. Mở bot trên Telegram, gửi `/id`. Bot trả về id của bạn (với tài khoản chưa được phép, bot chỉ trả lời lệnh này).
2. Điền id vào `.env`: `TELEGRAM_ALLOWED_IDS=123456789` (nhiều người thì cách nhau dấu phẩy).
3. Muốn báo cáo hằng ngày gửi vào một nhóm: thêm bot vào nhóm, lấy id nhóm (số âm) và đặt `TELEGRAM_REPORT_CHAT_IDS`. Bỏ trống thì gửi cho các id trong `TELEGRAM_ALLOWED_IDS`.
4. Khởi động lại server.

## 5. Kiểm tra sau khi chạy

1. Mở `PUBLIC_URL`. Trình duyệt hỏi đăng nhập: **tên gì cũng được**, mật khẩu là `WEB_PASSWORD`.
2. Nhập `FPT`, bấm **Chỉ quyết định hệ thống**. Ra hành động và mức giá trong vài giây là server đã gọi được DNSE/VCI.
   - Báo lỗi kết nối: có thể DNSE/VCI chặn IP máy chủ Botkeep (ở nước ngoài). Khi đó không chạy được trên host này.
3. Gửi `/phantich FPT` cho bot: quyết định hệ thống tới trước, báo cáo đầy đủ (kèm file HTML và link web) tới sau vài phút.
4. `/trangthai`: xem hàng đợi và nhà cung cấp AI nào đang sẵn sàng hay đang nghỉ.

## 6. Cách dùng

### Bot Telegram

| Lệnh | Việc |
|---|---|
| `/phantich FPT` | Phân tích đầy đủ bằng các agent (vài phút) |
| `/phantich FPT 2026-10-01` | Phân tích tại một ngày trong quá khứ (chỉ dùng dữ liệu có đến ngày đó) |
| `/nhanh FPT` | Chỉ quyết định của Quant Engine, không gọi AI (vài giây) |
| `/phanbien FPT [YYYY-MM-DD]` | AI phản biện quyết định của Quant Engine: **1 lượt gọi AI**, trả lời đồng ý/phản đối kèm lý do |
| `/phanbien` | AI phản biện đúng bao nhiêu (kết quả bóng của các lần phản biện) |
| `/theodoi FPT,HPG,VCB` | Đặt danh sách báo cáo tự động (tối đa 30 mã) |
| `/theodoi` | Xem danh sách đang theo dõi |
| `/trangthai` | Hàng đợi và trạng thái các nhà cung cấp AI |
| `/danhmuc` | Danh mục cả ba tài khoản ảo (và tài khoản thật ở chế độ `signal`) |
| `/lenh` | Lệnh bot đặt cho phiên tới |
| `/hieuchinh` | Ngưỡng Quant Engine đang dùng và kết quả tự hiệu chỉnh 5 đêm gần nhất |
| `/chay` | Chạy ngay (kèm chạy bù) các phiên đã đóng mà bot chưa xử lý; phiên đã chạy thì bỏ qua. Trước 15:05 chỉ tính tới phiên hôm trước |
| `/von 500000000` | Đặt tiền mặt tài khoản thật (chế độ `signal`) |
| `/khop mua FPT 100 56300 [YYYY-MM-DD]` | Báo đã khớp lệnh thật (ngày mặc định hôm nay); bán: `/khop ban FPT 100 58000` (chế độ `signal`). Bot từ chối lệnh không thể xảy ra: không đủ tiền, bán cổ phiếu chưa về (mua phiên T bán được từ chiều T+2), ngày tương lai/cuối tuần, hoặc ngày trước lệnh đã ghi gần nhất |
| `/id` | Telegram id của bạn |

Mã nhận được các dạng `FPT`, `fpt`, `FPT.VN`, `HOSE:FPT`. Chỉ số (`VNINDEX`, `VN30`...) không có quyết định giao dịch nên bị từ chối.

### Trang web

- **Phân tích đầy đủ**: xếp hàng như `/phantich`; trang tự tải lại đến khi xong.
- **Chỉ quyết định hệ thống**: như `/nhanh`.
- **Theo dõi hằng ngày**: sửa danh sách watchlist.
- Bảng **Nhà cung cấp AI**: thứ tự dự phòng và ai đang nghỉ vì lỗi/quota.
- Trang **Bot giao dịch** (`/bot`): biểu đồ NAV, hai danh mục ảo, ngưỡng và lịch sử hiệu chỉnh; nút **Chạy phiên ngay** như `/chay`.
- Nút mặt trời/mặt trăng trên thanh đầu trang đổi giao diện sáng/tối.

### Báo cáo tự động

Hai lịch chạy độc lập (giờ Việt Nam):

1. **Bot giao dịch** (nếu `TRADE_MODE` khác `off`), mỗi ngày lúc `TRADE_TIME` (mặc định = `DAILY_REPORT_TIME` nếu sau 15:05, ngược lại 15:30; **không bao giờ trước 15:05**, khi nến ngày mới chốt): khớp các phiên chưa xử lý, ra quyết định, đặt lệnh cho phiên tới cho cả ba chiến lược, gửi báo cáo 🛡️ Phòng thủ, 🤖 Quant Engine, 🚀 Động lượng và 📊 so sánh qua Telegram. Sau đó **tự hiệu chỉnh** (nếu `AUTO_CALIBRATE=1`) trên phiên vừa xử lý, gửi kết quả ⚙️.
2. **Sàng lọc watchlist rồi mới gọi AI**, thứ Hai - thứ Sáu lúc `DAILY_REPORT_TIME` (có thể đặt buổi sáng; trước giờ đóng cửa phân tích dùng phiên đã đóng gần nhất). Quant Engine xét mọi mã (không tốn lượt AI) và gửi một tin tóm tắt; chỉ `DAILY_AI_MAX` (mặc định 5) mã có hành động trong `DAILY_AI_ACTIONS` (mặc định `BUY,SELL`), xếp theo độ tin cậy của engine, mới được gửi AI, và chỉ khi hạn mức AI trong ngày còn đủ. Cách gửi theo `DAILY_AI_MODE`:
   - `critic` (mặc định, giống bot OKX): **mỗi mã 1 lượt gọi AI**. AI được giao nhiệm vụ tìm lý do để từ chối quyết định của hệ thống và trả lời đồng ý/phản đối, độ chắc, lý do, rủi ro chính. AI không đổi hành động hay mức giá, bot giao dịch không đi theo AI.
   - `full`: chuỗi agent đầy đủ như `/phantich` (khoảng 25 lượt gọi mỗi mã).
   - `off`: chỉ gửi kết quả của Quant Engine.

   **Kết quả bóng:** mỗi lần phản biện được lưu (`data/critic_log.json`) và chấm trên các phiên sau đó. Lệnh MUA: vào giá đóng cửa ngày quyết định, chạm stop trước (−1R), chạm mục tiêu (+R:R), hoặc hết 20 phiên. Lệnh BÁN: giá sau 10 phiên. Tin hằng ngày và `/phanbien` cho biết các lệnh AI phản đối kết thúc ra sao, tức AI chặn đúng bao nhiêu phần trăm. 5 kết quả gần nhất được đưa vào lời nhắc của lần phản biện sau, không tốn thêm lượt gọi.

   Mã khác muốn AI xem thì gửi `/phanbien MÃ` (1 lượt) hoặc `/phantich MÃ` (đầy đủ).

- **Ngày lễ:** bot giao dịch chỉ xử lý những phiên VNINDEX thực sự có nến, nên ngày lễ tự bỏ qua; phân tích watchlist cũng bỏ qua ngày lễ khi chạy sau 15:05.
- **Chạy bù:** mỗi lần chạy (kể cả lúc server khởi động), bot xử lý lần lượt mọi phiên bị lỡ từ phiên cuối đã xử lý, mỗi phiên chỉ dùng dữ liệu tới phiên đó. Bù tối đa 20 phiên; phiên cũ hơn bị bỏ qua và lệnh chờ của chúng bị huỷ (báo cáo có ghi).
- **Lỗi dữ liệu:** bot thử lại sau 10, 20, 40, 60... phút (tối đa 6 lần/ngày), báo Telegram một lần; phiên chưa xử lý được chạy bù lần sau.
- **Không chạy trùng:** mỗi tài khoản ảo ghi lại phiên cuối đã xử lý; chạy lại (lịch, `/chay`, nút web) không khớp/đặt lệnh hai lần. Các file trạng thái được ghi nguyên tử.

### Bot giao dịch

- **Bắt đầu:** để `TRADE_MODE=paper` (mặc định) vài tháng. Theo dõi NAV ba chiến lược so với VNINDEX trên trang `/bot` hoặc bằng `/danhmuc`.
- **Chọn chiến lược cho tín hiệu thật:** `SIGNAL_STRATEGY=factor` (Phòng thủ, mặc định), `momentum` hoặc `engine`.
  - Phòng thủ: được chọn qua kiểm định độc lập ngày 15/10/2026 (xem README chính): 2013–10/2026 15,6%/năm, sụt giảm tối đa −26%; giai đoạn TEST 2024–2026 chỉ 8,1%/năm, thua VNINDEX (15,9%). Lợi thế chính là giảm rủi ro. Mỗi lần dựng rổ (cuối tháng) bot đọc dữ liệu khoảng 400 mã HOSE từ VCI; lần đầu mất vài phút.
  - Động lượng: con số backtest cũ tính trên rổ VN30 hôm nay (thiên lệch sống sót); trên rổ point-in-time sụt giảm tối đa 33–49%.
  - Quant Engine: rất ít lệnh và rủi ro thấp.
- **Khi muốn nhận lệnh cho tài khoản thật:**
  1. Đặt `TRADE_MODE=signal` rồi khởi động lại.
  2. Gửi `/von <tiền mặt>`.
  3. Mỗi tối bot gửi danh sách **MUA** (mã, khối lượng, giá tối đa, stop, T1/T2) và cảnh báo **chạm stop / T1 / chuyển SELL** cho các mã bạn đang giữ.
  4. Sau khi khớp trên app CTCK, báo `/khop` để bot cập nhật danh mục thật. Stop/T1/T2 lấy theo tín hiệu BUY gần nhất của mã đó.
- **Đêm đầu tiên chạy lâu hơn** (khoảng 20–30 phút): bot tải lịch sử khối ngoại, tự doanh và ETF cho cả rổ, cùng khoảng 400 phiên độ rộng HOSE (mỗi phiên một lượt gọi). Dữ liệu được lưu dần, nên khởi động lại giữa chừng không mất phần đã tải.
- **Cách tự hiệu chỉnh và các giới hạn:** xem README chính, mục "Bot giao dịch tự hiệu chỉnh". Muốn tắt: `AUTO_CALIBRATE=0`. Muốn quay về ngưỡng gốc: xoá `data/results/quant_params.json`.

## 7. Hàng đợi và bảo vệ quota

- Mỗi lúc chạy **một** mã; yêu cầu khác xếp hàng.
- Yêu cầu **trùng mã và ngày** với một lượt đang chờ/đang chạy được **gộp** vào lượt đó: ai hỏi cũng nhận kết quả, AI chỉ chạy một lần.
- Web + Telegram chỉ được có tối đa `MAX_PENDING` (mặc định 10) lượt chờ; vượt thì báo "Hàng đợi đầy". Lượt tự động hằng ngày không bị giới hạn này.
- Trang web **bắt buộc mật khẩu**; form gửi từ trang web khác bị từ chối (chống CSRF); trang không cho nhúng vào iframe.
- Bot chỉ phục vụ id trong `TELEGRAM_ALLOWED_IDS`.

## 8. Tầng AI dự phòng

Mỗi lượt gọi AI đi theo `PROVIDER_ORDER` (mặc định `router9,groq,openrouter,xai`; nhà cung cấp chưa có key tự bỏ qua):

- Lỗi kết nối, timeout, lỗi máy chủ hoặc bị từ chối yêu cầu → chuyển ngay sang nhà cung cấp kế tiếp.
- Bị 429 (hết quota) → nghỉ theo `Retry-After`, hoặc `LLM_QUOTA_COOLDOWN_SEC` (mặc định 300 s).
- Key bị từ chối → nghỉ 1 giờ. 9Router lỗi → nghỉ `ROUTER9_RETRY_SEC` (mặc định 900 s).
- Khi tất cả đang nghỉ hoặc hết hạn mức, lượt gọi **báo lỗi ngay, không gửi gì** (trước đây vẫn thử lần lượt, chính điều này làm gateway đang quá tải bị dồn thêm). `LLM_TRY_RESTING=1` để quay lại cách cũ.
- Không retry bên trong SDK: một lượt gọi chỉ gửi một yêu cầu tới mỗi nhà cung cấp.
- Tối đa `LLM_MAX_CONCURRENT` (mặc định 2) yêu cầu cùng lúc.
- Hạn mức ngày (giờ VN, giữ qua lần khởi động lại trong `data/llm_usage.json`): tổng `LLM_MAX_CALLS_PER_DAY` (mặc định 300) và riêng từng nhà cung cấp `<TÊN>_MAX_CALLS_PER_DAY` (`ROUTER9_`, `GROQ_`, `OPENROUTER_`, `XAI_`; OpenRouter mặc định 45, dưới mức 50 lượt/ngày của gói free; 0 = không giới hạn). Nhà cung cấp hết hạn mức được bỏ qua tới hôm sau; hết tổng hạn mức thì mọi lượt phân tích báo lỗi tới hôm sau. `/trangthai` và trang web hiện số lượt đã dùng.

- Khoảng nghỉ tối thiểu giữa hai lượt gọi tới cùng một nhà cung cấp: `<TÊN>_MIN_INTERVAL_SEC` (mặc định 0). Các lượt gọi cùng lúc xếp hàng lần lượt, không tới dồn một lúc.

**9Router đứng sau một tài khoản ChatGPT free** (vd chatgpt2api giả lập Codex): một tài khoản free chỉ chịu được ít lượt mỗi giờ, trả lời chậm, và dễ bị đánh dấu khi nhận nhiều yêu cầu dồn dập. Nên đặt:

```
DAILY_AI_MODE=critic             # 1 lượt gọi mỗi mã, chỉ là chat thường (không tool, không JSON mode)
DAILY_AI_MAX=5
LLM_MAX_CONCURRENT=1
ROUTER9_MIN_INTERVAL_SEC=30
ROUTER9_MAX_CALLS_PER_DAY=40     # chỉnh theo mức tài khoản chịu được
ROUTER9_RETRY_SEC=1800           # 9Router lỗi thì nghỉ 30 phút, chuyển sang OpenRouter
LLM_CALL_TIMEOUT_SEC=180
```

`/phantich` (chuỗi agent) cần gọi tool và structured output, mỗi mã khoảng 25 lượt: với tài khoản free nên hạn chế, dùng `/phanbien` thay thế. Bộ giả lập nào không hỗ trợ tool calling thì lượt đó báo lỗi và chuyển sang nhà cung cấp kế tiếp.

Một lượt phân tích đầy đủ tốn khoảng 15-35 lượt gọi AI (4 analyst kèm các vòng gọi tool, Bull/Bear, Research Manager, Trader, 3 góc nhìn rủi ro, Portfolio Manager). Giảm `MAX_TOOL_ROUNDS` nếu cần tiết kiệm thêm.

`QUICK_MODEL` / `DEEP_MODEL` chọn model trên 9Router cho hai tầng (tầng deep dùng cho Research Manager và Portfolio Manager). Các nhà cung cấp khác dùng `GROQ_MODEL`, `OPENROUTER_MODEL`, `XAI_MODEL`.

## 9. Toàn bộ biến môi trường

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `PROVIDER_ORDER` | `router9,groq,openrouter,xai` | Thứ tự dự phòng AI |
| `ROUTER9_BASE_URL`, `ROUTER9_API_KEY`, `ROUTER9_MODEL` | - / - / `gpt-4o-mini` | 9Router |
| `GROQ_API_KEY`, `GROQ_MODEL` | - / `openai/gpt-oss-20b` | Groq |
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | - / model `:free` | OpenRouter |
| `XAI_API_KEY`, `XAI_MODEL` | - / `grok-4-fast` | Grok (tuỳ chọn) |
| `QUICK_MODEL`, `DEEP_MODEL` | `ROUTER9_MODEL` | Model 9Router cho từng tầng |
| `LLM_QUOTA_COOLDOWN_SEC` | 300 | Nghỉ khi 429 không có Retry-After |
| `ROUTER9_RETRY_SEC` | 900 | Nghỉ khi 9Router lỗi |
| `LLM_CALL_TIMEOUT_SEC` | 120 | Timeout mỗi lượt gọi AI |
| `LLM_MAX_CALLS_PER_DAY` | 300 | Tổng lượt gọi AI mỗi ngày (0 = không giới hạn) |
| `ROUTER9_MAX_CALLS_PER_DAY`, `GROQ_…`, `OPENROUTER_…`, `XAI_…` | 0, OpenRouter 45 | Hạn mức ngày từng nhà cung cấp (0 = không giới hạn) |
| `LLM_MAX_CONCURRENT` | 2 | Số lượt gọi AI chạy cùng lúc |
| `ROUTER9_MIN_INTERVAL_SEC`, `GROQ_…`, `OPENROUTER_…`, `XAI_…` | 0 | Số giây tối thiểu giữa hai lượt gọi tới nhà cung cấp đó |
| `LLM_TRY_RESTING` | 0 | 1 = vẫn gọi nhà cung cấp đang nghỉ khi không còn ai khác |
| `TELEGRAM_TOKEN` | - | Token bot (bỏ trống = tắt bot) |
| `TELEGRAM_ALLOWED_IDS` | - | Id được dùng bot |
| `TELEGRAM_REPORT_CHAT_IDS` | = allowed ids | Nơi nhận báo cáo hằng ngày |
| `WEB_PASSWORD` | - | **Bắt buộc** để mở trang web |
| `PUBLIC_URL` | - | Địa chỉ web, để bot gửi link báo cáo |
| `SERVER_PORT` / `PORT` | 8080 | Cổng web |
| `WATCHLIST` | - | Watchlist ban đầu (chỉ dùng lần chạy đầu) |
| `DAILY_REPORT_TIME` | `15:30` | Giờ phân tích watchlist tự động (HH:MM, giờ VN) |
| `DAILY_AI_MODE` | `critic` | Lượt tự động gọi AI thế nào: `critic` (1 lượt/mã), `full` (chuỗi agent, ~25 lượt/mã), `off` |
| `DAILY_AI_MAX` | 5 | Số mã watchlist được gửi AI mỗi ngày (0 = không gọi AI ở lượt tự động) |
| `DAILY_AI_ACTIONS` | `BUY,SELL` | Hành động của Quant Engine đáng để hỏi AI |
| `TRADE_TIME` | `DAILY_REPORT_TIME` nếu ≥ 15:05, ngược lại `15:30` | Giờ bot giao dịch + hiệu chỉnh chạy (HH:MM, giờ VN); giá trị trước 15:05 được nâng lên 15:05 |
| `MAX_PENDING` | 10 | Số lượt chờ tối đa từ web/Telegram |
| `TRADE_MODE` | `paper` | `paper` / `signal` / `off` |
| `PAPER_CAPITAL` | 1.000.000.000 | Vốn tài khoản ảo (VND) |
| `UNIVERSE_VN30` | 1 | 1 = rổ VN30 + watchlist; 0 = chỉ watchlist |
| `AUTO_CALIBRATE` | 1 | Tự hiệu chỉnh ngưỡng Quant Engine và Động lượng mỗi đêm (Phòng thủ không tự chỉnh) |
| `SIGNAL_STRATEGY` | `factor` | Chiến lược cho tín hiệu thật: `factor` (Phòng thủ) / `momentum` / `engine` |
| `DATA_DIR` | `data` | Thư mục dữ liệu (cache, báo cáo, jobs) |
| `MAX_DEBATE_ROUNDS`, `MAX_RISK_ROUNDS` | 1 / 1 | Số vòng tranh luận |
| `MAX_TOOL_ROUNDS` | 8 | Số lượt gọi tool tối đa của mỗi analyst |
| `KEEP_REPORTS` | 60 | Số thư mục báo cáo giữ lại |
| `CACHE_MAX_AGE_DAYS` | 3 | Xoá cache giá cũ hơn số ngày này |

## 10. Giới hạn và xử lý sự cố

- Một lượt phân tích đầy đủ gọi AI khoảng 15-20 lần, mất vài phút. Model miễn phí có thể chậm hoặc bị giới hạn tốc độ.
- Khởi động lại khi đang phân tích thì lượt đó bị đánh dấu lỗi; gửi lại là được.
- Cache giá cũ và báo cáo vượt `KEEP_REPORTS` được tự xoá để không đầy 2 GB.

| Triệu chứng | Cách xử lý |
|---|---|
| Trang web báo "Trang web đang khoá" | Đặt `WEB_PASSWORD`, khởi động lại |
| Bot trả "Bot chưa cho phép tài khoản này" | Thêm id vào `TELEGRAM_ALLOWED_IDS`, khởi động lại |
| `Every LLM provider failed` | Xem `/trangthai`; kiểm tra key, quota, thêm nhà cung cấp dự phòng |
| `/nhanh` báo lỗi kết nối | DNSE/VCI không truy cập được từ host |
| Không có báo cáo hằng ngày | Kiểm tra watchlist, `DAILY_REPORT_TIME`, `TRADE_TIME`, ngày lễ, và log `Daily run` / `Trading run` |
| `/khop` báo sổ tài khoản thật bị hỏng | File `real.json` hỏng đã được chuyển sang `real.json.corrupt-...` (không xoá). Đổi tên lại thành `real.json` sau khi sửa, hoặc xoá file đó để bắt đầu sổ mới |
| Link báo cáo trong Telegram thiếu | Đặt `PUBLIC_URL` |
| `/hieuchinh` báo "chưa chạy" | Hiệu chỉnh chạy sau phiên đầu tiên lúc `TRADE_TIME`; cần đủ 80 phiên lịch sử |
| Bot không đặt lệnh nhiều ngày | Bình thường khi VNINDEX risk-off; xem lý do bằng `/nhanh MÃ` |

> Thông tin chỉ mang tính tham khảo, không phải khuyến nghị đầu tư.
