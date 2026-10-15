# Bộ khung backtest nghiên cứu (`research/`)

Đây là bộ khung đã dùng cho đợt kiểm định độc lập ngày 15/10/2026 (đợt chọn chiến lược **Phòng thủ** làm mặc định cho bot), đưa vào repo để chạy lại và mở rộng nghiên cứu sau này. Kết quả của đợt đó nằm trong [RESULTS.md](RESULTS.md).

- **Point-in-time:** mỗi phiên D, chiến lược chỉ thấy dữ liệu tới hết phiên D. Rổ cổ phiếu được dựng từ dữ liệu trước ngày giao dịch, gồm cả các mã đã huỷ niêm yết hoặc đã rời HOSE.
- **Đúng sổ lệnh của bot:** mọi lệnh đi qua `tradingagents.trading.ledger.Ledger` thật. Khớp ở giá mở cửa phiên sau, trượt giá 0,1% mỗi chiều làm tròn theo bước giá, lô 100, phí mua 0,15%, phí bán 0,15% + thuế 0,1%, T+2, không mua phiên trần khoá, không bán phiên sàn khoá, phiên tạm ngừng không khớp.
- **Giai đoạn cố định, TEST bị khoá:** xem [phần dưới](#giai-đoạn-và-khoá-test).

Gói `research/` nằm ngoài `tradingagents/`, nên `scripts/build_botkeep_bundle.py` không đưa nó vào gói upload Botkeep. Bot không cần nó để chạy.

## Tải dữ liệu

```bash
pip install -e ".[dev]"
python -m research download              # lần đầu ~20 phút; các lần sau chỉ tải phần mới
```

Mọi lệnh `python -m research ...` chạy từ thư mục gốc của repo (gói `research` không được cài vào site-packages).

- **Dung lượng:** ~315 MB CSV cho ~550 mã (`ohlcv/` ~80 MB, `ph/` ~235 MB). Lần chạy backtest đầu tiên dựng thêm cache `research_panel.pkl` (~200 MB, mất ~1 phút).
- **Vị trí:** mặc định `research/data/`. Đặt biến môi trường `TRADING_VN_RESEARCH_DATA=/đường/dẫn` để dùng thư mục khác, ví dụ một ổ đĩa lớn hơn hoặc một bộ dữ liệu đã tải sẵn. Thư mục dữ liệu và `research/out/` đã nằm trong `.gitignore`: **không bao giờ commit dữ liệu**.
- **Tải tiếp và cập nhật:** chạy lại cùng lệnh.
  - Tệp đã tải xong được giữ lại; mỗi mã chỉ tải các phiên sau ngày cuối của tệp.
  - Giá điều chỉnh có thể đổi cả lịch sử khi có cổ tức hoặc cổ phiếu thưởng. Mỗi lần cập nhật, bộ tải lấy lại 20 ngày gối đầu. Nếu giá đóng cửa trong khoảng đó lệch quá 0,5%, nó tải lại toàn bộ lịch sử của mã đó.
  - Mã đã huỷ niêm yết mà đã có tệp thì bỏ qua.
  - Mỗi tệp được ghi vào tệp tạm rồi đổi tên, nên tiến trình bị tắt giữa chừng không để lại tệp ghi dở. Tệp rỗng hoặc hỏng (do phiên bản cũ của bộ tải để lại) được tải lại.
  - **Ctrl-C:** huỷ các mã còn trong hàng đợi, chờ các mã đang tải xong, ghi `download_meta.json` rồi dừng. Chạy lại lệnh để tải tiếp.
  - **Ngày cuối của dữ liệu** (`data_end` trong `download_meta.json`, lịch phiên của panel) chỉ tiến lên khi một lần tải **đầy đủ** chạy xong. Lần chạy với `--symbols`/`--limit` hoặc bị ngắt giữa chừng giữ nguyên `data_end`: nếu không, các mã không được cập nhật sẽ thiếu nến ở các phiên mới và trông như đang tạm ngừng giao dịch. Mã lỗi trong một lần tải đầy đủ cũng dừng ở ngày cũ cho tới khi chạy lại thành công.
- **Lịch sự với nguồn:** mặc định 4 luồng (tối đa 8, `--workers`). Mỗi yêu cầu thử lại 3 lần, chờ tăng dần. Ảnh chụp tháng của VNDirect chạy tối đa 2 luồng.
- **Lỗi:** ghi vào `download.log` trong thư mục dữ liệu và vào trường `"failed"` của `download_meta.json`. Chạy lại lệnh để thử lại. Mã lỗi không để lại tệp nào; mã lỗi chưa từng tải được (ví dụ gõ sai trong `--symbols`) bị xoá khỏi `"failed"` ở lần chạy sau nếu lần đó không thử lại nó.
- **Tuỳ chọn khác:**
  - `--refresh`: tải lại toàn bộ từ 2011;
  - `--symbols FPT,VNM`: chỉ tải hoặc cập nhật vài mã. Bỏ qua ảnh chụp tháng và bước dò mã huỷ niêm yết (chỉ dùng để dựng danh sách mã đầy đủ), và không dời `data_end`. Trên thư mục trống, không có `hose_monthly.csv` thì sàn của mã được suy từ biên độ giá;
  - `--limit N`: chỉ N mã đầu, để thử.

**Nội dung tải về**, tất cả qua các module nguồn có sẵn của repo (`tradingagents.dataflows.vendors`):

| tệp | nguồn | nội dung |
|---|---|---|
| `ohlcv/{MÃ}.csv`, `ohlcv/VNINDEX.csv`, `ohlcv/VN30.csv` | `vci.fetch_ohlcv` (DNSE dự phòng) | nến ngày đã điều chỉnh, từ 2011 |
| `ph/{MÃ}.csv` | VCI `company/{MÃ}/price-history` (endpoint của `flows.fetch_stock_flows`) | giá chưa điều chỉnh, tham chiếu/trần/sàn, khối lượng và giá trị khớp lệnh, **số cổ phiếu lưu hành** (để tính vốn hoá cho Phòng thủ) |
| `hose_monthly.csv` | `flows.fetch_hose_day` (VNDirect) | mọi mã HOSE ở phiên đầu mỗi tháng từ 01/2013: xác định mã nào đang ở HOSE |
| `listing.json`, `vn30_today.json`, `delisted_probe.json` | `vci.fetch_listing`, `money_flows.index_members` | danh sách mã hiện tại (cả mã DELISTED), rổ VN30 hôm nay |

Một số mã đã huỷ niêm yết không còn nến trên VCI. Khi đó nến được dựng lại từ price-history của VCI; nếu cả hai đều không có thì lấy từ VNDirect (từ 2013).

Bộ dữ liệu dùng cho đợt kiểm định (tải ngày 15/10/2026, tới phiên 14/10/2026) có 568 mã, gồm 162 mã đã rời HOSE (FLC, ROS, HAI, OGC, ITA, PVF, KSA…). Tải mới bằng code hiện tại được khoảng 550 mã. 18 mã chênh lệch (VTS, SJC, ASA, HDO, CTC, BSC…) là mã đã huỷ niêm yết mà một phiên bản trước của bước dò 2011–2012 (`delisted_probe.json`) đọc nhầm là ở HOSE; `infer_exchange` hiện tại không còn xếp chúng vào HOSE. Chúng không làm đổi con số đã công bố: trên dữ liệu tải mới, `factor` vẫn cho TRAIN 17,65% / −26,40%, VALID 13,86% / −11,99%, TEST 8,35% / −20,24%, và `momentum` trên `liq_top30` VALID 13,57% / −48,82%.

## Dòng lệnh

```bash
python -m research list                                   # các chiến lược có sẵn
python -m research run factor --split VALID               # bot "Phòng thủ" trên VALID
python -m research run momentum --split TRAIN --universe liq_top50 --slippage-mult 2
python -m research run ew --start 2016-01-01 --end 2019-12-31 --out research/out/ew_2016
python -m research run vnindex --split VALID              # VNINDEX mua và giữ
python -m research baselines                              # bảng baseline -> research/out/baselines.{json,md}
python -m research run factor --split TEST --unlock-test  # chỉ người kiểm định, một lần
```

`run <chiến lược>`:

| chiến lược | là gì |
|---|---|
| `factor` | Phòng thủ: chạy chính module của bot, `tradingagents/trading/factor.py`, từng phiên như `TradingBot` chạy (rổ vốn hoá cap50 tự dựng, bỏ qua `--universe`) |
| `momentum` | luật động lượng của repo (`momentum.decide/plan`, tham số DEFAULT) trên `--universe` |
| `ew` | chia đều cho cả rổ `--universe`, dựng lại cuối mỗi tháng |
| `quant` | Quant Engine (tín hiệu `simulator`) trên rổ VN30 hôm nay, **không** phát lại dòng tiền; chậm |
| `vnindex` | VNINDEX mua và giữ (chỉ số giá, không phí) |
| `buyhold:MÃ` | mua một mã ở phiên đầu và giữ |

Tuỳ chọn:

- `--split TRAIN|VALID|TRAIN+VALID|TEST|ALL` (mặc định VALID), hoặc `--start/--end YYYY-MM-DD`.
- `--unlock-test`: mở khoá TEST (xem dưới).
- `--capital 1e9`: vốn ban đầu.
- `--slippage-mult 2`: nhân trượt giá (thử độ bền với chi phí).
- `--max-adv-pct 0.05`: mỗi lệnh mua tối đa 5% khối lượng trung bình 20 phiên.
- `--universe liq_top30|liq_top50|liq_top100|vn30_today` (mặc định `liq_top30`).
- `--next-day calendar|guess`: chỉ cho `factor`.
  - `calendar` (mặc định): biết phiên kế tiếp thật, như khi bot chạy bù và như `factor.run`.
  - `guess`: đoán phiên kế tiếp theo lịch nghỉ, như bot ở phiên mới nhất. Lịch nghỉ trước 2026 không có Tết âm lịch, nên đôi khi lần xét tuần hoặc dựng rổ chạy trễ một phiên. VALID với `guess`: 13,5% / −12,8%.
- `--out THƯ_MỤC`: ghi `summary.json` (chỉ số, VNINDEX, từng năm, tuỳ chọn), `nav.csv` (NAV, tiền, tỷ trọng, VNINDEX theo ngày), `fills.csv` (mọi lệnh khớp: giá, phí + thuế, lãi/lỗ), `yearly.csv`.

Kết quả in ra gồm bảng chỉ số (chiến lược và VNINDEX cùng giai đoạn) và lợi nhuận từng năm. Ví dụ trên dữ liệu tải ngày 15/10/2026:

```
$ python -m research run factor --split VALID
factor [cap50] 2021-01-04..2023-12-29 (748 sessions, capital 1,000,000,000 VND, slippage_mult=1.0, next_day=calendar)

                        factor VNINDEX
metric
Total return %           47.27    0.84
CAGR %                   13.86    0.28
Max drawdown %          -11.99  -40.34
Sharpe (daily, rf=0)      0.99    0.12
...
Calendar-year returns (%):
      return_pct  max_drawdown_pct  sharpe  avg_exposure_pct  vnindex_pct  excess_pct
year
2021       56.93            -11.35    2.31              95.9        33.72       23.21
2022       -4.61             -7.52   -0.44              29.1       -32.78       28.17
2023       -1.62             -9.49   -0.15              40.1        12.20      -13.82
```

Ý nghĩa các chỉ số:

- **CAGR:** tính theo số ngày lịch.
- **Sharpe:** theo ngày, lãi suất phi rủi ro bằng 0, nhân √252.
- **Turnover:** một chiều, (mua + bán) / 2 / NAV bình quân / năm.
- **Time invested:** tỷ lệ phiên có hơn 5% NAV trong cổ phiếu.
- **NAV:** định giá vị thế theo giá đóng cửa; không tính chi phí thanh lý cuối kỳ.
- **Tiền mặt:** không có lãi.

## Viết một chiến lược

Một chiến lược là một object có `on_session(ctx)` (hoặc chỉ là một hàm `f(ctx)`). Hàm này được gọi ở giá đóng cửa mỗi phiên D, và kết quả trả về là lệnh cho phiên D+1:

- `None`: không làm gì;
- `dict {mã: tỷ trọng}`: rổ mục tiêu.
  - Mã đang giữ mà không có trong rổ bị bán hết ở giá mở cửa phiên sau.
  - Mã mới được mua `tỷ trọng × NAV` (làm tròn lô, chừa 2% cho gap).
  - Mã đang giữ **không** được mua thêm hay bán bớt (sổ lệnh vào một lần, ra một lần);
- `list` lệnh (`ctx.buy(...)`, `ctx.sell(...)`): dùng cho lệnh giới hạn, stop, mục tiêu;
- hoặc tự đặt lệnh lên `ctx.ledger`. Đây là cách `momentum` và `factor` dùng code của repo.

```python
from research.backtest import HOLD_RULES, run
from research.config import TRAIN, VALID


class TopKMomentum:
    name = "topk_mom"
    rules = HOLD_RULES                     # giữ tới khi chiến lược bán (không stop / mục tiêu)

    def __init__(self, k=5, lookback=126, market_ma=200, every=21):
        self.k, self.lookback, self.market_ma, self.every = k, lookback, market_ma, every

    def on_session(self, ctx):
        vni = ctx.vnindex                  # chỉ tới phiên hôm nay
        if len(vni) < self.market_ma or vni.iloc[-1] < vni.iloc[-self.market_ma:].mean():
            return {}                      # rổ rỗng -> bán hết ở phiên sau
        n = ctx.state.get("n", 0)
        ctx.state["n"] = n + 1
        if n % self.every and ctx.positions:
            return None                    # chỉ cân bằng lại mỗi `every` phiên
        syms = ctx.universe()              # rổ point-in-time cho phiên sau
        px = ctx.history("close", syms, self.lookback + 1)
        ret = (px.iloc[-1] / px.iloc[0] - 1).dropna()
        return dict.fromkeys(ret[ret > 0].nlargest(self.k).index, 0.98 / self.k)


res = run(TopKMomentum(), *TRAIN, universe_kind="liq_top50")   # chỉnh tham số trên TRAIN
print(res.summary())   # ... CAGR +2.8% MDD -63.2% ... | VNINDEX ... CAGR +12.9%: thua chỉ số
print(res.yearly)
print(run(TopKMomentum(), *VALID, universe_kind="liq_top50").summary())   # rồi kiểm tra trên VALID
```

Ví dụ đầy đủ: `python -m research.examples.example_strategy`. Muốn gọi chiến lược mới bằng tên trên dòng lệnh, thêm nó vào `STRATEGIES` / `make()` trong `research/strategies.py`.

Những gì `ctx` cho xem, tất cả cắt tới phiên D:

- Khung giá rộng (ngày × mã): `ctx.close / open / high / low / volume / value / raw_close / exchange`.
  - `value`: giá trị khớp lệnh (đồng).
  - `raw_close`: giá chưa điều chỉnh.
  - `exchange`: sàn của mã trong phiên đó.
- Chỉ số: `ctx.vnindex`, `ctx.vn30`, `ctx.vnindex_bars`.
- Hàm tiện ích: `ctx.history(field, symbols, n)`, `ctx.universe(kind)`, `ctx.last_close(mã)`, `ctx.adv20_value(mã)`.
- Sổ lệnh: `ctx.ledger`, `ctx.nav`, `ctx.cash`, `ctx.positions`.
- Bộ nhớ riêng của chiến lược trong suốt lần chạy: `ctx.state`.
- **Không đọc `ctx._panel`**: đó là toàn bộ dữ liệu, kể cả tương lai.

Rổ (`research.universe`), dựng từ dữ liệu **trước** ngày giao dịch:

- `liq_topN`: N mã HOSE có trung vị giá trị khớp 60 phiên cao nhất (phiên không giao dịch tính bằng 0). Điều kiện:
  - đang ở HOSE;
  - có ít nhất 250 phiên;
  - giá chưa điều chỉnh từ 5.000đ;
  - có giao dịch trong 5 phiên gần nhất.
- `vn30_today`: rổ VN30 hôm nay, **thiên lệch** (survivorship và nhìn trước). Chỉ dùng để so sánh.

`run(strategy, start, end, capital=1e9, universe_kind="liq_top30", slippage_mult=1.0, max_adv_pct=None, unlock_test=False)` trả về `Result` gồm:

- `.metrics` và `.benchmark` (VNINDEX cùng giai đoạn);
- `.yearly`, `.daily`, `.fills`, `.ledger`;
- `.warnings` (ví dụ: vị thế kẹt vì mã ngừng giao dịch hẳn);
- `.summary()`.

Hiệu chỉnh trượt giá là biến toàn cục: chạy song song bằng nhiều **tiến trình**, không dùng thread.

## Giai đoạn và khoá TEST

| giai đoạn | ngày | dùng để |
|---|---|---|
| TRAIN | 01/01/2013 – 31/12/2020 | tìm ý tưởng, chỉnh tham số |
| VALID | 01/01/2021 – 31/12/2023 | kiểm tra ngoài mẫu, so sánh ứng viên |
| TEST | 01/01/2024 – nay | **chạy một lần**, sau khi đã chốt quy tắc chọn |

`run()` từ chối mọi giai đoạn kết thúc sau 31/12/2023 (`TestPeriodLocked`) nếu không có `unlock_test=True`. `load_panel()` mặc định trả về một bản **cắt** ở 31/12/2023, và chiến lược chỉ thấy bản cắt đó. Nhưng đó không phải bức tường: panel đầy đủ, gồm cả giá TEST, vẫn nằm trong bộ nhớ (`research.data._FULL`) và trong cache `research_panel.pkl`. Khoá dựa vào việc kiểm tra ngày của `run()` và bản cắt được truyền xuống, nên code nghiên cứu không được đọc `_FULL` hay tệp cache. Dòng lệnh cần `--unlock-test` cho `--split TEST`, `ALL` hoặc `--end` sau 2023.

**Vì sao phải khoá: chống overfitting.** Mỗi lần nhìn kết quả rồi sửa chiến lược là một lần "học" từ dữ liệu đó. Sau vài trăm lần thử trên cùng một giai đoạn, con số đẹp nhất phần lớn là may mắn, không phải lợi thế thật. Mỗi nhóm trong đợt nghiên cứu 2026 đã chạy 156–1.120 lần trên TRAIN và VALID; VALID vì vậy đã "nhiễm" một phần. TEST là phép thử sạch duy nhất, và chỉ sạch nếu:

1. quy tắc chọn được viết ra **trước** khi chạy TEST (đợt 2026 ghi trong `protocol.py` lúc 05:08 UTC, lần chạy TEST đầu tiên lúc 05:34; `protocol.py` là tệp riêng của người kiểm định, không có trong repo, nội dung quy tắc được chép đầy đủ ở [RESULTS.md §3](RESULTS.md#3-quy-tắc-chọn-chốt-trước-khi-chạy-test));
2. TEST chỉ chạy **một lần**, cho các ứng viên đã chốt;
3. không quay lại sửa chiến lược vì kết quả TEST. Nếu sửa, TEST đã thành dữ liệu huấn luyện; phải đợi thêm dữ liệu mới (từ 15/10/2026 trở đi) làm TEST mới.

Thói quen tốt:

- chỉnh tham số trên TRAIN, xem VALID;
- đọc `res.yearly`: có thắng ở đa số các năm không?;
- thử tham số lân cận (±1 bước không được lật kết quả);
- thử `--slippage-mult 2`, `--max-adv-pct 0.05`, rổ `liq_top50/100`, dời ngày bắt đầu 1–3 tháng;
- luôn so với `ew`, `momentum` và `vnindex` cùng giai đoạn.

## Những điều cần biết trước khi tin một con số

1. **Thiên lệch sống sót đã giảm nhưng chưa hết.**
   - Từ 2013, ảnh chụp tháng liệt kê đủ mọi mã HOSE, kể cả mã sau này huỷ niêm yết.
   - Vẫn thiếu: mã huỷ niêm yết trước 2013 không có trong danh sách DELISTED của VCI, và mã cũ bị tái sử dụng cho công ty mới (mang lịch sử của công ty mới).
   - 18 mã chỉ có dữ liệu VNDirect từ 2013, chất lượng thấp hơn.
   - 7 mã đã huỷ niêm yết (GTN, BHS, KTB, NHS, BCI, SEC, PTK) không có số cổ phiếu lưu hành nên không bao giờ vào rổ vốn hoá của `factor`. Đây là thiên lệch lạc quan nhỏ.
2. **VNINDEX là chỉ số giá, không cộng cổ tức**, còn giá cổ phiếu đã điều chỉnh (có cổ tức). So với VNINDEX, chiến lược được lợi khoảng 1,5–2 điểm/năm. `vnindex` mua và giữ cũng không có phí; một ETF thật có phí và sai lệch.
3. **Giá điều chỉnh tính tới hôm nay** (cổ tức, thưởng, quyền theo VCI).
   - Lô và bước giá tính trên mức giá đã điều chỉnh. Trước khoảng 2017, lô vì vậy "rẻ" hơn thật, còn trượt giá làm tròn theo bước giá thì thô hơn. TEST gần giá thật nhất.
   - Ngưỡng 5.000đ và xếp hạng thanh khoản dùng giá và giá trị **chưa điều chỉnh**, nên đúng theo lịch sử.
4. **Khớp ở giá mở cửa** cộng trượt giá, cả lệnh, bất kể khối lượng.
   - Trừ khi đặt `--max-adv-pct`. Ở 1 tỷ đồng với `liq_top30/50` thì ổn; với mã nhỏ hoặc vốn lớn nên đặt giới hạn này.
   - Lệnh ATO thật có thể khớp khác.
   - Giá trị thoả thuận không tính vào thanh khoản.
5. **Sổ lệnh:** mỗi mã vào một lần, ra một lần. Lệnh bán chỉ bán phần đã về tài khoản (T+2); phần chưa về phải đặt lại.
6. **Mã ngừng giao dịch hẳn** (sáp nhập, ví dụ BHS vào SBT năm 2017) nằm lại trong sổ ở giá cuối và không bán được. `Result.warnings` liệt kê các trường hợp này. Hoán đổi cổ phiếu không được mô phỏng.
7. **`quant` không phải Quant Engine đang chạy:** không phát lại dòng tiền (khối ngoại, tự doanh, ETF, độ rộng) và chạy trên rổ VN30 hôm nay. Không có dòng tiền, engine gần như không bao giờ nói BUY.
8. Lịch sử chỉ số VN30 trên VCI bắt đầu từ 02/2012.

## Kiểm thử

```bash
python -m pytest -q tests/test_research_harness.py                  # dữ liệu giả lập, không cần mạng
python -m pytest -q tests/test_research_harness.py -m integration   # trên dữ liệu đã tải (~30 giây)
```

Test trên dữ liệu giả lập:

- mua và giữ khớp đúng lợi nhuận giá trừ phí, thuế, trượt giá, tới từng đồng;
- T+2 chặn lệnh bán ở T+1;
- `ctx` không bao giờ có dòng sau phiên D;
- khoá TEST trong `run()`, `load_panel()` và dòng lệnh;
- giới hạn thanh khoản;
- hệ số trượt giá được trả lại sau lần chạy;
- bộ tải cập nhật đúng (nối phần mới, tải lại khi giá bị điều chỉnh lại);
- bộ tải: tệp rỗng được tải lại, mã lỗi không để lại tệp, Ctrl-C huỷ hàng đợi, `--symbols` không dời `data_end` và không giữ mãi mã gõ sai trong `"failed"`;
- rổ `liq_topN` không bao giờ lấy nhầm kết quả của một panel cũ (cache gắn với chính object panel);
- `buyhold:MÃ` với mã không có trong dữ liệu và `baselines` khi chưa tải dữ liệu báo lỗi gọn, mã thoát 1;
- chiến lược `factor` khớp từng lệnh với bản chuyển ngữ code kiểm định và với module của bot (`tests/test_factor.py`).

Test `integration` bị bỏ qua nếu chưa có dữ liệu. Chúng kiểm tra:

- mua và giữ FPT năm 2019;
- `momentum` trong bộ khung trùng với `momentum.run` của repo;
- các con số đã công bố (dữ liệu tới 14/10/2026): `factor` VALID 13,86% / −12,0%, `momentum` trên `liq_top30` VALID 13,57% / −48,82%.

## Tệp

| tệp | nội dung |
|---|---|
| `config.py` | đường dẫn (`TRADING_VN_RESEARCH_DATA`), các giai đoạn, ngày khoá |
| `download.py` | tải và cập nhật dữ liệu |
| `data.py` | `Panel`, `load_panel` (cache pickle, bản cắt khoá TEST) |
| `universe.py` | rổ `liq_topN`, `vn30_today` |
| `backtest.py` | `run`, `run_index`, `Context`, `Result` |
| `metrics.py` | chỉ số |
| `strategies.py` | `RepoMomentum`, `EqualWeightRebalance`, `BuyHoldSymbol`, danh sách theo tên |
| `factor_book.py` | `DefensiveBook`: chạy `tradingagents/trading/factor.py` từng phiên như bot |
| `quant_engine.py` | Quant Engine |
| `baselines.py` | bảng baseline |
| `cli.py`, `__main__.py` | dòng lệnh |
| `examples/` | ví dụ chiến lược |
