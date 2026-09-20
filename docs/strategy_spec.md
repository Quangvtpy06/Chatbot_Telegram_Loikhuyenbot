# Strategy Specification: quality_trend_v1 (Version 1.0.0)

## 1. Tổng quan chiến lược
- **Tên chiến lược:** quality_trend_v1
- **Mục tiêu:** Kết hợp giữa chọn lọc cổ phiếu cơ bản tốt (Quality), xu hướng kỹ thuật (Trend) và dòng tiền lớn từ khối ngoại (Foreign Flow) để sinh tín hiệu giao dịch qua Telegram Bot.
- **Khung thời gian:** End-of-day / Swing Trading (Giao dịch theo ngày/trung hạn).

## 2. Nguyên tắc bắt buộc về dữ liệu (Data Gate)
- Hệ thống chỉ xử lý tín hiệu khi trạng thái dữ liệu đạt yêu cầu:
  - `data_status == "OK"`[cite: 1]
  - `signal_status == "ELIGIBLE"`[cite: 1]
- **Quy tắc chặn:** Nếu `data_status == "DATA WARNING"` hoặc dữ liệu bị thiếu/lỗi, hệ thống tuyệt đối **không được phép** phát sinh tín hiệu BUY/SELL mà phải trả về `NO SIGNAL` kèm lý do cụ thể[cite: 1].

## 3. Điều kiện định lượng sinh tín hiệu BUY
Một mã cổ phiếu được kích hoạt hành động **BUY** khi đồng thời thỏa mãn các điều kiện sau:
1. **Chất lượng doanh nghiệp (Fundamental):**
   - ROE quý gần nhất $\ge 15\%$ (`roe_quarter >= 0.15`)[cite: 1].
   - Tỷ lệ nợ vay trên vốn chủ sở hữu ở mức an toàn (`debt_to_equity_quarter <= 1.0`).
2. **Xu hướng kỹ thuật (Technical & Volume):**
   - Giá đóng cửa hiện tại nằm trên đường trung bình động: `latest_close > sma_20` và `sma_20 > sma_50`[cite: 1].
   - Chỉ báo động lượng RSI (14 phiên) nằm trong vùng tích cực, chưa quá mua: $50 \le \text{RSI}_{14} \le 70$[cite: 1].
   - Đường MACD nằm trên đường tín hiệu (`macd > macd_signal`)[cite: 1].
3. **Dòng tiền khối ngoại (Foreign Flow):**
   - Khối ngoại mua ròng tối thiểu 3 trong 5 phiên gần nhất ($\ge 3/5$ phiên).
   - Tổng khối lượng/giá trị mua ròng cộng dồn trong 5 phiên gần nhất mang giá trị dương (` foreign_net_ volume > 0`).

## 4. Quản trị rủi ro kèm theo tín hiệu BUY (Risk Management)
Mỗi tín hiệu `BUY` phải tính toán sẵn các thông số quản trị rủi ro trước khi chuyển qua Risk Gate:
- **Reference Price (Giá tham chiếu):** Giá đóng cửa mới nhất (`latest_close`)[cite: 1].
- **Stop Loss (Cắt lỗ):** Đặt tại mức $-5\%$ so với giá tham chiếu (`reference_price * 0.95`)[cite: 1].
- **Take Profit (Chốt lời):** Đặt tại mức $+10\%$ so với giá tham chiếu (`reference_price * 1.10`)[cite: 1].

## 5. Quy chuẩn Đầu ra (Output Schema)
- Khi đủ điều kiện BUY: Trả về action là `BUY`, kèm theo mức độ tự tin (`confidence`), giá tham chiếu, stop_loss, take_profit và danh sách `reasons`[cite: 1].
- Khi không đủ điều kiện hoặc dữ liệu lỗi: Trả về action là `NO SIGNAL` và ghi rõ lý do vào `reasons`[cite: 1]. Không được tự ý thay thế bằng `HOLD`[cite: 1].