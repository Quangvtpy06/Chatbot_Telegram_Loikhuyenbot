# Strategy Specification: quality_trend_v1 (Version 1.2.0)

## 1. Tổng quan chiến lược
- **Tên chiến lược:** quality_trend_v1
- **Mục tiêu:** Xây dựng hệ thống sinh tín hiệu giao dịch tự động qua Telegram dựa trên việc kết hợp 4 lớp bộ lọc: Sức khỏe doanh nghiệp (Quality), Xu hướng kỹ thuật cá thể (Trend), Dòng tiền khối ngoại (Foreign Flow) và Trạng thái thị trường chung vĩ mô (Market Filter).
- **Khung thời gian:** End-of-day / Swing Trading (Giao dịch theo ngày / trung hạn).

## 2. Nguyên tắc bắt buộc về dữ liệu (Data Gate)
- Hệ thống chỉ xử lý tín hiệu khi trạng thái dữ liệu thỏa mãn:
  - `data_status == "OK"`[cite: 1]
  - `signal_status == "ELIGIBLE"`[cite: 1]
- **Quy tắc chặn tuyệt đối:** Nếu `data_status == "DATA WARNING"`[cite: 1] hoặc dữ liệu bị thiếu/lỗi, hệ thống không được phép phát sinh tín hiệu BUY/SELL/HOLD mà phải trả về `NO SIGNAL` kèm lý do cụ thể[cite: 1]. Tuyệt đối không thay thế `NO SIGNAL` bằng `HOLD`.

## 3. Điều kiện định lượng sinh tín hiệu Giao dịch (Signal Logic)

### A. Tín hiệu MUA (BUY)
Một mã cổ phiếu được kích hoạt hành động **BUY** khi đồng thời thỏa mãn các nhóm điều kiện sau:
- **Lớp 1 (Vĩ mô):** Chỉ số **VN-Index / VN30** phải duy trì trạng thái tích cực: Giá đóng cửa của chỉ số chung phải nằm trên đường trung bình động $\text{SMA}_{20}$ ($\text{Close}_{\text{Index}} > \text{SMA}_{20}$).
- **Lớp 2 (Cơ bản):** ROE quý gần nhất đạt từ $15\%$ trở lên (`roe_quarter >= 0.15`) và hệ số nợ vay trên vốn chủ sở hữu ở mức an toàn (`debt_to_equity_quarter <= 1.0`).
- **Lớp 3 (Kỹ thuật):** Giá đóng cửa thỏa mãn `latest_close > sma_20` và `sma_20 > sma_50`; chỉ báo RSI(14) nằm trong vùng $50 \le \text{RSI}_{14} \le 70$; đường MACD nằm trên đường tín hiệu (`macd > macd_signal`).
- **Lớp 4 (Khối ngoại):** Khối ngoại mua ròng tối thiểu 3 trong 5 phiên giao dịch gần nhất ($\ge 3/5$ phiên) và tổng giá trị mua ròng 5 phiên mang giá trị dương (`foreign_net_volume > 0`).

### B. Tín hiệu NẮM GIỮ (HOLD)
Áp dụng cho các cổ phiếu đang nằm trong danh mục nắm giữ sẵn:
- Giá hiện tại (`latest_close`) nằm trên đường trung bình ngắn hạn ($\text{sma\_20}$), đồng thời chưa chạm mốc Take Profit ($+10\%$) và chưa vi phạm mốc Stop Loss ($-5\%$).
- Chỉ báo RSI đi ngang trong vùng an toàn ($45 \le \text{RSI} \le 55$), không có tín hiệu đảo chiều mạnh hay dòng ngoại tháo chạy đột ngột.

### C. Tín hiệu BÁN (SELL)
Một mã cổ phiếu đang nắm giữ sẽ nhận được tín hiệu **SELL** ngay lập tức khi vi phạm các ngưỡng quản trị rủi ro hoặc gãy xu hướng:
- **Cắt lỗ (Stop Loss):** Giá giao dịch thấp hơn hoặc bằng mức giá tham chiếu trừ đi $5\%$ (`latest_close <= reference_price * 0.95`).
- **Chốt lời (Take Profit):** Giá giao dịch đạt hoặc vượt mức giá tham chiếu cộng thêm $10\%$ (`latest_close >= reference_price * 1.10`).
- **Gãy xu hướng:** Đường $\text{sma\_20}$ cắt xuống dưới đường $\text{sma\_50}$ ($\text{sma\_20} < \text{sma\_50}$), hoặc chỉ số VN-Index thủng ngưỡng hỗ trợ vĩ mô ($\text{Close}_{\text{Index}} \le \text{SMA}_{20}$).

## 4. Quản trị rủi ro kèm theo tín hiệu BUY (Risk Management)
Mỗi tín hiệu `BUY` phải tính toán sẵn các thông số quản trị rủi ro trước khi chuyển qua Risk Gate:
- **Reference Price (Giá tham chiếu):** Giá đóng cửa mới nhất (`latest_close`).
- **Stop Loss (Cắt lỗ):** Đặt tại mức $-5\%$ so với giá tham chiếu (`reference_price * 0.95`).
- **Take Profit (Chốt lời):** Đặt tại mức $+10\%$ so với giá tham chiếu (`reference_price * 1.10`).

## 5. Quy chuẩn Đầu ra (Output Schema)
- **`BUY`**: Thỏa mãn toàn bộ điều kiện mở vị thế mới (kèm confidence, stop_loss, take_profit, reasons).
- **`HOLD`**: Tiếp tục nắm giữ danh mục cũ, giá nằm trong vùng an toàn giữa ngưỡng cắt lỗ và chốt lời.
- **`SELL`**: Vi phạm điểm cắt lỗ, đạt điểm chốt lời, hoặc xu hướng kỹ thuật/vĩ mô đã đảo chiều xấu.
- **`NO SIGNAL`**: Không đủ điều kiện hoặc dữ liệu đầu vào gặp lỗi (`DATA WARNING`)[cite: 1].