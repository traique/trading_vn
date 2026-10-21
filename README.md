# TradingAgents VN

Hệ thống phân tích và **bot giao dịch tự hiệu chỉnh** dành riêng cho cổ phiếu Việt Nam (HOSE, HNX, UPCoM).

- **Quant Engine** chốt hành động và mọi mức giá bằng code: cùng dữ liệu thì cùng kết quả.
- **Các agent AI** (analyst, Bull/Bear, Trader, nhóm rủi ro, Portfolio Manager) diễn giải, tranh luận và phản biện quanh quyết định đó. **Output Guard** chặn bằng code những gì AI nói trái với hệ thống.
- **Bot giao dịch** chạy ba chiến lược trên ba tài khoản ảo, tự hiệu chỉnh ngưỡng mỗi đêm và gửi tín hiệu cho tài khoản thật nếu bạn muốn. Bot **không bao giờ tự gửi lệnh tới công ty chứng khoán**.
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

Ba chiến lược chạy song song, mỗi chiến lược một tài khoản ảo 1 tỷ đồng, để so sánh bằng số liệu thật hằng ngày. Tín hiệu cho tài khoản thật mặc định theo **Phòng thủ**.

| | **Phòng thủ** (mặc định) | Quant Engine | Động lượng VN30 (để so sánh) |
|---|---|---|---|
| Chọn mã | 10 mã trong 50 mã vốn hoá lớn nhất (thuộc 100 mã HOSE thanh khoản nhất), xếp hạng theo **biến động 1 năm thấp + gần đỉnh 52 tuần**; dựng lại rổ cuối mỗi tháng, mã đang giữ còn trong top 15 thì giữ | Kế hoạch BUY của engine | Mỗi ~21 phiên, top 5 mã VN30 hiện tại tăng mạnh nhất ~3 tháng |
| Vào / thoát | Mua giá mở cửa phiên sau, 0,98/10 NAV mỗi mã; bán khi rời rổ; **về tiền mặt khi VNINDEX đóng cửa dưới MA200** (xét phiên cuối mỗi tuần) | Stop, chốt một phần tại T1, T2, hết số phiên, engine chuyển SELL | Mã rời rổ; về tiền mặt khi VNINDEX dưới MA100 |
| Vòng quay vốn | 2–4 lần/năm | thấp | 7–9 lần/năm |
| Code | `trading/factor.py` | `quant/`, `trading/simulator.py` | `trading/momentum.py` |

### Kết quả kiểm định độc lập (chạy ngày 15/10/2026, dữ liệu tới 14/10/2026)

Chiến lược Phòng thủ được chọn từ 14 ứng viên của 5 nhóm nghiên cứu, theo quy tắc chọn **chốt trước khi chạy giai đoạn TEST**; TEST chỉ chạy một lần. Mọi con số đã trừ phí mua 0,15%, phí bán 0,15% + thuế 0,1%, trượt giá 0,1% mỗi chiều, khớp giá mở cửa phiên sau, T+2, lô 100, bước giá, biên độ/khoá trần sàn (đúng sổ lệnh của bot); vốn 1 tỷ, tiền mặt không có lãi.

CAGR / sụt giảm tối đa (Sharpe):

| | TRAIN 2013–2020 | VALID 2021–2023 | **TEST 01/2024–10/2026** | Cả kỳ 2013–10/2026 (một lần chạy) |
|---|---|---|---|---|
| **Phòng thủ** | 18,1% / −25,9% (1,37) | 13,9% / −12,0% (0,99) | **8,1% / −20,8% (0,55)** | 15,6% / −25,9% (1,11) |
| Động lượng (luật cũ của bot), rổ VN30 point-in-time* | 4,3% / −43,9% | 2,9% / −46,4% | 11,8% / −33,1% | 4,7% / −45,2% |
| Động lượng, cùng rổ 50 mã vốn hoá | 10,7% / −48,9% | 13,4% / −47,5% | 7,5% / −41,3% | 10,9% / −48,9% |
| VNINDEX mua và giữ (chỉ số giá, không phí) | 12,9% / −45,3% | 0,3% / −40,3% | 15,9% / −18,1% | 10,7% / −45,3% |

\* 30 mã vốn hoá lớn nhất trong 100 mã thanh khoản nhất tại từng thời điểm, xấp xỉ rổ VN30 thật. Con số +73…91% báo trước đây cho luật động lượng được tính trên **rổ VN30 hôm nay** (thiên lệch sống sót: MCH, BSR khi được mua còn ở UPCoM) và chưa đủ chi phí; trên rổ point-in-time luật này chỉ được 0,1–11,8%/năm ở TEST tuỳ rổ.

Đọc kỹ trước khi dùng tiền thật:

