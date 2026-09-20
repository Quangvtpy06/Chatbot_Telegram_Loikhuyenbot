# Strategy Specification: quality_trend_v1 (Version 1.5.0)

## 1. Tổng quan chiến lược
- **Tên chiến lược:** quality_trend_v1
- **Mục tiêu:** Xây dựng hệ thống sinh tín hiệu giao dịch tự động qua Telegram dựa trên việc kết hợp các lớp bộ lọc: Sức khỏe doanh nghiệp (Quality), Xu hướng kỹ thuật cá thể (Trend), Dòng tiền khối ngoại (Foreign Flow) và Trạng thái thị trường chung vĩ mô (Market Filter).
- **Khung thời gian:** End-of-day / Swing Trading (Giao dịch theo ngày / trung hạn).

## 2. Nguyên tắc bắt buộc về dữ liệu (Data Gate)
- **Tách biệt độc lập trạng thái:** Phải tách rời hoàn toàn `data_status` (trạng thái dữ liệu hệ thống) và `signal_status` (trạng thái sinh tín hiệu).
- **Quy tắc chặn tuyệt đối:** Nếu dữ liệu gặp lỗi hoặc thiếu, hệ thống bắt buộc phải trả về `NO SIGNAL` kèm theo lý do cụ thể. Tuyệt đối không tự ý thay thế `NO SIGNAL` bằng `HOLD`.

## 3. Điều kiện định lượng sinh tín hiệu Giao dịch (Signal Logic)

### A. Tín hiệu MUA (BUY)
Một mã cổ phiếu được kích hoạt hành động **BUY** khi đồng thời thỏa mãn các nhóm điều kiện sau:
- **Lớp 1 (Vĩ mô):** Chỉ số **VN-Index / VN30** duy trì trạng thái tích cực: Giá đóng cửa của chỉ số chung nằm trên đường trung bình động $\text{SMA}_{20}$ ($\text{Close}_{\text{Index}} > \text{SMA}_{20}$).
- **Lớp 2 (Cơ bản):** 
  - Đạt chuẩn ROE TTM từ $15\%$ trở lên (`roe_ttm >= 0.15`).
  - **Ngoại lệ ngành:** Tạm thời loại trừ nhóm ngành tài chính (Ngân hàng, Chứng khoán) ra khỏi điều kiện khắt khe về hệ số nợ $D/E \le 1.0$ chung do đặc thù đòn bẩy ngành.
- **Lớp 3 (Kỹ thuật):** Giá đóng cửa thỏa mãn `latest_close > sma_20` và `sma_20 > sma_50`; chỉ báo RSI(14) nằm trong vùng $50 \le \text{RSI}_{14} \le 70$; đường MACD nằm trên đường tín hiệu (`macd > macd_signal`).
- **Lớp 4 (Khối ngoại):** 
  - Tính toán chỉ số dòng tiền: $\text{foreign\_net\_volume} = \text{foreign\_buy\_volume} - \text{foreign\_sell\_volume}$.
  - Tổng khối lượng mua 5 phiên gần nhất lớn hơn tổng bán, `foreign_net_volume > 0` (dương) và khối ngoại mua ròng ít nhất $3/5$ phiên.

### B. Tín hiệu NẮM GIỮ (HOLD) & Cảnh báo kèm theo
Áp dụng cho các cổ phiếu đang có sẵn vị thế trong danh mục:
- **Điều kiện cốt lõi:** Chỉ áp dụng khi đang có vị thế và **chưa xuất hiện điều kiện SELL**. Không bắt buộc cứng nhắc chỉ báo RSI phải nằm trong khung $45–55$.
- **Cảnh báo xu hướng yếu:** Trường hợp giá thủng ngắn hạn ($\text{Close} < \text{SMA}_{20}$) nhưng xu hướng lớn chưa bị phá vỡ ($\text{SMA}_{20} > \text{SMA}_{50}$), hệ thống vẫn duy trì trạng thái **HOLD** đi kèm **cảnh báo xu hướng yếu**.
- **Cảnh báo dòng tiền ngoại:** Trường hợp khối ngoại chuyển sang trạng thái bán ròng nhưng chưa đủ cấu thành điều kiện SELL, hệ thống tiếp tục giữ trạng thái **HOLD** đi kèm **cảnh báo dòng tiền ngoại suy yếu**.

### C. Tín hiệu BÁN (SELL) & Thứ tự ưu tiên xử lý
Khi kiểm tra thoát lệnh hoặc chạy backtest, nếu có nhiều điều kiện xuất hiện cùng lúc, hệ thống bắt buộc phải xét duyệt tín hiệu **SELL** theo đúng thứ tự ưu tiên nghiêm ngặt sau đây:
1. **Stop Loss (Cắt lỗ):** Giá chạm hoặc vượt ngưỡng $-5\%$ so với giá tham chiếu. (*Lưu ý khi backtest: Sử dụng giá Low/High trong phiên để kiểm tra; nếu trong cùng một phiên giá vừa chạm Stop Loss vừa chạm Take Profit, bắt buộc phải ưu tiên kích hoạt Stop Loss trước*).
2. **Take Profit (Chốt lời):** Giá đạt hoặc vượt mức $+10\%$ so với giá tham chiếu (dựa trên mức giá High trong phiên).
3. **Trend Exit (Gãy xu hướng kỹ thuật):** Đường $\text{sma\_20}$ cắt xuống dưới đường $\text{sma\_50}$ ($\text{sma\_20} < \text{sma\_50}$).
4. **Market Exit (Gãy vĩ mô):** Chỉ số VN-Index thủng hỗ trợ kỹ thuật ($\text{Close}_{\text{Index}} \le \text{SMA}_{20}$).
5. **Foreign Net Sell (Khối ngoại bán mạnh):** Tổng khối lượng bán 5 phiên lớn hơn tổng mua, `foreign_net_volume < 0` (âm) và có từ $3/5$ phiên trở lên ở trạng thái bán ròng.

## 4. Công thức tính điểm độ tự tin (Confidence Score)
Đối với tín hiệu `BUY`, điểm số độ tự tin $S$ được tính theo công thức trọng số kết hợp 4 thành phần:
$$S = 100 \times (0.20M + 0.25F + 0.35T + 0.20FF)$$

Trong đó:
- $M$: Market Score (Điểm vĩ mô VN-Index).
- $F$: Fundamental Score (Điểm cơ bản doanh nghiệp - ROE, D/E).
- $T$: Technical Score (Điểm kỹ thuật - SMA, RSI, MACD).
- $FF$: Foreign Flow Score (Điểm dòng tiền khối ngoại).
*Mỗi thành phần được chuẩn hóa về khoảng $[0, 1]$ trước khi đưa vào tính toán.*

## 5. Quy chuẩn Đầu ra (Output Schema)
- **`BUY`**: Thỏa mãn toàn bộ điều kiện mở vị thế mới (kèm `reference_price`, `stop_loss`, `take_profit`, `confidence` tính theo công thức trên, và danh sách `reasons`).
- **`HOLD`**: Tiếp tục nắm giữ danh mục hiện tại, có kèm theo các cảnh báo phụ (xu hướng yếu, ngoại bán ròng nhẹ) nếu phát sinh.
- **`SELL`**: Kích hoạt bán theo đúng thứ tự ưu tiên (Stop Loss → Take Profit → Trend Exit → Market Exit → Foreign Net Sell).
- **`NO SIGNAL`**: Không đủ điều kiện giao dịch hoặc dữ liệu đầu vào gặp lỗi (`DATA WARNING`).