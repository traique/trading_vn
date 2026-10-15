# TradingAgents VN

Hệ thống phân tích và **bot giao dịch tự hiệu chỉnh** dành riêng cho cổ phiếu Việt Nam (HOSE, HNX, UPCoM).

- **Quant Engine** chốt hành động và mọi mức giá bằng code: cùng dữ liệu thì cùng kết quả.
- **Các agent AI** (analyst, Bull/Bear, Trader, nhóm rủi ro, Portfolio Manager) diễn giải, tranh luận và phản biện quanh quyết định đó. **Output Guard** chặn bằng code những gì AI nói trái với hệ thống.
- **Bot giao dịch** chạy hai chiến lược trên hai tài khoản ảo, tự hiệu chỉnh ngưỡng mỗi đêm và gửi tín hiệu cho tài khoản thật nếu bạn muốn. Bot **không bao giờ tự gửi lệnh tới công ty chứng khoán**.
- **Một tiến trình duy nhất** gồm trang web, bot Telegram và báo cáo tự động. Chạy được trên gói free của Botkeep (2 GB RAM).

Dựa trên [TradingAgents](https://github.com/TauricResearch/TradingAgents) v0.6.0 của Tauric Research (Apache 2.0), đã chuyển hẳn sang thị trường Việt Nam.

> Chỉ dùng cho nghiên cứu và tham khảo, **không phải khuyến nghị đầu tư**. Kết quả quá khứ không bảo đảm tương lai.

---

## Mục lục

- [Bắt đầu nhanh](#bắt-đầu-nhanh)
- [Cách hệ thống ra quyết định](#cách-hệ-thống-ra-quyết-định)
- [Bot giao dịch](#bot-giao-dịch)
- [Dữ liệu](#dữ-liệu)
- [Sử dụng](#sử-dụng)
- [Chạy trên máy cá nhân, Docker, thư viện Python](#chạy-trên-máy-cá-nhân-docker-thư-viện-python)
- [Bộ nhớ, checkpoint và backtest](#bộ-nhớ-checkpoint-và-backtest)
- [Cấu trúc mã nguồn](#cấu-trúc-mã-nguồn)
- [Phát triển và kiểm thử](#phát-triển-và-kiểm-thử)
- [Giới hạn cần biết](#giới-hạn-cần-biết)
- [Ghi công](#ghi-công)

---

## Bắt đầu nhanh

### Trên Botkeep (khuyến nghị)

```bash
git clone https://github.com/traique/trading_vn.git
cd trading_vn
python scripts/build_botkeep_bundle.py      # → dist/tradingagents-vn-botkeep.zip (~110 file, ~280 KB)
```

1. Trên Botkeep, tạo workload **Python 3.11+**, upload rồi giải nén zip vào thư mục gốc.
2. Đặt file khởi động là `main_botkeep.py`, file requirements là `requirements.txt`.
3. Vào tab **Environment** và điền tối thiểu:
   - **Một nhà cung cấp AI:** `ROUTER9_BASE_URL` + `ROUTER9_API_KEY`, hoặc `GROQ_API_KEY` / `OPENROUTER_API_KEY`.
   - **Web:** `WEB_PASSWORD` (**bắt buộc**, thiếu thì trang web bị khoá) và `PUBLIC_URL`.
   - **Telegram:** `TELEGRAM_TOKEN`. Khởi động bot, gửi `/id` cho bot, điền id vào `TELEGRAM_ALLOWED_IDS`, rồi Restart.
4. Restart rồi kiểm tra log:
   - log có `AI provider chain: ...` và `Trading bot: mode paper, calibration on`;
   - mở web, thử **Chỉ quyết định hệ thống** với `FPT`.

Hướng dẫn đầy đủ (từng bước, toàn bộ biến môi trường, xử lý sự cố): **[deploy/botkeep/README.md](deploy/botkeep/README.md)**. File này cũng có trong gói upload.

### Trên VPS / máy bất kỳ

```bash
pip install ".[bot]"
cp deploy/botkeep/.env.example .env        # điền như trên
python main_botkeep.py                     # web ở http://localhost:8080
```

Hoặc dùng Docker: `docker compose --profile bot up -d bot`.

---

## Cách hệ thống ra quyết định

```
Mã + ngày
   ├─► Analyst (song song): Market · Sentiment · News · Fundamentals
   └─► Quant Engine (code): giá, VNINDEX, ngành, tin, dòng tiền
          → BUY / HOLD / WATCH / SELL / NO_TRADE
          + vùng mua, cắt lỗ, mục tiêu, R:R, tỷ trọng
                 │
                 ▼
Bull ⇄ Bear → Research Manager ─[Guard]→ Trader ─[Guard]→ Risk (3 góc nhìn) → Portfolio Manager
                 │
                 ▼
Output Guard (code) → báo cáo Markdown + HTML
```

### Quant Engine

`tradingagents/quant/` là một pipeline xác định, không gọi AI:

1. **Regime VNINDEX.** Xu hướng đa khung thời gian, ADX và số ngày phân phối xếp thị trường vào `risk_on` / `neutral` / `risk_off`. Hai điều kiện có thể hạ regime xuống một bậc mỗi điều kiện:
   - khối ngoại bán ròng toàn thị trường kéo dài;
   - phân kỳ độ rộng: VNINDEX lập đỉnh 20 phiên trong khi số mã tăng yếu đi.

   Khi `risk_off` hoặc thiếu dữ liệu VNINDEX thì **không mở mua mới**.
2. **Setup của mã.** MA, RSI, MACD, ADX, hỗ trợ/kháng cự theo swing, thanh khoản, sức mạnh tương đối so với VNINDEX (ghép theo ngày giao dịch), ngành.
3. **Dòng tiền.** Độ tin cậy được nâng hoặc hạ theo các tín hiệu sau:
   - khối ngoại mua ròng liên tiếp, hoặc bán ròng 7/10 phiên;
   - tự doanh cùng chiều với khối ngoại;
   - dòng tiền ETF với mã thuộc VN30;
   - room khối ngoại gần hết thì bỏ qua tín hiệu mua của khối ngoại.
4. **Chất lượng dữ liệu.** Phát hiện chuỗi giá chưa điều chỉnh quyền (theo biên độ của từng sàn), dữ liệu cũ hoặc thiếu.
5. **Kế hoạch giao dịch.** Vùng mua, cắt lỗ, T1/T2 (R:R tối thiểu 1,5), tỷ trọng theo rủi ro 1% NAV, làm tròn theo bước giá và lô 100.

Mọi ngưỡng nằm trong `tradingagents/quant/params.py`. Giá trị mặc định là bộ ngưỡng gốc; bot tự hiệu chỉnh dần (xem [Bot giao dịch](#bot-giao-dịch)).

### Output Guard

Code kiểm tra văn bản ngay sau Research Manager và sau Trader, **trước khi** các agent sau đọc (nên nhóm rủi ro và Portfolio Manager không bị dẫn dắt bởi một khuyến nghị sai), rồi kiểm tra lần cuối cả ba văn bản:

- **Nói ngược hành động của hệ thống** (ví dụ "MUA", "giải ngân", "tăng tỷ trọng" khi hệ thống `NO_TRADE`): đoạn đó bị thay bằng bản tóm tắt của hệ thống. Những câu sau **không** bị tính là mâu thuẫn: câu điều kiện ("nếu thủng stop thì BÁN"), luật giao dịch ("T+2 mới được BÁN"), và mô tả thị trường ("khối ngoại bán ròng", "lực BÁN").
- **Nêu giá không có trong dữ liệu:** gắn cảnh báo. Điểm chỉ số, khối lượng, EPS và giá trị tính bằng tỷ không bị coi là giá cổ phiếu.
- **`final_rating` luôn là hành động của Quant Engine.** Khi hệ thống không đưa kế hoạch (WATCH/NO_TRADE), báo cáo cũng không có stop, mục tiêu hay R:R.

### Quy tắc thị trường Việt Nam

`tradingagents/market/vn_rules.py` gồm:
- **Biên độ:** HOSE ±7%, HNX ±10%, UPCoM ±15%. Cổ phiếu giá rất thấp vẫn có biên độ tối thiểu ±1 bước giá.
- **Bước giá:** HOSE 10/50/100đ, HNX/UPCoM 100đ.
- **Lô 100, T+2.**
- **Phiên ATO/ATC.**
- **Phí và thuế:** phí mua/bán và thuế bán 0,1%.

---

## Bot giao dịch

Hai chiến lược chạy song song, mỗi chiến lược một tài khoản ảo 1 tỷ đồng, để so sánh bằng số liệu thật hằng ngày:

| | Quant Engine | Động lượng VN30 |
|---|---|---|
| Chọn mã | Kế hoạch BUY của engine | Mỗi ~21 phiên, giữ top 5 mã VN30 tăng mạnh nhất ~3 tháng, chia đều vốn |
| Thoát | Stop, chốt một phần tại T1, T2, hết số phiên, engine chuyển SELL | Mã rời rổ; **về tiền mặt toàn bộ khi VNINDEX dưới MA100** |
| Backtest 01/2022 – 10/2026 | +2,2%, sụt giảm tối đa 4% | **+91%, sụt giảm tối đa 21%** |
| Giai đoạn giảm (2022 – 06/2024, VNINDEX −17,6%) | −1,4% | +53% |
| Giai đoạn tăng (07/2024 – 10/2026, VNINDEX +37%) | +3,7% | +25% |

VNINDEX cả kỳ +13,8%. Cả hai backtest dùng đúng luật giao dịch bên dưới.

### Mỗi ngày giao dịch, sau giờ đóng cửa (`DAILY_REPORT_TIME`, mặc định 15:30)

1. **Hai tài khoản ảo khớp phiên hôm nay:** lệnh hôm qua khớp hoặc hết hạn; stop và mục tiêu được kiểm tra trên nến hôm nay.
2. **Ra quyết định và đặt lệnh cho phiên mai:**
   - Quant Engine xét VN30 + watchlist + các mã đang giữ.
   - Chiến lược động lượng xếp hạng lại VN30 khi đến kỳ.

   Báo cáo 🤖 / 🚀 / 📊 gửi qua Telegram; NAV được ghi cạnh VNINDEX.
3. **Phân tích AI đầy đủ** cho từng mã trong watchlist.
4. **Tự hiệu chỉnh** cả hai chiến lược, rồi gửi kết quả ⚙️.

Ngày lễ (VNINDEX không có phiên) được bỏ qua.

### Luật giao dịch

Paper và backtest dùng chung luật này, nên kết quả backtest đúng là điều tài khoản ảo sẽ làm.

- **Thứ tự trong phiên:** bán trước, mua sau. Tiền bán dùng được ngay trong phiên.
- **Mua (Quant Engine):** lệnh giới hạn tại cận trên vùng mua, hiệu lực một phiên. Khối lượng theo % NAV của engine, lô 100, tối đa `max_positions` mã.
- **Mua/bán (Động lượng):** lệnh giá mở cửa phiên kế tiếp, chia đều vốn cho các mã trong rổ.
- **Bán:** từ T+2. Thứ tự ưu tiên:
  1. hệ thống chuyển SELL (bán giá mở cửa);
  2. chạm stop;
  3. bán một phần tại T1 rồi dời stop về hoà vốn;
  4. phần còn lại tại T2;
  5. hết số phiên giữ.

  Nếu một phiên chạm cả stop lẫn mục tiêu, giả định stop chạm trước.
- **Phiên trắng bên:** khoá trần thì không mua được, khoá sàn thì không bán được.
- **Phí:** phí mua/bán và thuế bán được trừ trên mọi lệnh khớp.

### Hai chế độ (`TRADE_MODE`)

- **`paper`** (mặc định): chỉ dùng tài khoản ảo. Nên chạy vài tháng trước khi dùng tiền thật.
- **`signal`**: như paper, cộng thêm lệnh đề xuất cho **tài khoản thật**, tính theo vốn bạn khai bằng `/von`.
  - Lệnh đề xuất theo chiến lược chọn ở `SIGNAL_STRATEGY`: `momentum` (mặc định) hoặc `engine`.
  - Bot cảnh báo khi mã đang giữ chạm stop/T1 hoặc rời rổ.
  - Bạn tự đặt lệnh trên app CTCK rồi báo lại bằng `/khop mua FPT 100 56300`.

### Tự hiệu chỉnh hằng đêm (champion / challenger)

Code nằm ở `tradingagents/trading/calibrate.py`.

1. **Chia dữ liệu:** lấy 460 phiên gần nhất; 400 phiên để huấn luyện, 60 phiên mới nhất để kiểm tra.
2. **Ứng viên:** bộ ngưỡng hiện tại với **một** ngưỡng dịch **một** bước, bộ ngưỡng trước đó (để quay lại), và bộ mặc định.
3. **Điều kiện chọn:** một ứng viên chỉ được chọn khi đạt cả bốn điều kiện:
   - thắng rõ trên phần huấn luyện, với ít nhất 6 lệnh mua;
   - **không thua trên 60 phiên kiểm tra** (phần nó không được chọn dựa vào). Nếu có từ 2 lệnh trở lên ở phần này thì phải thắng hẳn; thị trường im ắng thì chỉ cần không kém hơn;
   - sụt giảm tối đa không xấu hơn quá 2 điểm so với bộ hiện tại;
   - sụt giảm tối đa **không vượt 25%**.
4. **Điểm số:** lợi nhuận % − ½ sụt giảm tối đa %.

Ngưỡng được hiệu chỉnh:
- **Quant Engine:** `confidence_buy_min`, `min_rr_ratio`, ngày phân phối, trọng số/ngưỡng dòng tiền, cách thoát lệnh, tỷ trọng, số mã tối đa.
- **Động lượng:** `mom_top_k`, `mom_lookback`, `mom_market_ma`, `mom_rebalance`, `mom_stop_pct`.

Bộ đang dùng nằm trong `quant_params.json`, lịch sử trong `calibration_history.jsonl`. Xem bằng `/hieuchinh` hoặc trang `/bot`.

---

## Dữ liệu

Tất cả công khai, **không cần key**, gọi thẳng API/HTML (không dùng trình duyệt):

| Loại | Nguồn |
|---|---|
| Giá ngày (đã điều chỉnh quyền), chỉ số | DNSE, dự phòng VCI |
| BCTC kèm ngày công bố, chỉ số TTM, sự kiện doanh nghiệp, công bố thông tin | VCI |
| Tin theo mã | CafeF (tin gắn mã), VCI (công bố), Google News tiếng Việt |
| Báo cáo phân tích của CTCK | CafeF |
| Khối ngoại mua/bán ròng, room, tự doanh từng mã; chứng chỉ quỹ ETF | VCI (lịch sử nhiều năm) |
| Khối ngoại và tự doanh toàn thị trường | CafeF |
| Độ rộng HOSE (mã tăng/giảm, trần/sàn, % trên MA50) | VNDirect |
| Thành phần rổ VN30 | VCI |

**Point-in-time:** một lượt chạy ngày D chỉ thấy các phiên đã đóng đến D, BCTC đã công bố đến D, và tin trước D. Dữ liệu dòng tiền được cache theo ngày; lịch sử dài được tải dần trong đêm. Nguồn nào lỗi thì coi như không có tín hiệu từ nguồn đó, quyết định vẫn được đưa ra.

Mã nhận các dạng `FPT`, `fpt`, `FPT.VN`, `HOSE:FPT`. Chỉ số (`VNINDEX`, `VN30`...) chỉ dùng làm bối cảnh, không có quyết định giao dịch.

---

## Sử dụng

### Telegram

| Lệnh | Việc |
|---|---|
| `/phantich FPT [YYYY-MM-DD]` | Phân tích đầy đủ bằng các agent (vài phút): quyết định hệ thống tới trước, báo cáo + file HTML tới sau |
| `/nhanh FPT` | Chỉ quyết định của Quant Engine (vài giây, không tốn quota AI) |
| `/theodoi FPT,HPG,VCB` / `/theodoi` | Đặt / xem watchlist |
| `/danhmuc` | Danh mục hai tài khoản ảo (và tài khoản thật ở chế độ `signal`) |
| `/lenh` | Lệnh bot đặt cho phiên tới |
| `/hieuchinh` | Ngưỡng đang dùng và kết quả tự hiệu chỉnh gần đây |
| `/chay` | Chạy ngay phiên giao dịch đã đóng gần nhất cho bot (bỏ qua nếu phiên đó đã chạy) |
| `/von 500000000` | Đặt tiền mặt tài khoản thật (chế độ `signal`) |
| `/khop mua FPT 100 56300` | Báo đã khớp lệnh thật; bán: `/khop ban ...` |
| `/trangthai` | Hàng đợi và trạng thái nhà cung cấp AI |
| `/id` | Telegram id của bạn |

### Trang web

- **Trang chính:** nhập mã, chọn phân tích đầy đủ hoặc chỉ quyết định hệ thống, xem hàng đợi, sửa watchlist, xem trạng thái nhà cung cấp AI.
- **`/bot`:** biểu đồ NAV hai chiến lược so với VNINDEX, danh mục, lệnh và giao dịch của hai tài khoản ảo; ngưỡng đang dùng; lịch sử tự hiệu chỉnh. Nút **Chạy phiên ngay** làm như `/chay`.
- **Giao diện sáng / tối:** theo cài đặt của thiết bị, đổi bằng nút mặt trời/mặt trăng trên thanh đầu trang (trình duyệt nhớ lựa chọn). Tông màu trà đào - cam - sả, dùng tốt trên điện thoại.
- **`/jobs/<id>`, `/reports/<id>`:** tiến trình và báo cáo HTML của từng lượt phân tích.

### Bảo mật và quota

- Trang web bắt buộc mật khẩu (`WEB_PASSWORD`), chặn form gửi từ trang khác, không cho nhúng iframe.
- Bot Telegram chỉ phục vụ id trong `TELEGRAM_ALLOWED_IDS`.
- Yêu cầu trùng mã + ngày được gộp; tối đa `MAX_PENDING` lượt chờ.
- Chuỗi AI dự phòng: 9Router → Groq → OpenRouter → Grok. Nhà cung cấp lỗi được bỏ qua; bị 429 thì cho nghỉ.

---

## Chạy trên máy cá nhân, Docker, thư viện Python

### Cài đặt

```bash
git clone https://github.com/traique/trading_vn.git
cd trading_vn
python -m venv .venv && source .venv/bin/activate     # Python 3.11+
pip install ".[bot]"                                  # bỏ [bot] nếu chỉ dùng CLI/thư viện
cp .env.example .env                                  # key AI cho CLI
```

**Docker:**
- CLI: `docker compose run --rm tradingagents`
- Bot luôn bật: `docker compose --profile bot up -d bot`, dùng `.env` theo mẫu `deploy/botkeep/.env.example`.

### Nhà cung cấp AI

**Key cho CLI và thư viện:** OpenAI, Anthropic, Google, xAI, DeepSeek, Qwen, GLM, MiniMax, OpenRouter, Mistral, Kimi, Groq, NVIDIA.

**Các nhà cung cấp khác:**
- Azure OpenAI: dùng `.env.enterprise.example`.
- AWS Bedrock: `pip install ".[bedrock]"`.
- Ollama.
- Mọi endpoint tương thích OpenAI: `llm_provider: "openai_compatible"`.
- Chuỗi dự phòng của bot: `llm_provider: "router9_chain"`.

### CLI

```bash
tradingagents                                     # giao diện hỏi từng bước
tradingagents --ticker FPT --date 2026-10-13 --analysts market,news,fundamentals --save --no-show
```

### Thư viện Python

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.quant import engine

print(engine.analyze("FPT", "2026-10-13").deterministic_summary)   # chỉ Quant Engine, không gọi AI

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"
ta = TradingAgentsGraph(config=config)
state, action = ta.propagate("FPT", "2026-10-13")                # BUY / HOLD / WATCH / SELL / NO_TRADE
ta.save_reports(state, "FPT")
```

Backtest hai chiến lược của bot trên dữ liệu lịch sử:

```python
from tradingagents.dataflows import money_flows as mf
from tradingagents.quant.params import DEFAULT
from tradingagents.trading import momentum, simulator as sim

data = sim.build_dataset(mf.index_members("VN30"), 460, "2026-10-13")
print(sim.score(sim.run(data, DEFAULT)).line())        # Quant Engine
print(sim.score(momentum.run(data, DEFAULT)).line())   # Động lượng
```

**Danh mục đang nắm giữ:** truyền `PortfolioContext` vào `propagate(..., portfolio=...)` hoặc dùng `tradingagents --portfolio book.json`. Khi đó tín hiệu xấu với mã đang giữ sẽ thành `SELL`/`HOLD`, và có tiền mặt thì khối lượng được tính theo lô 100.

---

## Bộ nhớ, checkpoint và backtest

- **Memory log:** mỗi lượt phân tích AI ghi quyết định lại. Khi hết thời gian nắm giữ, quyết định được chấm theo alpha so với VNINDEX, và Portfolio Manager ở các lượt sau đọc lại bài học.
- **Checkpoint** (`--checkpoint`): chạy tiếp từ bước cuối khi bị ngắt. Bản Botkeep tắt tính năng này để tiết kiệm ổ đĩa.
- **Backtest bằng agent AI** trên lưới mã × ngày: `tradingagents backtest FPT,HPG --start 2026-06-01 --end 2026-08-01 --every 7`.

---

## Cấu trúc mã nguồn

```
tradingagents/
  quant/          Quant Engine: features, policy, params, flow_features, sector, validation, report_format
  trading/        ledger (sổ lệnh luật VN), simulator, momentum, calibrate
  agents/         analyst, researcher, trader, risk, manager, output_guard
  dataflows/      DNSE, VCI, CafeF, VNDirect, Google News; cache giá và dòng tiền point-in-time
  market/         vn_rules: biên độ, bước giá, lô, T+2, phiên, phí
  graph/          LangGraph: đồ thị, analyst song song, checkpoint
  llm_clients/    nhà cung cấp AI, chuỗi dự phòng
  memory/         memory log, chấm điểm quyết định
vnbot/            ứng dụng luôn bật: web, Telegram, bot giao dịch, hàng đợi, lịch hằng ngày
cli/              CLI tương tác
main_botkeep.py   điểm khởi động ứng dụng luôn bật
deploy/botkeep/   requirements, .env.example, hướng dẫn Botkeep
scripts/          build_botkeep_bundle.py (gói upload Botkeep)
tests/            ~1200 test, không cần mạng hay key
```

## Phát triển và kiểm thử

```bash
pip install -e ".[dev]"
pytest -q
ruff check .
```

Test không gọi mạng hay AI: dữ liệu và model đều được giả lập. CI chạy trên Python 3.11–3.14. Lịch sử thay đổi xem ở [CHANGELOG.md](CHANGELOG.md).

## Giới hạn cần biết

- **Nguồn dữ liệu không chính thức:** các endpoint DNSE/VCI/CafeF/VNDirect là API công khai mà website của họ dùng, không phải API có cam kết. Chúng có thể đổi hoặc chặn IP nước ngoài.
- **Backtest có thể đẹp hơn thực tế:**
  - giá đã điều chỉnh quyền tính đến hôm nay;
  - rổ VN30 lấy theo danh sách hiện tại, nên bỏ sót những mã từng bị loại (thiên lệch sống sót);
  - không phát lại tin tức.
- **Rủi ro của chiến lược động lượng:** có thể sụt giảm 20–25%, và thua VNINDEX khi thị trường tăng mạnh.
- **Tự hiệu chỉnh có giới hạn:** chỉ chỉnh ngưỡng của chiến lược có sẵn, không tạo ra lợi thế mới.

## Ghi công

Dựa trên **TradingAgents** của Tauric Research ([mã nguồn gốc](https://github.com/TauricResearch/TradingAgents)), giấy phép Apache 2.0 (xem [LICENSE](LICENSE)). Quant Engine và Output Guard được port từ lớp policy của dự án lananh-main.

```
@misc{xiao2025tradingagentsmultiagentsllmfinancial,
      title={TradingAgents: Multi-Agents LLM Financial Trading Framework},
      author={Yijia Xiao and Edward Sun and Di Luo and Wei Wang},
      year={2025},
      eprint={2412.20138},
      archivePrefix={arXiv},
      primaryClass={q-fin.TR},
      url={https://arxiv.org/abs/2412.20138},
}
```