- **Lợi thế chính là giảm rủi ro, không phải tăng lợi nhuận.** Sụt giảm tối đa khoảng −26% so với −43…−49% của VNINDEX và luật động lượng; biến động 13–17%/năm. Chênh lệch lợi nhuận ngày so với luật động lượng cùng rổ không có ý nghĩa thống kê.
- **Thua thị trường trong đợt tăng 2024–2026:** TEST 8,1%/năm so với VNINDEX 15,9%; năm 2025 +21,5% so với +40,9%. Ở TEST nó cũng kém luật động lượng trên rổ VN30 point-in-time (8,1% so với 11,8%, chênh lệch không có ý nghĩa thống kê). Nó thắng rõ ở các năm giảm: 2018 −3,3% (VNINDEX −9,3%), 2022 −4,9% (VNINDEX −32,8%).
- **Thiên lệch còn lại:** xếp hạng vốn hoá cần số cổ phiếu lưu hành của VCI; 7 mã đã huỷ niêm yết thiếu dữ liệu này nên không bao giờ vào rổ (thiên lệch lạc quan nhỏ). Dữ liệu gồm 568 mã HOSE/từng ở HOSE, trong đó 162 mã đã rời HOSE (huỷ niêm yết hoặc chuyển sang HNX/UPCoM). VNINDEX là chỉ số giá (không cộng cổ tức) nên khoảng cách thật nhỏ hơn 1,5–2 điểm/năm. Giá điều chỉnh quyền tính tới hôm nay.
- **Độ nhạy:** dời ngày bắt đầu 1–3 tháng, TEST còn 3,4–8,1%/năm; trượt giá gấp đôi −0,6 điểm. Các bộ tham số lân cận (K 5/15, rổ 30/70, buffer 1/2, MA150/250...) cho TEST 4,6–14,9%, trung vị 7,7%: điểm đang dùng không phải điểm may mắn.
- **Bot dùng luật y hệt bản đã kiểm định,** chỉ khác một điểm: khi hai mã bằng điểm, mã ít biến động hơn được xếp trước (bản kiểm định để pandas tự xếp). Phát lại luật của bot trên cùng dữ liệu cho TRAIN 17,7% / −26,4%, VALID 13,9% / −12,0%, TEST 8,4% / −20,2%, cả kỳ 15,3% / −26,4%; lệnh khớp trùng từng lệnh với bộ khung kiểm định (test `tests/test_factor.py` kiểm tra điều này trên dữ liệu giả lập, kể cả qua bot thật).
- **Tiền nhàn rỗi:** chiến lược đứng ngoài 20–45% thời gian. Gửi phần tiền mặt vào tiền gửi/quỹ thị trường tiền tệ ~4%/năm cộng thêm khoảng 0,9–2,1 điểm/năm (cả kỳ 17,1%).
- **Trạng thái ngày 14/10/2026:** VNINDEX 1.704,9 dưới MA200 1.797,9 nên rổ đang **toàn tiền mặt** (từ phiên xét 25/09/2026), đang trong đợt sụt −20,8% bắt đầu 03/02/2026. Bot sẽ đứng ngoài tới khi một phiên cuối tuần đóng cửa trên MA200.

Toàn bộ bảng so sánh, các phép thử độ bền và giới hạn của kết luận: [research/RESULTS.md](research/RESULTS.md); tự chạy lại bằng bộ khung trong [research/](research/README.md).

Quant Engine không phải ứng viên trong đợt kiểm định này. Chạy lại theo luật khớp lệnh mới trên rổ VN30 hiện tại (không có dữ liệu dòng tiền), giai đoạn VALID 2021–2023 nó được −1,6%/năm, sụt giảm tối đa −4,7%, gần như luôn đứng ngoài (trung bình 2% vốn). Con số +2,2% báo trước đây được đo trước khi sửa luật T+2 và trượt giá.

### Mỗi ngày giao dịch, sau giờ đóng cửa (`TRADE_TIME`, không sớm hơn 15:05)

1. **Ba tài khoản ảo khớp phiên hôm nay:** lệnh hôm qua khớp hoặc hết hạn; stop và mục tiêu được kiểm tra trên nến hôm nay.
2. **Ra quyết định và đặt lệnh cho phiên mai:**
   - Phòng thủ xét VNINDEX so với MA200 ở phiên cuối tuần; cuối tháng (hoặc khi thị trường chuyển tốt) dựng lại rổ từ dữ liệu tới hôm nay: danh sách HOSE (VCI), giá trị khớp lệnh và số cổ phiếu lưu hành (VCI price-history), giá ngày. Rổ vốn hoá được cache theo ngày; nếu không dựng được (nguồn lỗi) thì phiên báo lỗi và được thử lại sau, như khi thiếu nến, chứ không xếp hạng trên một rổ khác.
   - Quant Engine xét VN30 + watchlist + các mã đang giữ.
   - Chiến lược động lượng xếp hạng lại VN30 khi đến kỳ.

   Báo cáo 🛡️ / 🤖 / 🚀 / 📊 gửi qua Telegram; NAV được ghi cạnh VNINDEX.
