# Strategy Specification: quality_trend_v1 (Version 1.1.0)

## 1. Tổng quan chiến lược
- **Tên chiến lược:** quality_trend_v1
- **Mục tiêu:** Xây dựng hệ thống sinh tín hiệu giao dịch tự động qua Telegram dựa trên việc kết hợp 4 lớp bộ lọc: Sức khỏe doanh nghiệp (Quality), Xu hướng kỹ thuật cá thể (Trend), Dòng tiền khối ngoại (Foreign Flow) và Trạng thái thị trường chung vĩ mô (Market Filter).
- **Khung thời gian:** End-of-day / Swing Trading (Giao dịch theo ngày / trung hạn).

## 2. Nguyên tắc bắt buộc về dữ liệu (Data Gate)
- Hệ thống chỉ xử lý tín hiệu khi trạng thái dữ liệu thỏa mãn:
  - `data_status == "OK"`
  - `signal_status == "ELIGIBLE"`
- **Quy tắc chặn tuyệt đối:** Nếu `data_status == "DATA WARNING"` hoặc dữ liệu bị thiếu/lỗi, hệ thống không được phép phát sinh tín hiệu BUY/SELL mà phải trả về `NO SIGNAL` kèm lý do cụ thể. Tuyệt đối không thay thế `NO SIGNAL` bằng `HOLD`.

## 3. Điều kiện định lượng sinh tín hiệu BUY
Một mã cổ phiếu được kích hoạt hành động **BUY** khi đồng thời thỏa mãn các nhóm điều kiện sau:

### Lớp 1: Bộ lọc Thị trường chung (Market Filter - Vĩ mô)
- Chỉ số **VN-Index / VN30** phải duy trì trạng thái tích cực: Giá đóng cửa của chỉ số chung phải nằm trên đường trung bình động $\text{SMA}_{20}$ ($\text{Close}_{\text{Index}} > \text{SMA}_{20}$) nhằm đảm bảo thị trường không nằm trong xu hướng giảm mạnh (Downtrend).

### Lớp 2: Chất lượng Doanh nghiệp (Fundamental Filter)
- ROE quý gần nhất đạt từ $15\%$ trở lên (`roe_quarter >= 0.15`).
- Hệ số nợ vay trên vốn chủ sở hữu ở mức an toàn (`debt_to_equity_quarter <= 1.0`).

### Lớp 3: Xu hướng Kỹ thuật & Động lượng (Technical Filter)
- Giá đóng cửa cổ phiếu thỏa mãn: `latest_close > sma_20` và `sma_20 > sma_50`.
- Chỉ báo sức mạnh tương đối RSI (14 phiên) nằm trong vùng an toàn, không quá mua: $50 \le \text{RSI}_{14} \le 70$.
- Đường MACD nằm trên đường tín hiệu (`macd > macd_signal`).

### Lớp 4: Dòng tiền Khối ngoại (Foreign Flow Filter)
- Khối ngoại mua ròng tối thiểu 3 trong 5 phiên giao dịch gần nhất ($\ge 3/5$ phiên).
- Tổng khối lượng/giá trị mua ròng cộng dồn trong 5 phiên gần nhất mang giá trị dương (`foreign_net_volume > 0`).

## 4. Quản trị rủi ro kèm theo tín hiệu BUY (Risk Management)
Mỗi tín hiệu `BUY` phải tính toán sẵn các thông số quản trị rủi ro trước khi chuyển qua Risk Gate:
- **Reference Price (Giá tham chiếu):** Giá đóng cửa mới nhất (`latest_close`).
- **Stop Loss (Cắt lỗ):** Đặt tại mức $-5\%$ so với giá tham chiếu (`reference_price * 0.95`).
- **Take Profit (Chốt lời):** Đặt tại mức $+10\%$ so với giá tham chiếu (`reference_price * 1.10`).

## 5. Quy chuẩn Đầu ra (Output Schema)
- Khi đủ điều kiện BUY: Trả về action là `BUY`, kèm theo độ tự tin (`confidence`), giá tham chiếu, stop_loss, take_profit và danh sách `reasons` chi tiết.
- Khi không đủ điều kiện hoặc dữ liệu lỗi: Trả về action là `NO SIGNAL` kèm theo danh sách lý do cụ thể.