# Kết quả kiểm định độc lập ngày 15/10/2026

Đây là tóm tắt báo cáo của người kiểm định độc lập: đợt chọn chiến lược **Phòng thủ** (`tradingagents/trading/factor.py`) làm chiến lược mặc định của bot. Mọi con số được chép nguyên từ báo cáo gốc.

**Điều kiện chung của mọi con số:**

- **Dữ liệu:** tới 14/10/2026, nến đã điều chỉnh của VCI, 568 mã HOSE và từng ở HOSE (tải mới bằng bộ tải trong repo được ~550 mã, cùng kết quả; xem [README.md](README.md#tải-dữ-liệu)).
- **Sổ lệnh:** của repo, đúng như bot dùng. Khớp ở giá mở cửa phiên sau, T+2, lô 100, bước giá, biên độ/khoá trần sàn, phí mua 0,15%, phí bán 0,15% + thuế 0,1%, trượt giá 0,1% mỗi chiều.
- **Vốn:** 1 tỷ đồng. Tiền mặt không có lãi.

**Đơn vị:** CAGR và MDD (sụt giảm tối đa) tính bằng %.

Cách chạy lại bằng bộ khung trong repo: xem [README.md](README.md) và [phần cuối](#chạy-lại-bằng-bộ-khung-trong-repo).

## 1. Quyết định

**Người thắng: `lvhi52_cap50_sma200`** (nhóm trend-index, mã `TI_lvhi52`), nay là chiến lược Phòng thủ của bot.

- Đây là rổ 10 mã vốn hoá lớn, vòng quay vốn thấp, chọn trong 50 mã vốn hoá lớn nhất của nhóm cổ phiếu HOSE thanh khoản.
- Xếp hạng theo **biến động 1 năm thấp** cộng **độ gần đỉnh 52 tuần**.
- Rổ dựng lại mỗi tháng, có vùng đệm giữ mã.
- Chỉ cầm cổ phiếu khi VNINDEX đóng cửa trên trung bình 200 phiên, xét mỗi tuần.

| | TRAIN 2013-2020 | VALID 2021-2023 | TEST 2024-01..2026-10 | 2013-01..2026-10, một lần chạy liên tục |
|---|---|---|---|---|
| người thắng CAGR / MDD / Sharpe | 18.1 / -25.9 / 1.37 | 13.9 / -12.0 / 0.99 | 8.1 / -20.8 / 0.55 | 15.6 / -25.9 / 1.11 |
| động lượng của repo, cùng rổ (cap50) | 10.7 / -48.9 | 13.4 / -47.5 | 7.5 / -41.3 | 10.9 / -48.9 |
| động lượng của repo, rổ VN30 point-in-time (cap30) | 4.3 / -43.9 | 2.9 / -46.4 | 11.8 / -33.1 | 4.7 / -45.2 |
| VNINDEX mua và giữ (chỉ số giá, không phí) | 12.9 / -45.3 | 0.3 / -40.3 | 15.9 / -18.1 | 10.7 / -45.3 |

`cap30`: 30 mã vốn hoá lớn nhất trong 100 mã thanh khoản nhất tại từng thời điểm. Đây là bản xấp xỉ point-in-time của rổ VN30, mà luật động lượng cũ của bot dùng.

## 2. Các ứng viên

Có 14 ứng viên từ 5 nhóm nghiên cứu, cộng các baseline. Mọi ứng viên chạy qua cùng bộ khung, theo điểm vào của chính nhóm đó.

| mã | chiến lược | nhóm |
|---|---|---|
| MC_C1 | C1 riskadj 6m mom top15 liq_top100, rebal regime, 3 tranches | momentum-core |
| MC_C2 | C2 = C1 single book | momentum-core |
| MC_C3 | C3 = core with daily MA100 regime | momentum-core |
| RR_ens3_atr3 | ens3_atr3: MA50/100/200 vote + ATR3 stop | regime-risk |
| RR_ma100_atr3 | ma100_atr3: repo MA100 + ATR3 stop | regime-risk |
| RR_ens3 | ens3: MA vote, no stop | regime-risk |
| TI_hi52 | hi52_cap50_sma200 | trend-index |
| **TI_lvhi52** | **lvhi52_cap50_sma200** | trend-index |
| TI_cap30ew | cap30_ew_sma200 | trend-index |
| EH_C1 | C1 engine-filtered momentum k10, 3 tranches | engine-hybrid |
| EH_C3 | C3 engine book exit-on-SELL | engine-hybrid |
| PB_C1 | C1 50% M(ivol mom, 3 tr) + 50% F(lvhi52) | portfolio-blend |
| PB_C2 | C2 25% M + 75% F | portfolio-blend |
| PB_C3 | C3 75% M + 25% F | portfolio-blend |

Baseline:

- `B_repo_*`: luật động lượng của repo (`momentum.decide/plan`, tham số DEFAULT) trên các rổ `vn30_today` (thiên lệch), `liq_top30`, `liq_top100`, `cap30`, `cap50`.
- `B_cap30_ew`: rổ cap30 chia đều, không lọc xu hướng.
- `B_vnindex`: VNINDEX mua và giữ.

**Các giai đoạn:**

- TRAIN: 2013-01-01..2020-12-31.
- VALID: 2021-01-01..2023-12-31.
- TEST: 2024-01-01..2026-10-14, chạy **một lần**.
- Thêm một lần chạy liên tục 2013-01-01..2026-10-14.

**Các biến thể trên mỗi giai đoạn:**

- base: vốn 1e9, trượt giá 1x;
- slip2: trượt giá gấp đôi;
- cap3e8: vốn 3e8;
- adv10: mỗi lệnh mua tối đa 10% khối lượng trung bình 20 phiên;
- sh1m/sh2m/sh3m: dời ngày bắt đầu 1, 2, 3 tháng.

Tổng cộng 453 lần chạy theo giai đoạn và 44 lần chạy liên tục, 0 lỗi.

**Tái lập:** cả 34 con số TRAIN/VALID mà các nhóm báo cáo đều được tính lại khớp, chỉ khác làm tròn. Ví dụ `TI_lvhi52` VALID báo cáo 13.86 / -12.00 / 0.990, tính lại 13.86 / -11.99 / 0.993; `B_repo_liq30` VALID 13.60 / -48.80 / 0.590, tính lại 13.57 / -48.82 / 0.586.

**Nhìn trước tương lai (look-ahead):** không tìm thấy, qua cả đọc code từng nhóm lẫn phép thử cắt dữ liệu.

- Phép thử chạy mỗi chiến lược hai lần: trên dữ liệu đầy đủ tới 2026 và trên bản cắt tại một ngày. NAV, lệnh khớp và lệnh còn mở tại ngày cắt phải trùng khớp.
- Mọi lần chạy trùng khớp tuyệt đối.
- Hai chiến lược cố tình nhìn trước 5 phiên và 1 phiên đều bị phát hiện.

## 3. Quy tắc chọn, chốt trước khi chạy TEST

Quy tắc được ghi vào `protocol.py` lúc 05:08 UTC; lần chạy TEST đầu tiên bắt đầu lúc 05:34. Nó được áp dụng máy móc trong `agg.py`. Hai tệp này là script riêng của người kiểm định và **không có trong repo** (xem [phần cuối](#chạy-lại-bằng-bộ-khung-trong-repo)); quy tắc được chép đầy đủ dưới đây. Một ứng viên phải qua cả bốn cửa:

1. **Không nhìn trước tương lai.**
2. **Thắng luật động lượng của repo trên cùng rổ, ở cả VALID và TEST.** Thắng nghĩa là CAGR cao hơn, hoặc CAGR kém không quá 1 điểm nhưng MDD tốt hơn ít nhất 5 điểm. "Cùng rổ" của từng nhóm:
   - momentum-core: liq_top100;
   - regime-risk, engine-hybrid: liq_top30;
   - rổ factor của trend-index: cap50;
   - cap30_ew: cap30;
   - portfolio-blend: cả liq_top100 và cap50.
3. **MDD không tệ hơn -35%** ở TRAIN, VALID và TEST.
4. **Bền qua các phép thử độ bền** (dung sai 0,5 điểm, một lần):
   - trượt giá 2x mất không quá 2 điểm;
   - vốn 3e8 mất không quá 3 điểm;
   - giới hạn ADV mất không quá 2 điểm;
   - thắng baseline dời ngày tương ứng ở ít nhất 2/3 lần dời, cả VALID lẫn TEST;
   - không lần dời nào có MDD tệ hơn -40%.

**Xếp hạng:** trung bình CAGR của VALID và TEST. Các ứng viên gần hoà nhau (trong 2 điểm CAGR và 5 điểm MDD) được phân định bằng **số tham số ít nhất**.

| mã | số tham số | 1. không nhìn trước | 2. thắng repo cùng rổ VALID+TEST | 3. MDD<=35% mọi giai đoạn (tệ nhất) | 4. độ bền | TB CAGR VALID/TEST | kết quả |
|---|---|---|---|---|---|---|---|
| MC_C1 | 6 | có | có | không (-35.6) | không (S4mdd:VALID:sh3m:-41.3) | 9.5 | loại |
| MC_C2 | 6 | có | có | không (-37.2) | không (S4mdd:TRAIN:sh3m:-40.1;S4mdd:VALID:sh2m:-50.9;S4mdd:VALID:sh3m:-55.5) | 13.2 | loại |
| MC_C3 | 6 | có | không (TEST<B_repo_liq100) | không (-44.6) | không (S4mdd:TRAIN:sh1m:-44.6;S4mdd:TRAIN:sh2m:-44.6;S4mdd:TRAIN:sh3m:-44.5;S4beat:TEST:0/3) | 3.0 | loại |
| RR_ens3_atr3 | 3 | có | không (TEST<B_repo_liq30) | không (-50.9) | không (S4mdd:TRAIN:sh1m:-50.9;S4mdd:TRAIN:sh2m:-50.9;S4mdd:TRAIN:sh3m:-50.9;S4beat:TEST:0/3) | 4.7 | loại |
| RR_ma100_atr3 | 2 | có | không (VALID<B_repo_liq30) | không (-46.2) | không (S4mdd:VALID:sh1m:-46.1;S4mdd:VALID:sh2m:-46.2;S4mdd:VALID:sh3m:-46.1) | 7.4 | loại |
| RR_ens3 | 1 | có | không (TEST<B_repo_liq30) | không (-51.0) | không (S4mdd:TRAIN:sh1m:-51.0;S4mdd:TRAIN:sh2m:-51.1;S4mdd:TRAIN:sh3m:-51.1;S4mdd:TEST:sh1m:-40.6;S4mdd:TEST:sh2m:-40.6;S4mdd:TEST:sh3m:-40.3;S4beat:TEST:0/3) | 1.9 | loại |
| TI_hi52 | 6 | có | không (TEST<B_repo_cap50) | có (-29.3) | có | 10.6 | loại |
| TI_lvhi52 | 6 | có | có | có (-25.9) | có | 11.0 | **ĐẠT** |
| TI_cap30ew | 3 | có | không (TEST<B_repo_cap30) | không (-36.0) | không (S4beat:TEST:0/3) | 6.5 | loại |
| EH_C1 | 4 | có | không (VALID<B_repo_liq30) | có (-34.9) | không (S4beat:VALID:0/3) | 6.3 | loại |
| EH_C3 | 2 | có | không (VALID<B_repo_liq30) | có (-32.7) | không (S4beat:VALID:0/3) | -2.5 | loại |
| PB_C1 | 13 | có | có | có (-26.6) | có | 10.2 | **ĐẠT** |
| PB_C2 | 13 | có | có | có (-25.9) | có | 10.6 | **ĐẠT** |
| PB_C3 | 13 | có | không (VALID<B_repo_cap50) | có (-28.9) | không (S4beat:TEST:1/3) | 9.4 | loại |

**Ba ứng viên đạt:** TI_lvhi52 (10.97, 6 tham số), PB_C2 (10.60, 13 tham số), PB_C1 (10.22, 13 tham số).

- Cả ba nằm trong 2 điểm CAGR và 5 điểm MDD của nhau, nên số tham số quyết định: **TI_lvhi52**.
- Nó cũng là một sổ lệnh duy nhất, so với 4 sổ con của mỗi bản pha trộn.
- Giữa ba ứng viên, nó còn đứng đầu về CAGR riêng VALID, riêng TEST và chạy liên tục (15.61 so với 15.49 và 15.45).
- PB_C2 có Sharpe chạy liên tục nhỉnh hơn (1.12 so với 1.11).

**Độ nhạy của quyết định** (mỗi lần đổi một quy tắc):

- Chỉ so các bản pha trộn với liq_top100 (cap50 là tập con của nó): PB_C3 cũng đạt. Người thắng không đổi.
- Nới ngưỡng MDD 35% thành 37% hoặc 45%: không thêm ai đạt. Người thắng không đổi.
- So các rổ trend-index với rổ VN30 point-in-time (cap30) thay vì cap50:
  - TI_lvhi52 trượt ở TEST (8.1 < 11.8), và người thắng hình thức thành PB_C2.
  - PB_C2 là 75% chính rổ này và cũng kém cap30 ở TEST (7.9).
  - Nếu mọi ứng viên đều phải thắng cap30 ở TEST thì **không ai đạt**. Cách đọc đó nghĩa là giữ bot cũ (xem mục 6).

## 4. Bảng so sánh đầy đủ (base: 1e9 đồng, trượt giá 1x)

Lưu ý khi đọc bảng:

- `B_vnindex` không có phí.
- `B_repo_vn30today` thiên lệch sống sót và nhìn trước (rổ VN30 hôm nay).
- VALID của mọi nhóm là một phần trong mẫu: các nhóm đã xem VALID và vùng lân cận khi chọn. TEST là phép thử sạch duy nhất.

| mã | chiến lược | TRAIN CAGR | TRAIN MDD | TRAIN Sharpe | VALID CAGR | VALID MDD | VALID Sharpe | TEST CAGR | TEST MDD | TEST Sharpe | TEST turnover/năm | TEST tỷ trọng TB % |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| B_vnindex | VNINDEX buy&hold (price index, no costs) | 12.9 | -45.3 | 0.81 | 0.3 | -40.3 | 0.12 | 15.9 | -18.1 | 0.91 | - | 100 |
| B_repo_vn30today | repo momentum, today's VN30 (biased) | 10.9 | -41.5 | 0.63 | -2.1 | -55.0 | 0.01 | 26.2 | -22.1 | 1.23 | 8.2 | 72 |
| B_repo_liq30 | repo momentum, liq_top30 | 3.1 | -46.0 | 0.25 | 13.6 | -48.8 | 0.59 | 0.1 | -36.9 | 0.12 | 8.5 | 72 |
| B_repo_liq100 | repo momentum, liq_top100 | 1.3 | -49.9 | 0.18 | -2.1 | -45.1 | 0.07 | 2.2 | -44.5 | 0.21 | 9.2 | 72 |
| B_repo_cap30 | repo momentum, cap30 (PIT VN30 proxy) | 4.3 | -43.9 | 0.31 | 2.9 | -46.4 | 0.24 | 11.8 | -33.1 | 0.61 | 8.4 | 72 |
| B_repo_cap50 | repo momentum, cap50 | 10.7 | -48.9 | 0.57 | 13.4 | -47.5 | 0.60 | 7.5 | -41.3 | 0.43 | 8.7 | 72 |
| B_cap30_ew | EW cap30 basket, untimed (index proxy, with costs) | 15.2 | -43.2 | 0.85 | -0.2 | -43.9 | 0.10 | 19.9 | -17.1 | 1.07 | 0.3 | 96 |
| MC_C1 | C1 riskadj 6m mom top15 liq_top100, rebal regime, 3 tranches | 15.9 | -35.6 | 1.00 | 10.8 | -35.5 | 0.58 | 8.2 | -26.4 | 0.52 | 4.1 | 67 |
| MC_C2 | C2 = C1 single book | 15.7 | -37.2 | 0.95 | 17.3 | -35.2 | 0.81 | 9.0 | -30.6 | 0.52 | 4.2 | 68 |
| MC_C3 | C3 = core with daily MA100 regime | 14.2 | -44.6 | 0.88 | 6.2 | -36.5 | 0.39 | -0.2 | -32.7 | 0.09 | 7.6 | 70 |
| RR_ens3_atr3 | ens3_atr3: MA50/100/200 vote + ATR3 stop | 8.6 | -50.9 | 0.52 | 16.6 | -36.1 | 0.76 | -7.3 | -37.5 | -0.23 | 9.8 | 70 |
| RR_ma100_atr3 | ma100_atr3: repo MA100 + ATR3 stop | 6.1 | -39.5 | 0.40 | 13.3 | -46.2 | 0.61 | 1.5 | -34.7 | 0.18 | 9.1 | 66 |
| RR_ens3 | ens3: MA vote, no stop | 2.5 | -51.0 | 0.23 | 12.7 | -41.0 | 0.59 | -8.9 | -40.6 | -0.27 | 8.7 | 75 |
| TI_hi52 | hi52_cap50_sma200 | 25.2 | -29.3 | 1.55 | 15.9 | -25.1 | 0.85 | 5.2 | -23.8 | 0.37 | 5.4 | 81 |
| **TI_lvhi52** | **lvhi52_cap50_sma200** | 18.1 | -25.9 | 1.37 | 13.9 | -12.0 | 0.99 | 8.1 | -20.8 | 0.55 | 3.9 | 80 |
| TI_cap30ew | cap30_ew_sma200 | 11.0 | -36.0 | 0.81 | 7.1 | -14.1 | 0.53 | 5.8 | -19.8 | 0.45 | 2.7 | 78 |
| EH_C1 | C1 engine-filtered momentum k10, 3 tranches | 10.1 | -29.3 | 0.76 | 7.4 | -34.9 | 0.47 | 5.1 | -21.3 | 0.44 | 4.0 | 43 |
| EH_C3 | C3 engine book exit-on-SELL | 12.8 | -32.7 | 0.90 | -5.9 | -16.6 | -0.94 | 0.8 | -12.0 | 0.15 | 0.5 | 10 |
| PB_C1 | C1 50% M(ivol mom, 3 tr) + 50% F(lvhi52) | 18.0 | -26.6 | 1.37 | 12.7 | -20.6 | 0.85 | 7.8 | -17.5 | 0.56 | 3.9 | 71 |
| PB_C2 | C2 25% M + 75% F | 18.0 | -25.9 | 1.39 | 13.3 | -13.9 | 0.96 | 7.9 | -16.8 | 0.57 | 3.7 | 73 |
| PB_C3 | C3 75% M + 25% F | 18.1 | -28.9 | 1.30 | 11.6 | -27.9 | 0.71 | 7.2 | -21.0 | 0.51 | 4.0 | 69 |

Chạy liên tục 2013-01..2026-10 (base):

| mã | CAGR % | MDD % | Sharpe | tỷ trọng TB % | turnover/năm | phí+thuế % vốn |
|---|---|---|---|---|---|---|
| B_vnindex | 10.7 | -45.3 | 0.66 | 100 | - | - |
| B_cap30_ew | 13.4 | -43.2 | 0.73 | 100 | 0.2 | 3 |
| B_repo_vn30today | 11.6 | -53.0 | 0.65 | 65 | 7.3 | 90 |
| B_repo_liq30 | 5.2 | -50.0 | 0.33 | 66 | 7.6 | 61 |
| B_repo_cap30 | 4.7 | -45.2 | 0.32 | 66 | 7.7 | 59 |
| B_repo_cap50 | 10.9 | -48.9 | 0.56 | 66 | 7.7 | 108 |
| **TI_lvhi52** | 15.6 | -25.9 | 1.11 | 67 | 2.9 | 66 |
| PB_C2 | 15.5 | -25.9 | 1.12 | 67 | 3.0 | 68 |
| PB_C1 | 15.4 | -26.6 | 1.08 | 66 | 3.2 | 71 |
| TI_hi52 | 19.2 | -30.4 | 1.11 | 68 | 4.4 | 158 |
| MC_C1 | 14.0 | -35.6 | 0.81 | 67 | 3.5 | 65 |
| MC_C2 | 14.4 | -37.2 | 0.80 | 69 | 3.3 | 62 |
| PB_C3 | 15.4 | -28.9 | 1.01 | 66 | 3.4 | 75 |
| EH_C1 | 7.3 | -36.0 | 0.55 | 51 | 4.6 | 50 |

Ứng viên trừ luật động lượng của repo trên cùng rổ (điểm CAGR):

| mã | so với | TRAIN | VALID | TEST | VALID dời (1/2/3 tháng) | TEST dời (1/2/3 tháng) |
|---|---|---|---|---|---|---|
| MC_C1 | B_repo_liq100 | +14.6 | +12.9 | +6.0 | +6.2 / +8.3 / +11.9 | +3.4 / -3.4 / -0.1 |
| MC_C2 | B_repo_liq100 | +14.4 | +19.4 | +6.8 | +15.4 / -0.1 / +0.9 | +6.4 / -1.7 / +1.0 |
| MC_C3 | B_repo_liq100 | +12.9 | +8.3 | -2.4 | +6.1 / +3.7 / +6.3 | -4.8 / -6.5 / -3.9 |
| RR_ens3_atr3 | B_repo_liq30 | +5.5 | +3.1 | -7.4 | +2.4 / +5.4 / +6.8 | -6.3 / -5.8 / -6.2 |
| RR_ma100_atr3 | B_repo_liq30 | +3.0 | -0.2 | +1.4 | +0.1 / +0.5 / +0.3 | +1.5 / +2.8 / +1.9 |
| RR_ens3 | B_repo_liq30 | -0.5 | -0.8 | -9.0 | -0.2 / +5.1 / +6.1 | -8.3 / -8.4 / -8.3 |
| TI_hi52 | B_repo_cap50 | +14.5 | +2.5 | -2.3 | -1.1 / +10.5 / +8.8 | -1.8 / -0.7 / -0.5 |
| **TI_lvhi52** | B_repo_cap50 | +7.4 | +0.5 | +0.6 | -3.7 / +6.0 / +6.8 | -0.4 / +1.5 / +3.8 |
| TI_cap30ew | B_repo_cap30 | +6.7 | +4.2 | -5.9 | +1.4 / +7.3 / +7.2 | -6.0 / -3.8 / -2.6 |
| EH_C1 | B_repo_liq30 | +7.0 | -6.2 | +5.0 | -19.9 / -4.3 / -2.2 | -0.3 / +0.1 / +1.2 |
| EH_C3 | B_repo_liq30 | +9.8 | -19.5 | +0.7 | -30.5 / -16.2 / -13.0 | +1.4 / +3.3 / +4.1 |
| PB_C1 | B_repo_liq100 | +16.7 | +14.8 | +5.6 | +10.8 / +11.9 / +16.5 | +2.8 / -0.7 / +2.8 |
| PB_C1 | B_repo_cap50 | +7.3 | -0.7 | +0.3 | -6.6 / +4.2 / +3.4 | -0.6 / -1.0 / +1.3 |
| PB_C2 | B_repo_liq100 | +16.7 | +15.4 | +5.7 | +12.2 / +12.8 / +18.4 | +2.7 / +0.7 / +4.2 |
| PB_C2 | B_repo_cap50 | +7.3 | -0.1 | +0.4 | -5.2 / +5.1 / +5.3 | -0.7 / +0.4 / +2.7 |
| PB_C3 | B_repo_liq100 | +16.8 | +13.7 | +5.0 | +8.9 / +10.6 / +14.0 | +2.4 / -1.8 / +1.6 |
| PB_C3 | B_repo_cap50 | +7.3 | -1.8 | -0.3 | -8.4 / +2.8 / +0.9 | -1.0 / -2.1 / +0.0 |

## 5. Các phép thử độ bền

Mỗi ô là CAGR % (MDD %). Cột cuối là khoảng CAGR của bản base và 3 lần dời ngày bắt đầu.

**TRAIN**

| mã | base | slip2 | cap3e8 | adv10 | sh1m | sh2m | sh3m | khoảng CAGR khi dời |
|---|---|---|---|---|---|---|---|---|
| B_vnindex | 12.9 (-45) | - | - | - | 11.0 (-45) | 11.3 (-45) | 10.6 (-45) | 10.6..12.9 |
| B_repo_vn30today | 10.9 (-41) | 9.8 (-43) | 11.1 (-41) | 11.9 (-47) | 8.8 (-42) | 9.7 (-41) | 9.3 (-41) | 8.8..10.9 |
| B_repo_liq30 | 3.1 (-46) | 1.8 (-50) | 3.2 (-46) | 3.0 (-46) | 1.3 (-46) | 0.5 (-46) | 0.4 (-46) | 0.4..3.1 |
| B_repo_liq100 | 1.3 (-50) | -0.1 (-52) | 1.6 (-49) | 0.3 (-49) | -1.0 (-50) | 0.8 (-50) | 1.0 (-50) | -1.0..1.3 |
| B_repo_cap30 | 4.3 (-44) | 3.0 (-46) | 4.2 (-43) | 4.2 (-44) | 1.7 (-44) | 1.5 (-44) | 1.4 (-44) | 1.4..4.3 |
| B_repo_cap50 | 10.7 (-49) | 9.3 (-51) | 10.7 (-49) | 9.9 (-49) | 7.8 (-49) | 6.9 (-49) | 6.9 (-49) | 6.9..10.7 |
| B_cap30_ew | 15.2 (-43) | 15.1 (-43) | 14.9 (-43) | 15.2 (-43) | 13.4 (-43) | 13.8 (-43) | 13.2 (-43) | 13.2..15.2 |
| MC_C1 | 15.9 (-36) | 15.4 (-36) | 16.4 (-35) | 16.0 (-36) | 15.5 (-35) | 12.7 (-39) | 12.1 (-39) | 12.1..15.9 |
| MC_C2 | 15.7 (-37) | 15.0 (-37) | 15.8 (-37) | 15.5 (-37) | 16.0 (-39) | 8.0 (-40) | 7.1 (-40) | 7.1..16.0 |
| MC_C3 | 14.2 (-45) | 13.0 (-46) | 14.8 (-44) | 14.1 (-45) | 12.7 (-45) | 12.4 (-45) | 11.4 (-45) | 11.4..14.2 |
| RR_ens3_atr3 | 8.6 (-51) | 7.3 (-52) | 8.6 (-51) | 8.6 (-51) | 6.2 (-51) | 6.8 (-51) | 6.1 (-51) | 6.1..8.6 |
| RR_ma100_atr3 | 6.1 (-40) | 4.8 (-42) | 6.1 (-40) | 6.0 (-40) | 3.9 (-40) | 3.7 (-40) | 3.0 (-40) | 3.0..6.1 |
| RR_ens3 | 2.5 (-51) | 1.4 (-53) | 2.5 (-51) | 2.4 (-51) | 0.7 (-51) | 0.1 (-51) | -0.3 (-51) | -0.3..2.5 |
| TI_hi52 | 25.2 (-29) | 24.5 (-30) | 25.3 (-29) | 24.6 (-29) | 23.5 (-29) | 24.4 (-29) | 22.5 (-29) | 22.5..25.2 |
| **TI_lvhi52** | 18.1 (-26) | 17.8 (-27) | 18.0 (-26) | 17.6 (-26) | 17.1 (-26) | 17.6 (-26) | 16.2 (-26) | 16.2..18.1 |
| TI_cap30ew | 11.0 (-36) | 10.8 (-37) | 10.8 (-35) | 11.0 (-36) | 9.5 (-36) | 9.7 (-36) | 9.1 (-36) | 9.1..11.0 |
| EH_C1 | 10.1 (-29) | 9.3 (-30) | 9.9 (-28) | 10.1 (-29) | 8.3 (-32) | 8.5 (-31) | 9.1 (-32) | 8.3..10.1 |
| EH_C3 | 12.8 (-33) | 12.7 (-33) | 12.8 (-33) | 12.8 (-33) | 9.8 (-33) | 10.0 (-33) | 10.2 (-33) | 9.8..12.8 |
| PB_C1 | 18.0 (-27) | 17.4 (-27) | 17.9 (-26) | 18.0 (-27) | 17.0 (-26) | 16.2 (-27) | 15.2 (-27) | 15.2..18.0 |
| PB_C2 | 18.0 (-26) | 17.6 (-27) | 17.8 (-24) | 17.8 (-26) | 17.0 (-26) | 16.8 (-26) | 15.6 (-25) | 15.6..18.0 |
| PB_C3 | 18.1 (-29) | 17.4 (-29) | 17.6 (-29) | 17.9 (-29) | 16.9 (-30) | 15.5 (-31) | 14.7 (-31) | 14.7..18.1 |

**VALID**

| mã | base | slip2 | cap3e8 | adv10 | sh1m | sh2m | sh3m | khoảng CAGR khi dời |
|---|---|---|---|---|---|---|---|---|
| B_vnindex | 0.3 (-40) | - | - | - | 3.0 (-40) | -1.7 (-40) | -2.6 (-40) | -2.6..3.0 |
| B_repo_vn30today | -2.1 (-55) | -3.2 (-56) | -1.7 (-54) | -2.0 (-55) | 4.8 (-53) | -7.3 (-55) | -8.8 (-54) | -8.8..4.8 |
| B_repo_liq30 | 13.6 (-49) | 12.3 (-50) | 13.6 (-48) | 13.6 (-49) | 24.4 (-49) | 10.0 (-49) | 7.1 (-49) | 7.1..24.4 |
| B_repo_liq100 | -2.1 (-45) | -3.1 (-46) | -1.7 (-45) | -2.1 (-45) | 3.2 (-45) | -2.5 (-45) | -7.5 (-45) | -7.5..3.2 |
| B_repo_cap30 | 2.9 (-46) | 1.8 (-48) | 3.0 (-46) | 2.9 (-46) | 8.0 (-45) | -3.5 (-46) | -4.2 (-47) | -4.2..8.0 |
| B_repo_cap50 | 13.4 (-48) | 12.2 (-48) | 13.3 (-47) | 13.4 (-48) | 20.6 (-48) | 5.3 (-47) | 5.5 (-47) | 5.3..20.6 |
| B_cap30_ew | -0.2 (-44) | -0.4 (-44) | 0.7 (-41) | -0.2 (-44) | 2.1 (-44) | -3.5 (-43) | -4.2 (-43) | -4.2..2.1 |
| MC_C1 | 10.8 (-36) | 10.2 (-36) | 10.2 (-34) | 10.8 (-36) | 9.4 (-33) | 5.8 (-36) | 4.4 (-41) | 4.4..10.8 |
| MC_C2 | 17.3 (-35) | 17.0 (-35) | 17.7 (-34) | 17.3 (-35) | 18.7 (-38) | -2.6 (-51) | -6.7 (-55) | -6.7..18.7 |
| MC_C3 | 6.2 (-37) | 5.2 (-37) | 6.2 (-36) | 6.2 (-37) | 9.3 (-37) | 1.2 (-36) | -1.2 (-36) | -1.2..9.3 |
| RR_ens3_atr3 | 16.6 (-36) | 15.1 (-37) | 16.4 (-36) | 16.6 (-36) | 26.8 (-34) | 15.4 (-38) | 13.9 (-33) | 13.9..26.8 |
| RR_ma100_atr3 | 13.3 (-46) | 12.0 (-47) | 13.5 (-46) | 13.3 (-46) | 24.6 (-46) | 10.5 (-46) | 7.4 (-46) | 7.4..24.6 |
| RR_ens3 | 12.7 (-41) | 11.2 (-42) | 13.0 (-40) | 12.7 (-41) | 24.2 (-39) | 15.1 (-37) | 13.2 (-34) | 12.7..24.2 |
| TI_hi52 | 15.9 (-25) | 15.3 (-25) | 15.5 (-25) | 15.9 (-25) | 19.5 (-25) | 15.8 (-25) | 14.3 (-25) | 14.3..19.5 |
| **TI_lvhi52** | 13.9 (-12) | 13.4 (-12) | 13.6 (-12) | 13.9 (-12) | 16.9 (-12) | 11.4 (-12) | 12.3 (-12) | 11.4..16.9 |
| TI_cap30ew | 7.1 (-14) | 6.9 (-14) | 6.8 (-13) | 7.1 (-14) | 9.4 (-14) | 3.8 (-14) | 3.1 (-14) | 3.1..9.4 |
| EH_C1 | 7.4 (-35) | 6.7 (-35) | 7.6 (-33) | 7.4 (-35) | 4.5 (-31) | 5.7 (-34) | 4.9 (-35) | 4.5..7.4 |
| EH_C3 | -5.9 (-17) | -6.0 (-17) | -5.9 (-17) | -5.9 (-17) | -6.1 (-17) | -6.2 (-17) | -5.9 (-15) | -6.2..-5.9 |
| PB_C1 | 12.7 (-21) | 12.1 (-21) | 12.9 (-15) | 12.7 (-21) | 14.0 (-18) | 9.5 (-20) | 9.0 (-22) | 9.0..14.0 |
| PB_C2 | 13.3 (-14) | 12.8 (-14) | 12.1 (-10) | 13.3 (-14) | 15.4 (-14) | 10.4 (-14) | 10.9 (-14) | 10.4..15.4 |
| PB_C3 | 11.6 (-28) | 11.1 (-28) | 12.4 (-22) | 11.6 (-28) | 12.2 (-25) | 8.1 (-27) | 6.5 (-31) | 6.5..12.2 |

**TEST**

| mã | base | slip2 | cap3e8 | adv10 | sh1m | sh2m | sh3m | khoảng CAGR khi dời |
|---|---|---|---|---|---|---|---|---|
| B_vnindex | 15.9 (-18) | - | - | - | 14.9 (-18) | 12.3 (-18) | 11.9 (-18) | 11.9..15.9 |
| B_repo_vn30today | 26.2 (-22) | 24.4 (-23) | 25.7 (-22) | 26.2 (-22) | 23.8 (-22) | 18.1 (-22) | 15.8 (-22) | 15.8..26.2 |
| B_repo_liq30 | 0.1 (-37) | -1.4 (-38) | -0.2 (-36) | 0.1 (-37) | -0.6 (-37) | -2.4 (-37) | -3.2 (-37) | -3.2..0.1 |
| B_repo_liq100 | 2.2 (-45) | 0.8 (-45) | 1.5 (-44) | 2.2 (-45) | 2.7 (-45) | 1.6 (-45) | -1.7 (-45) | -1.7..2.7 |
| B_repo_cap30 | 11.8 (-33) | 10.2 (-34) | 11.8 (-33) | 11.8 (-33) | 10.3 (-33) | 5.6 (-33) | 4.2 (-33) | 4.2..11.8 |
| B_repo_cap50 | 7.5 (-41) | 6.1 (-42) | 7.6 (-40) | 7.5 (-41) | 6.1 (-41) | 1.9 (-41) | -0.2 (-41) | -0.2..7.5 |
| B_cap30_ew | 19.9 (-17) | 19.9 (-17) | 18.9 (-16) | 19.9 (-17) | 19.3 (-17) | 17.3 (-17) | 16.9 (-17) | 16.9..19.9 |
| MC_C1 | 8.2 (-26) | 7.5 (-27) | 7.3 (-20) | 8.2 (-26) | 6.1 (-30) | -1.9 (-30) | -1.9 (-30) | -1.9..8.2 |
| MC_C2 | 9.0 (-31) | 8.2 (-31) | 8.0 (-29) | 9.0 (-31) | 9.0 (-30) | -0.1 (-31) | -0.7 (-31) | -0.7..9.0 |
| MC_C3 | -0.2 (-33) | -1.3 (-34) | 0.3 (-32) | -0.2 (-33) | -2.1 (-33) | -5.0 (-33) | -5.7 (-32) | -5.7..-0.2 |
| RR_ens3_atr3 | -7.3 (-37) | -9.1 (-39) | -6.7 (-37) | -7.3 (-37) | -6.9 (-37) | -8.2 (-37) | -9.4 (-37) | -9.4..-6.9 |
| RR_ma100_atr3 | 1.5 (-35) | -0.1 (-36) | 1.2 (-34) | 1.5 (-35) | 1.0 (-35) | 0.4 (-35) | -1.2 (-35) | -1.2..1.5 |
| RR_ens3 | -8.9 (-41) | -10.1 (-41) | -8.5 (-40) | -8.9 (-41) | -8.8 (-41) | -10.8 (-41) | -11.4 (-40) | -11.4..-8.8 |
| TI_hi52 | 5.2 (-24) | 4.5 (-24) | 5.3 (-24) | 5.2 (-24) | 4.3 (-23) | 1.1 (-24) | -0.7 (-24) | -0.7..5.2 |
| **TI_lvhi52** | 8.1 (-21) | 7.5 (-21) | 8.0 (-20) | 8.1 (-21) | 5.7 (-21) | 3.4 (-21) | 3.6 (-21) | 3.4..8.1 |
| TI_cap30ew | 5.8 (-20) | 5.4 (-20) | 4.8 (-19) | 5.8 (-20) | 4.4 (-19) | 1.8 (-20) | 1.6 (-20) | 1.6..5.8 |
| EH_C1 | 5.1 (-21) | 4.3 (-22) | 6.4 (-19) | 5.1 (-21) | -0.8 (-24) | -2.3 (-25) | -1.9 (-24) | -2.3..5.1 |
| EH_C3 | 0.8 (-12) | 0.8 (-12) | 0.9 (-12) | 0.8 (-12) | 0.9 (-12) | 0.9 (-12) | 0.9 (-12) | 0.8..0.9 |
| PB_C1 | 7.8 (-17) | 7.0 (-18) | 5.6 (-16) | 7.8 (-17) | 5.5 (-19) | 0.9 (-19) | 1.1 (-19) | 0.9..7.8 |
| PB_C2 | 7.9 (-17) | 7.2 (-17) | 6.8 (-15) | 7.9 (-17) | 5.4 (-17) | 2.2 (-17) | 2.5 (-17) | 2.2..7.9 |
| PB_C3 | 7.2 (-21) | 6.5 (-21) | 4.9 (-18) | 7.2 (-21) | 5.1 (-23) | -0.3 (-23) | -0.2 (-23) | -0.3..7.2 |

Các phép thử cho thấy:

- **Chi phí (trượt giá 2x):** mất từ -0.04 tới -1.9 điểm/năm.
  - Các rổ vòng quay thấp (trend-index, pha trộn; khoảng 2-4 lần/năm) mất 0.4-0.75 điểm.
  - Luật của repo (khoảng 7-9 lần/năm) mất 1.0-1.6 điểm.
- **Vốn 3e8:**
  - Các rổ một sổ không bị ảnh hưởng: `TI_lvhi52` TEST 8.0 so với 8.1.
  - Các rổ chia nhánh chỉ còn 0.5-1e8 mỗi nhánh. Ở mức giá TEST, làm tròn lô 100 khiến một số mã không mua được: `PB_C1` TEST 5.6 so với 7.8, `MC_C1` 7.3 so với 8.2.
- **Thanh khoản (giới hạn 10% ADV):** hầu như không ràng buộc ở 1e9. Ảnh hưởng lớn nhất khoảng 1 điểm ở TRAIN (thị trường mỏng 2013-2014). Với người thắng: -0.5 ở TRAIN, 0 ở VALID và TEST.
- **Ngày bắt đầu là nguồn chênh lệch lớn nhất.**
  - Các rổ động lượng đếm phiên vẫn dao động mạnh, kể cả khi chia 3 nhánh:
    - `MC_C1` TEST -1.9..8.2, VALID 4.4..10.8 (MDD -41% với lần dời 3 tháng);
    - `MC_C2` VALID -6.7..18.7 (MDD tới -55%);
    - `EH_C1` TEST -2.3..5.1.
  - Các rổ neo theo lịch dao động ít hơn nhiều: ở TEST `TI_lvhi52` 3.4..8.1 và `PB_C2` 2.2..7.9, trong khi VNINDEX trên cùng các cửa sổ dao động 11.9..15.9. Phần lớn độ chênh đó là của thị trường, không phải của chiến lược.

**Lân cận tham số** (chạy sau khi đã quyết định, không dùng để quyết định): dịch một bước mỗi tham số, trên mọi giai đoạn.

- Khoảng CAGR của 12 điểm lân cận (thấp nhất / trung vị / cao nhất):
  - TRAIN 14.9 / 18.8 / 20.6;
  - VALID 9.4 / 12.8 / 16.3;
  - TEST 4.6 / 7.7 / 14.9.
- Mọi MDD ở TEST nằm trong -17.8% tới -21.5%.
- Kết quả TEST của điểm đã chọn (8.1) nằm cạnh trung vị: không phải điểm may, cũng không phải điểm xui.

| biến thể | TRAIN CAGR | VALID CAGR | TEST CAGR | TRAIN MDD | VALID MDD | TEST MDD |
|---|---|---|---|---|---|---|
| người thắng (cap50,k10,M,buf1.5,sma200,W) | 18.1 | 13.9 | 8.1 | -25.9 | -12.0 | -20.8 |
| uni=cap30 | 15.3 | 9.4 | 11.2 | -28.1 | -11.4 | -17.8 |
| uni=cap70 | 20.3 | 11.0 | 10.0 | -24.8 | -10.8 | -18.1 |
| k=5 | 20.3 | 10.7 | 14.9 | -23.4 | -14.8 | -21.5 |
| k=15 | 18.9 | 12.2 | 4.9 | -28.0 | -13.2 | -20.5 |
| rebalance=Q | 17.8 | 14.9 | 4.6 | -28.2 | -11.3 | -21.3 |
| buffer=1.0 | 20.4 | 12.9 | 11.2 | -25.1 | -13.3 | -18.2 |
| buffer=2.0 | 18.6 | 16.3 | 6.6 | -24.3 | -11.3 | -20.4 |
| sma150 | 19.1 | 14.3 | 7.4 | -24.8 | -13.8 | -18.5 |
| sma250 | 15.8 | 11.6 | 7.7 | -32.9 | -16.6 | -19.9 |
| check=1 sessions | 15.3 | 14.4 | 5.3 | -28.5 | -12.0 | -21.2 |
| check=10 sessions | 20.6 | 12.7 | 7.8 | -22.4 | -14.2 | -19.7 |
| signal src=VN30 | 14.9 | 13.2 | 8.1 | -33.4 | -13.8 | -19.9 |
| no trend filter | 25.2 | 8.3 | 15.5 | -32.3 | -32.9 | -17.3 |

**Bộ lọc xu hướng tốn lợi nhuận.**

- Không có bộ lọc, cùng rổ này được 15.5% ở TEST (so với 8.1%), 21.4% so với 17.6% cho 2013-2023 và 20.1% so với 15.6% cho 2013-2026.
- Đổi lại MDD sâu hơn: -33.1% so với -25.9%. Sharpe gần như nhau: 1.18 so với 1.11 cho 2013-2026.
- Ở VALID, bản không lọc chỉ được 8.3% với MDD khoảng -33%, trượt cửa 2 so với luật repo trên cap50 (13.4). Vì vậy nhóm nghiên cứu giữ bộ lọc.
- Bỏ bộ lọc bây giờ, dựa vào kết quả TEST, là "nhìn trộm TEST". Đó là câu hỏi cho một đợt nghiên cứu mới.

## 6. Quyết định và giới hạn của nó

**Đọc kỹ trước khi dùng tiền thật.**

1. **Nó quản lý rủi ro nhiều hơn là tăng lợi nhuận.** So với luật động lượng của repo trên chính rổ của nó, nó chỉ hơn +0.5 điểm CAGR ở VALID và +0.6 ở TEST.
   - Sụt giảm chỉ khoảng một nửa: -12.0 so với -47.5 ở VALID, -20.8 so với -41.3 ở TEST.
   - Biến động khoảng 13-17%/năm, so với 22-27%.
   - Vòng quay vốn 2-4 lần/năm, so với 7-9.
   - Chênh lệch lợi nhuận ngày so với baseline đó không có ý nghĩa thống kê: t = -0.18 ở VALID và -0.06 ở TEST. Toàn bộ phần CAGR hơn đến từ biến động thấp hơn.
2. **Nó thua thị trường trong đợt tăng 2024-2026.**
   - TEST CAGR 8.1%, so với 15.9% của VNINDEX mua và giữ và 19.9% của rổ cap30 chia đều không lọc.
   - Năm 2025 nó được +21.5% trong khi VNINDEX +40.9%.
   - Trên cả kỳ 2013-2026 nó vẫn hơn VNINDEX: 15.6% so với 10.7%, với MDD -25.9% so với -45.3%. VNINDEX không tính cổ tức nên khoảng cách thật nhỏ hơn khoảng 1.5-2 điểm/năm.
3. **So với bot cũ, nó thắng dài hạn nhưng thua ở cửa sổ TEST.** Bot cũ xếp hạng rổ VN30; bản thay thế point-in-time gần nhất là luật động lượng trên cap30.
   - Người thắng hơn +13.9 điểm/năm ở TRAIN, +10.9 ở VALID, và 10.9 điểm/năm (15.6 so với 4.7) trên 2013-2026.
   - Nhưng **kém 3.7 điểm/năm ở TEST** (8.1 so với 11.8). Chênh lệch này không có ý nghĩa thống kê (t = -0.39).
   - Nếu đòi "phải thắng rổ VN30 point-in-time ở TEST" thì **không ứng viên nào đạt**. CAGR TEST tốt nhất trong các ứng viên là 9.0% của MC_C2, so với 11.8%; khi đó bot cũ sẽ được giữ. Báo cáo nêu điều này vì đó là ranh giới trung thực của kết quả.
4. **Kết quả trên rổ VN30 hôm nay không phải thước đo công bằng.** Luật động lượng trên VN30 hôm nay được 26.2%/năm ở TEST, nhưng con số đó có nhìn trước.
   - Hai trong năm khoản lãi lớn nhất đến từ MCH (+159 triệu đồng, thứ 2) và BSR (+116 triệu đồng, thứ 5). Cả hai còn ở **UPCoM** khi được mua, và chỉ có trong danh sách vì sau này vào VN30.
   - Cùng luật đó trên rổ point-in-time được 0.1% (liq_top30), 2.2% (liq_top100), 7.5% (cap50) và 11.8% (cap30) mỗi năm.
5. **Trạng thái ngày 14/10/2026: toàn tiền mặt.**
   - VNINDEX đóng cửa 1,704.9, dưới SMA200 1,797.9.
   - Rổ đứng ngoài từ lần xét tuần ngày 2026-09-25, và trước đó từ cuối tháng 7 tới cuối tháng 8.
   - Rổ đang trong đợt sụt -20.8% bắt đầu 2026-02-03; đáy tới nay là 2026-09-11. Các mã của nó giảm khoảng 18% từ tháng 2 tới tháng 7/2026, trong khi VNINDEX giảm khoảng 5%.
6. **Tiền nhàn rỗi.** Backtest tính lãi 0% cho tiền mặt. Lãi tiền gửi hoặc quỹ thị trường tiền tệ 4% trên 20-45% thời gian đứng ngoài cộng thêm khoảng 0.9-2.1 điểm/năm: TRAIN 19.7, VALID 15.9, TEST 8.9, 2013-2026 17.1 (CAGR %).

**Á quân:** `PB_C2`, gồm 25% động lượng chia nhánh và 75% chính rổ này.

- Lợi nhuận như nhau: CAGR 2013-2026 15.5% so với 15.6%.
- MDD ở TEST nhỏ hơn: -16.8% so với -20.8%.
- Nhưng cần 4 sổ con thay vì 1, và có hơn gấp đôi số tham số.

**Các giới hạn khác:**

- **Sức mạnh thống kê.** Cửa sổ TEST dài 2.8 năm; sai số chuẩn của Sharpe trên cửa sổ đó khoảng 0.6. Không ứng viên nào chứng minh được lợi thế thống kê so với chỉ số thụ động.
  - Với người thắng trên 2013-2026, t-statistic của lợi nhuận ngày là +0.96 so với VNINDEX và +0.23 so với rổ cap30 chia đều không lọc.
  - Điều vững là mức giảm sụt giảm: khoảng -26% so với -43 tới -49% của mua và giữ và của luật repo.
- **VALID một phần là trong mẫu** với mọi nhóm, nên TEST là phép thử sạch duy nhất. Các nhóm đã chạy 156 tới 1,120 lần mỗi nhóm trên TRAIN và VALID, và không nhóm nào chạy TEST.
- **cap50 là bản xấp xỉ** thành phần chỉ số thật. Nó dựa vào số cổ phiếu (`Shares`) của VCI, và 7 mã đã huỷ niêm yết thiếu dữ liệu này:
  - GTN (67 tháng trong liq_top100), BHS (27), KTB (13), NHS (12), BCI (7), SEC (3), PTK (2);
  - mỗi tháng thiếu nhiều nhất 4 mã; chỉ GTN và có thể BCI từng gần ngưỡng cap50;
  - đây là thiên lệch lạc quan nhỏ, không quyết định.
- **Khớp lệnh lý tưởng hoá.** Mọi lệnh khớp ở giá mở cửa cộng trượt giá; lệnh ATO thật có thể khác. Ở 1e9 đồng, giới hạn 10% ADV làm người thắng mất nhiều nhất 0.5 điểm (TRAIN) và không mất gì ở TEST.
- **Các giới hạn chung của bộ khung:**
  - Giá điều chỉnh tới hôm nay, nên lô và bước giá tính trên giá đã điều chỉnh. Trước khoảng 2017, lô rẻ hơn thật và làm tròn trượt giá thô hơn (tới 1% mỗi chiều với mã bị điều chỉnh nhiều, tức là bi quan). Giá TEST gần giá thật nhất.
  - Tiền mặt không có lãi.
  - VNINDEX là chỉ số giá, còn giá cổ phiếu đã gồm cổ tức.
  - Cổ tức tiền mặt coi như tái đầu tư trước thuế 5%, lợi khoảng 0.1%/năm.
  - Sáp nhập làm vị thế cũ kẹt ở giá cuối: NHS và BHS ở TRAIN.

**Theo năm** (một lần chạy liên tục 2013-2026; 2026 tính tới 2026-10-14):

| năm | TI_lvhi52 | PB_C2 | PB_C1 | TI_hi52 | MC_C1 | B_repo_cap50 | B_repo_cap30 | B_repo_liq30 | B_repo_vn30today | B_cap30_ew | VNINDEX |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2013 | 43.4 | 42.3 | 41.9 | 67.8 | 39.1 | 39.9 | 21.4 | 17.5 | 8.8 | 27.7 | 20.6 |
| 2014 | 22.7 | 20.5 | 18.4 | 32.0 | 15.7 | 13.1 | -10.2 | -10.6 | -9.3 | 15.6 | 8.1 |
| 2015 | 11.9 | 10.3 | 8.6 | 16.6 | 1.0 | 16.3 | 5.2 | -0.6 | -3.6 | 1.6 | 6.1 |
| 2016 | -0.1 | 1.7 | 3.6 | 14.1 | 8.8 | -26.0 | -6.3 | -17.9 | 19.1 | 17.9 | 14.8 |
| 2017 | 68.7 | 61.4 | 53.8 | 66.8 | 34.0 | 54.6 | 38.5 | 51.5 | 72.4 | 44.2 | 48.0 |
| 2018 | -3.3 | -2.8 | -2.0 | 0.4 | -5.2 | -9.5 | -17.9 | -18.9 | 5.6 | -8.2 | -9.3 |
| 2019 | 2.7 | 1.9 | 1.2 | 1.0 | -1.7 | -6.7 | -3.8 | -4.9 | -4.1 | 12.0 | 7.7 |
| 2020 | 15.1 | 22.2 | 30.3 | 21.2 | 47.5 | 27.0 | 18.8 | 27.0 | 15.7 | 17.9 | 14.9 |
| 2021 | 67.9 | 72.9 | 78.0 | 112.2 | 74.3 | 100.2 | 39.4 | 95.5 | 45.1 | 41.5 | 35.7 |
| 2022 | -4.9 | -8.4 | -12.0 | -20.7 | -18.0 | -25.6 | -30.4 | -27.0 | -29.2 | -33.4 | -32.8 |
| 2023 | -1.6 | 0.7 | 3.3 | -2.4 | 10.7 | 0.9 | 1.3 | 10.5 | 0.5 | 23.1 | 12.2 |
| 2024 | 8.1 | 9.3 | 10.7 | 7.0 | 11.0 | 5.5 | 20.1 | -10.3 | 53.0 | 28.5 | 12.1 |
| 2025 | 21.5 | 20.6 | 20.8 | 34.1 | 24.2 | 26.9 | 12.9 | 20.8 | 24.1 | 28.9 | 40.9 |
| 2026 | -5.6 | -7.8 | -10.9 | -21.1 | -14.8 | -8.9 | 0.7 | -7.1 | 1.2 | -4.9 | -4.5 |

Người thắng hơn VNINDEX trong 8/14 năm.

- Tốt hơn hẳn ở các năm giảm: 2018 -3.3 so với -9.3; 2022 -4.9 so với -32.8.
- Tụt lại trong các đợt tăng rộng và khi hồi phục từ tiền mặt: 2016, 2019, 2023, 2024, 2025.
- 2026 tới nay -5.6 so với -4.5.

**Đợt sụt giảm:**

| lần chạy | đỉnh | đáy | độ sâu | hồi phục |
|---|---|---|---|---|
| TRAIN (cũng là đợt tệ nhất của lần chạy liên tục) | 2018-04-05 | 2020-08-31 | -25.9% | 2021-01-07 |
| VALID | 2021-11-12 | 2022-03-14 | -12.0% | chưa hồi phục trong VALID |
| TEST | 2026-02-03 | 2026-09-11 | -20.8% | chưa hồi phục (còn mở ngày 2026-10-14) |

## Chạy lại bằng bộ khung trong repo

Mã nguồn của 13 ứng viên còn lại và các script của người kiểm định (`registry.py`, `protocol.py`, `agg.py`…) không được đưa vào repo. Repo chỉ giữ bộ khung, các baseline (`momentum`, `ew`, `vnindex`, `quant`) và người thắng.

Người thắng chạy qua **module thật của bot** (`python -m research run factor`), không qua bản nghiên cứu `ti.py` (tệp của nhóm trend-index, cũng không có trong repo). Module của bot chỉ khác bản nghiên cứu một điểm: khi hai mã bằng điểm, mã ít biến động hơn được xếp trước (bản nghiên cứu để pandas tự xếp). Vì vậy kết quả chạy lại hơi khác bảng trên.

| `python -m research run factor` | CAGR % | MDD % | Sharpe |
|---|---|---|---|
| `--split TRAIN` | 17.65 | -26.40 | 1.34 |
| `--split VALID` | 13.86 | -11.99 | 0.99 |
| `--split TEST --unlock-test` | 8.35 | -20.24 | 0.57 |
| `--split ALL --unlock-test` | 15.29 | -26.40 | 1.10 |

`python -m research run momentum --split VALID --universe liq_top30` cho 13.57 / -48.82, đúng như `B_repo_liq30` ở trên.

TEST của đợt này đã được dùng. Từ nay, một đợt nghiên cứu mới cần dữ liệu sau 14/10/2026 làm TEST mới (xem [README.md](README.md#giai-đoạn-và-khoá-test)).