3. **Sàng lọc rồi mới gọi AI (giống bot OKX):** Quant Engine xét VN100 + watchlist (`SCAN_UNIVERSE`, không tốn AI); chỉ `DAILY_AI_MAX` (mặc định 5) mã BUY/SELL mạnh nhất được **AI phản biện, 1 lượt gọi mỗi mã** (đồng ý/phản đối kèm lý do; không đổi hành động hay mức giá). Bot theo dõi kết quả bóng để biết AI phản đối đúng bao nhiêu. Tất cả nằm trong hạn mức AI của ngày (`LLM_MAX_CALLS_PER_DAY`, xem [tầng AI dự phòng](deploy/botkeep/README.md#8-tầng-ai-dự-phòng)).
4. **Tự hiệu chỉnh** Quant Engine và Động lượng, rồi gửi kết quả ⚙️. Phòng thủ không tự hiệu chỉnh.

**Trong phiên** (`INTRADAY_ALERTS=1`, không gọi AI): mỗi 15 phút bot đọc giá khớp gần nhất và báo khi mã đang giữ chạm stop/T1/T2/giá sàn, lệnh mua hôm nay vào vùng mua, hoặc VNINDEX giảm từ 2%. Bot chỉ báo, các tài khoản ảo vẫn khớp theo nến ngày sau 15:05.

Ngày lễ (VNINDEX không có phiên) được bỏ qua.

### Luật giao dịch

Paper và backtest dùng chung luật này, nên kết quả backtest đúng là điều tài khoản ảo sẽ làm.

- **Thứ tự trong phiên:** bán trước, mua sau. Tiền bán dùng được ngay trong phiên.
- **Mua (Quant Engine):** lệnh giới hạn tại cận trên vùng mua, hiệu lực một phiên. Khối lượng theo % NAV của engine, lô 100, tối đa `max_positions` mã.
- **Mua/bán (Phòng thủ, Động lượng):** lệnh giá mở cửa phiên kế tiếp, chia đều vốn cho các mã trong rổ; mã đang giữ không mua thêm. Lệnh bị chặn (khoá trần/sàn, tạm ngừng, chưa về T+2) được đặt lại mỗi phiên.
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
  - Lệnh đề xuất theo chiến lược chọn ở `SIGNAL_STRATEGY`: `factor` (Phòng thủ, mặc định), `momentum` hoặc `engine`.
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
- **Phòng thủ: không tự hiệu chỉnh.** `fb_top_k` 10, `fb_buffer` 1,5, `fb_market_ma` 200, `fb_universe` 50, `fb_rebalance_months` 1 giữ đúng bộ đã kiểm định. Đợt kiểm định chỉ thử điểm này và từng lân cận đơn lẻ (K 5/15, buffer 1/2, MA150/250...; TEST 4,6–14,9%, K=15 chỉ 4,9%), chưa từng thử một phiên bản tự chỉnh; còn dữ liệu hiệu chỉnh hằng đêm chỉ có các mã hôm nay nên không dựng lại được rổ point-in-time (thiên lệch sống sót). Sửa tay trong `quant_params.json` bị kẹp vào vùng đã thử (K 5–15, buffer 1–2, MA 150–250, rổ 30–70, 1–3 tháng).
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
| `/phanbien FPT` / `/phanbien` | AI phản biện quyết định của hệ thống (1 lượt gọi AI) / AI phản đối đúng bao nhiêu |
| `/theodoi FPT,HPG,VCB` / `/theodoi` | Đặt / xem watchlist |
| `/danhmuc` | Danh mục ba tài khoản ảo (và tài khoản thật ở chế độ `signal`) |
| `/lenh` | Lệnh bot đặt cho phiên tới |
| `/hieuchinh` | Ngưỡng đang dùng và kết quả tự hiệu chỉnh gần đây |
| `/chay` | Chạy ngay phiên giao dịch đã đóng gần nhất cho bot (bỏ qua nếu phiên đó đã chạy) |
| `/von 500000000` | Đặt tiền mặt tài khoản thật (chế độ `signal`) |
| `/khop mua FPT 100 56300` | Báo đã khớp lệnh thật; bán: `/khop ban ...` |
| `/trangthai` | Hàng đợi và trạng thái nhà cung cấp AI |
| `/id` | Telegram id của bạn |

**Zoom Team Chat** (tuỳ chọn, giống bot OKX): mọi thông báo gửi cả vào Zoom (Incoming Webhook hoặc chatbot; dùng chung được app chatbot của bot OKX để nhận thông báo); gõ lệnh trong Zoom cần một app chatbot riêng. Cách cài: [deploy/botkeep/README.md](deploy/botkeep/README.md#zoom-team-chat).

### Trang web

- **Trang chính:** nhập mã, chọn phân tích đầy đủ hoặc chỉ quyết định hệ thống, xem hàng đợi, sửa watchlist, xem trạng thái nhà cung cấp AI.
- **`/bot`:** biểu đồ NAV ba chiến lược so với VNINDEX, danh mục, lệnh và giao dịch của ba tài khoản ảo, rổ Phòng thủ hiện tại; ngưỡng đang dùng; lịch sử tự hiệu chỉnh. Nút **Chạy phiên ngay** làm như `/chay`.
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

Backtest các chiến lược của bot trên dữ liệu lịch sử:

```python
from tradingagents.dataflows import money_flows as mf
from tradingagents.quant.params import DEFAULT
from tradingagents.trading import factor, momentum, simulator as sim

data = sim.build_dataset(mf.index_members("VN30"), 460, "2026-10-13")
print(sim.score(sim.run(data, DEFAULT)).line())        # Quant Engine
print(sim.score(momentum.run(data, DEFAULT)).line())   # Động lượng
print(sim.score(factor.run(data, DEFAULT)).line())     # Phòng thủ trên rổ cố định (không point-in-time)
```

Kết quả kiểm định ở mục [Bot giao dịch](#bot-giao-dịch) dùng rổ vốn hoá point-in-time từ 2013, do bộ khung nghiên cứu riêng chạy (không nằm trong repo); `factor.run` trên `build_dataset` dùng rổ cố định nên chỉ để thử nhanh.

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
  trading/        ledger (sổ lệnh luật VN), simulator, factor (Phòng thủ), momentum, calibrate
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
research/         bộ khung backtest nghiên cứu (không nằm trong gói Botkeep): research/README.md
```

## Phát triển và kiểm thử

```bash
pip install -e ".[dev]"
pytest -q
ruff check .
```

Test không gọi mạng hay AI: dữ liệu và model đều được giả lập. CI chạy trên Python 3.11–3.14. Lịch sử thay đổi xem ở [CHANGELOG.md](CHANGELOG.md).

**Bộ khung backtest nghiên cứu** (`research/`, nằm ngoài gói Botkeep): tải dữ liệu lịch sử 2011–nay của mọi mã từng niêm yết HOSE, chạy lại hoặc mở rộng nghiên cứu trên đúng sổ lệnh của bot, với các giai đoạn TRAIN/VALID/TEST cố định và TEST bị khoá. Hướng dẫn: [research/README.md](research/README.md). Kết quả kiểm định ngày 15/10/2026 (vì sao chọn Phòng thủ): [research/RESULTS.md](research/RESULTS.md).

```bash
python -m research download                 # lần đầu ~20 phút, ~320 MB (không commit)
python -m research run factor --split VALID # CAGR 13,86% / sụt giảm tối đa −12,0%
```

## Giới hạn cần biết

- **Nguồn dữ liệu không chính thức:** các endpoint DNSE/VCI/CafeF/VNDirect là API công khai mà website của họ dùng, không phải API có cam kết. Chúng có thể đổi hoặc chặn IP nước ngoài.
- **Backtest có thể đẹp hơn thực tế:**
  - giá đã điều chỉnh quyền tính đến hôm nay;
  - Phòng thủ: rổ point-in-time, nhưng 7 mã đã huỷ niêm yết thiếu số cổ phiếu lưu hành nên không vào được rổ; khớp lệnh giả định đúng giá mở cửa cộng trượt giá;
  - Quant Engine và Động lượng: rổ VN30 lấy theo danh sách hiện tại, nên bỏ sót những mã từng bị loại (thiên lệch sống sót);
  - không phát lại tin tức.
- **Rủi ro của chiến lược Phòng thủ:** sụt giảm tối đa lịch sử −26%, thua VNINDEX trong các đợt tăng mạnh và khi thị trường hồi từ đáy (bộ lọc MA200 vào lại muộn); giai đoạn TEST 2,8 năm quá ngắn để chứng minh lợi thế lợi nhuận.
- **Rủi ro của chiến lược động lượng:** trên rổ point-in-time, sụt giảm tối đa 33–45% trong giai đoạn TEST 2024–2026, tới 50% ở các giai đoạn khác.
- **Tự hiệu chỉnh có giới hạn:** chỉ chỉnh ngưỡng của Quant Engine và Động lượng, không tạo ra lợi thế mới; Phòng thủ giữ bộ đã kiểm định.

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
