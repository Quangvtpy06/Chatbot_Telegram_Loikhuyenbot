# Hệ thống tín hiệu chứng khoán Việt Nam qua Telegram

Tài liệu này giúp thành viên mới hiểu phần đã hoàn thành, phần cần triển khai tiếp và cách chia việc song song. Mục tiêu của dự án là thu thập dữ liệu thị trường Việt Nam, kiểm soát chất lượng, áp dụng chiến lược đầu tư, quản trị rủi ro và gửi tín hiệu qua Telegram.

## 1. Nguyên tắc bắt buộc

> Dữ liệu không đủ tin cậy thì hệ thống không được phép tạo BUY/SELL.

Luồng xử lý mục tiêu:

```text
Nguồn dữ liệu
    ↓
Thu thập dữ liệu
    ↓
Kiểm tra dữ liệu thô
    ↓
Làm sạch và chuẩn hóa
    ↓
Đối chiếu nguồn chính/dự phòng
    ↓
Kiểm tra lại theo từng mã
    ↓
Phân tích cơ bản và kỹ thuật
    ↓
Signal Engine
    ↓
Risk Gate
    ↓
Lưu sự kiện tín hiệu
    ↓
Telegram Bot
```

Các trạng thái dữ liệu và tín hiệu phải được giữ nguyên xuyên suốt hệ thống:

```text
data_status   = OK | DATA WARNING
signal_status = ELIGIBLE | NO SIGNAL
action        = BUY | SELL | HOLD | NO SIGNAL
```

Chỉ bản ghi có `data_status = OK` và `signal_status = ELIGIBLE` mới được chuyển tới bộ sinh tín hiệu.

## 2. Phần đã hoàn thành

### Thu thập dữ liệu

File: `collectors_processing/dnse_api_crawl.py`

Crawler hiện hỗ trợ:

- Danh sách cổ phiếu HOSE, HNX và UPCOM.
- Giá OHLCV lịch sử từ DNSE.
- Snapshot giao dịch realtime từ DNSE.
- Báo cáo tài chính quý/năm từ VCI, dự phòng KBS.
- Dữ liệu mua/bán và mua ròng/bán ròng của khối ngoại từ KBS.
- Retry khi API lỗi tạm thời.
- Chuyển sang nguồn báo cáo tài chính dự phòng khi nguồn chính lỗi hoặc không có dữ liệu.

Schema realtime chuẩn:

```text
symbol
timestamp
open
high
low
close
volume
collected_at
```

Schema báo cáo tài chính tối thiểu:

```text
symbol
report_period
published_date
revenue
net_profit
roe
debt_to_equity
collected_at
```

`published_date` có thể null khi provider không cung cấp ngày công bố thật. Không được tự lấy ngày cuối quý hoặc tự tạo một ngày thay thế.

Schema giao dịch khối ngoại:

```text
symbol
timestamp
foreign_buy_volume
foreign_sell_volume
foreign_net_volume
foreign_net_buy_volume
foreign_net_sell_volume
source
collected_at
```

### Xử lý và phân tích

File: `collectors_processing/analytics.py`

Pipeline hiện có:

- Phát hiện null/NaN, sai định dạng, dữ liệu bất thường và dữ liệu trùng.
- Kiểm tra dữ liệu giá, realtime và báo cáo tài chính quá cũ.
- Ghi riêng các bản ghi bị loại.
- Đối chiếu dữ liệu tài chính giữa các nguồn.
- Chỉ điền trường thiếu bằng dữ liệu thật của nguồn dự phòng cùng kỳ.
- Chặn tín hiệu khi nguồn mâu thuẫn hoặc dữ liệu chưa đủ.
- Lưu CSV, báo cáo chất lượng JSON và SQLite.
- Tạo bảng tổng hợp theo từng mã.

Các chỉ báo kỹ thuật đã có:

- SMA 10, 20, 50 và 200.
- EMA 12, 20, 26 và 50.
- RSI 14.
- MACD 12/26, Signal 9 và Histogram.
- Bollinger Bands 20 phiên.
- ATR 14 và ADX 14.
- Stochastic %K 14 và %D 3.
- OBV.
- Lợi suất 1, 5 và 20 phiên.
- Biến động, khối lượng trung bình, tỷ lệ khối lượng và khoảng cách tới đỉnh 52 tuần.

Crawler và analytics dùng chung một thư mục gốc `data`; không tạo thêm `data/dnse`:

```text
data/
├── history/
├── realtime/
├── fundamental/
├── foreign/
└── analytics/
    ├── clean/
    ├── rejected/
    ├── analysis/
    │   ├── data_quality.csv
    │   ├── price_metrics.csv
    │   ├── source_audit.csv
    │   ├── stock_snapshot.csv
    │   └── screening_ranked.csv
    ├── market_analytics.sqlite
    └── quality_report.json
```

## 3. Cách chạy hiện tại

Trước khi chạy, kiểm tra thư mục hiện hành:

```powershell
Get-Location
```

Hai file Python thực tế nằm tại:

```text
dnse/collectors_processing/dnse_api_crawl.py
dnse/collectors_processing/analytics.py
```

`dnse_api_crawl.py` dùng để gọi API và thu thập dữ liệu. `analytics.py` chỉ xử lý dữ liệu đã thu thập; chạy `analytics.py` không crawl FPT từ API.

### Cách A: terminal đang ở thư mục bên ngoài

Nếu `Get-Location` trả về:

```text
D:\Folder1\Folder2
```

thì đường dẫn lệnh phải bắt đầu bằng `dnse/collectors_processing`.

Crawl riêng FPT:

```powershell
python .\dnse\collectors_processing\dnse_api_crawl.py `
  --mode all `
  --symbols FPT `
  --start 2024-01-01 `
  --output-dir .\dnse\data
```

Phân tích FPT sau khi crawl xong:

```powershell
python .\dnse\collectors_processing\analytics.py `
  --input-dir .\dnse\data `
  --output-dir .\dnse\data\analytics `
  --expected-symbols FPT
```

### Cách B: chuyển terminal vào thư mục `dnse`

Từ thư mục `code python`, chạy:

```powershell
Set-Location .\dnse
```

Sau đó mới dùng đường dẫn ngắn:

```powershell
python .\collectors_processing\dnse_api_crawl.py `
  --mode all `
  --symbols FPT `
  --start 2024-01-01 `
  --output-dir .\data

python .\collectors_processing\analytics.py `
  --input-dir .\data `
  --output-dir .\data\analytics `
  --expected-symbols FPT
```

Dấu backtick `` ` `` của PowerShell chỉ đặt ở cuối dòng khi lệnh còn tiếp tục ở dòng sau. Không đặt backtick ở dòng cuối cùng.

Code luôn quy đường dẫn tương đối về thư mục gốc `dnse`. Vì vậy `data`, `dnse/data` hoặc đường dẫn tuyệt đối vô tình chứa `dnse/dnse/data` đều được chuẩn hóa về đúng `dnse/data`.

### Chuẩn bị môi trường

Tạo virtual environment riêng và cài các thư viện mà project đang sử dụng:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install dnse python-dotenv pandas numpy vnstock
```

File `.env` nằm trong thư mục `dnse` và không được commit:

```env
DNSE_BASE_URL=https://openapi.dnse.com.vn
DNSE_API_KEY=...
DNSE_API_SECRET=...
DNSE_TIMEOUT=20
DNSE_MAX_RETRIES=3
DNSE_RETRY_SLEEP=1.5
DNSE_USE_SYSTEM_PROXY=false

TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_IDS=
TELEGRAM_MODE=polling
```

Không in token, API key hoặc API secret vào log, ảnh chụp màn hình, issue hay pull request.

### Crawl thử một số mã khi terminal đã ở `dnse`

```powershell
python .\collectors_processing\dnse_api_crawl.py `
  --mode all `
  --symbols FPT,VCB,HPG `
  --start 2024-01-01 `
  --output-dir data
```

Chạy toàn bộ thị trường có thể mất nhiều giờ. Khi phát triển, luôn dùng `--symbols` hoặc `--max-symbols`.

### Chạy analytics khi terminal đã ở `dnse`

```powershell
python .\collectors_processing\analytics.py `
  --input-dir data `
  --output-dir data/analytics `
  --expected-symbols FPT,VCB,HPG
```

Trước khi dùng kết quả, kiểm tra:

```text
data/analytics/quality_report.json
data/analytics/analysis/data_quality.csv
```

Không đọc `screening_ranked.csv` như tín hiệu mua bán nếu `signal_status = NO SIGNAL`.

## 4. Những phần chưa có

Project chưa có các thành phần sau:

1. Quy tắc đầu tư được duyệt và quản lý phiên bản.
2. Signal Engine sinh BUY/SELL/HOLD.
3. Risk Gate kiểm soát rủi ro và vị thế.
4. Backtest chống look-ahead bias và tính phí giao dịch.
5. Cơ sở dữ liệu subscriber, watchlist, vị thế và lịch sử tín hiệu.
6. Telegram Bot và định dạng thông báo.
7. Scheduler điều phối crawler, analytics và signal engine.
8. Cơ chế chống gửi trùng và chống spam.
9. Monitoring, heartbeat và cảnh báo API lỗi.
10. Docker/VPS hoặc Windows Service cho môi trường chạy liên tục.
11. Dữ liệu VNINDEX/VN30, sự kiện doanh nghiệp và giá điều chỉnh.
12. Test tự động và dữ liệu fixture không phụ thuộc API thật.

## 5. Cấu trúc thư mục mục tiêu

```text
dnse/
├── collectors_processing/
│   ├── dnse_api_crawl.py
│   └── analytics.py
├── signals/
│   ├── models.py
│   ├── signal_engine.py
│   ├── strategies.py
│   └── risk_manager.py
├── backtesting/
│   ├── engine.py
│   ├── metrics.py
│   └── reports.py
├── telegram_bot/
│   ├── bot.py
│   ├── handlers.py
│   ├── formatter.py
│   └── subscriber_repository.py
├── orchestration/
│   ├── scheduler.py
│   └── health_check.py
├── storage/
│   ├── migrations/
│   └── repositories.py
├── tests/
│   ├── fixtures/
│   ├── test_signal_engine.py
│   ├── test_risk_manager.py
│   └── test_telegram_formatter.py
├── docs/
│   ├── strategy_spec.md
│   ├── data_contracts.md
│   └── operations.md
├── .env
├── .env.example
├── requirements.txt
└── README.md
```

## 6. Thứ tự tích hợp bắt buộc

```text
1. Chốt Strategy Specification
2. Hoàn thành data contracts và database schema
3. Viết Signal Engine
4. Viết Risk Gate
5. Backtest và duyệt ngưỡng
6. Lưu signal events và chống trùng
7. Nối Telegram Bot bằng tín hiệu giả lập
8. Nối Telegram Bot với tín hiệu thật
9. Thêm scheduler và health check
10. Deploy môi trường chạy liên tục
```

Telegram Bot không được tự tính chỉ báo hoặc tự quyết định BUY/SELL. Bot chỉ đọc sự kiện đã vượt qua Data Quality Gate và Risk Gate.

## 7. Phân chia công việc song song

Các nhóm dưới đây có thể bắt đầu cùng lúc. Mỗi nhóm chỉ sửa phạm vi file của mình để giảm xung đột merge.

| Nhóm | Việc thực hiện ngay | Phạm vi file đề xuất | Kết quả bàn giao | Phụ thuộc |
|---|---|---|---|---|
| A. Quant/Chiến lược | Viết triết lý đầu tư thành điều kiện định lượng; xác định BUY/SELL/HOLD, timeframe và ngưỡng | `docs/strategy_spec.md` | Đặc tả có công thức, dữ liệu đầu vào, lý do BUY/SELL và ví dụ | Không |
| B. Data | Bổ sung VNINDEX/VN30, giá điều chỉnh, corporate actions, ngày công bố BCTC và lịch sử khối ngoại | `collectors_processing/`, `docs/data_contracts.md` | Dataset có provenance, timestamp và kiểm tra độ mới | Không |
| C. Signal Backend | Tạo model tín hiệu, interface strategy và Signal Engine dùng dữ liệu giả | `signals/models.py`, `signals/signal_engine.py`, `signals/strategies.py` | Sinh BUY/SELL/HOLD/NO SIGNAL có reasons | Chưa cần ngưỡng cuối; dùng config tạm có ghi chú |
| D. Risk | Xây position sizing, stop loss, take profit, cooldown và cổng chặn | `signals/risk_manager.py` | Quyết định ALLOW/BLOCK cùng lý do | Có thể dùng tín hiệu giả |
| E. Backtest/QA | Tạo fixture 250–500 phiên, khung backtest, metric và test data leakage | `backtesting/`, `tests/` | Báo cáo return, drawdown, win rate, expectancy | Có thể dùng strategy mẫu |
| F. Telegram | Tạo bot polling, `/start`, `/help`, `/status`, `/signal`, watchlist và formatter | `telegram_bot/` | Bot nhận lệnh và gửi tín hiệu giả; chưa nối BUY/SELL thật | Token và dữ liệu mock |
| G. Storage | Thiết kế schema và migration cho subscriber, watchlist, position, signal event, notification | `storage/` | Migration chạy lặp lại an toàn và repository CRUD | Không |
| H. DevOps | Tạo `.env.example`, dependency lock, Docker, scheduler mẫu, log rotation và health check | File cấu hình, `orchestration/`, `docs/operations.md` | Có lệnh chạy local và production rõ ràng | Không |
| I. Product/Nội dung | Viết mẫu tin Telegram, luồng onboarding, giải thích tín hiệu và cảnh báo dữ liệu | `docs/`, fixture message | Bộ message được duyệt, dễ đọc trên điện thoại | Không |

### Việc không cần chờ code

- Chốt đối tượng người dùng: cá nhân, nhóm kín hay kênh công khai.
- Chốt khung đầu tư: intraday, swing, trung hạn hay nhiều chiến lược tách biệt.
- Định nghĩa thế nào là một tín hiệu thành công/thất bại.
- Chọn benchmark: VNINDEX, VN30 hoặc chỉ số ngành.
- Chốt mức stop loss, take profit, reward/risk và số vị thế tối đa.
- Tạo 20–30 tình huống mẫu: dữ liệu tốt, dữ liệu cũ, nguồn mâu thuẫn, API lỗi, thị trường giảm mạnh.
- Viết mẫu nội dung BUY, SELL, HOLD, DATA WARNING và NO SIGNAL.
- Xác định tần suất gửi và thời gian im lặng để tránh spam.
- Chuẩn bị VPS/domain/HTTPS nếu dự kiến dùng webhook.
- Kiểm tra điều khoản sử dụng và quyền phân phối lại dữ liệu của từng nguồn.
- Lập danh sách mã ưu tiên để thử nghiệm thay vì chạy toàn thị trường ngay.

## 8. Đặc tả Signal Engine tối thiểu

Đầu vào là một dòng từ `stock_snapshot` kết hợp `data_quality`:

```python
{
    "symbol": "FPT",
    "data_status": "OK",
    "signal_status": "ELIGIBLE",
    "latest_close": 112.5,
    "roe_quarter": 0.26,
    "pe_quarter": 12.2,
    "debt_to_equity_quarter": 0.79,
    "rsi_14": 61.0,
    "sma_20": 110.0,
    "sma_50": 105.0,
    "ema_12": 111.5,
    "ema_26": 108.0,
    "macd": 1.2,
    "macd_signal": 0.9,
    "adx_14": 28.0,
    "volume_ratio_20d": 1.5,
    "foreign_net_volume": 1810510
}
```

Đầu ra chuẩn:

```python
{
    "signal_id": "...",
    "symbol": "FPT",
    "strategy": "quality_trend_v1",
    "strategy_version": "1.0.0",
    "action": "BUY",
    "confidence": 78.5,
    "reference_price": 112.5,
    "stop_loss": 106.8,
    "take_profit": 124.0,
    "reasons": [
        "ROE đạt ngưỡng",
        "Giá trên SMA20 và SMA50",
        "MACD cao hơn Signal",
        "Khối ngoại mua ròng"
    ],
    "data_as_of": "...",
    "generated_at": "..."
}
```

Nếu dữ liệu không đạt:

```python
{
    "symbol": "FPT",
    "action": "NO SIGNAL",
    "reasons": ["Báo cáo tài chính thiếu published_date"]
}
```

Không được thay `NO SIGNAL` bằng HOLD. `HOLD` là một quyết định chiến lược trên dữ liệu hợp lệ; `NO SIGNAL` là hệ thống không đủ cơ sở để quyết định.

## 9. Risk Gate tối thiểu

Risk Gate phải chạy sau Signal Engine và trước Telegram:

- Chặn mọi tín hiệu không phải `ELIGIBLE`.
- Chặn BUY nếu thiếu stop loss.
- Chặn BUY nếu reward/risk dưới ngưỡng đã duyệt.
- Chặn BUY nếu vị thế dự kiến vượt giới hạn một mã hoặc một ngành.
- Chặn tín hiệu lặp lại trong thời gian cooldown.
- Chặn SELL nếu không có vị thế hoặc không có watchlist phù hợp với chính sách sản phẩm.
- Ghi lại đầy đủ quyết định `ALLOW/BLOCK` và lý do.

## 10. Database cần bổ sung

Tối thiểu cần các bảng:

```text
subscribers
watchlists
positions
strategy_runs
signal_events
risk_decisions
notifications
system_errors
```

`signal_events` cần khóa chống trùng theo ít nhất:

```text
symbol + strategy_version + action + signal_timestamp
```

`notifications` lưu Telegram message ID và trạng thái gửi để retry không tạo tin nhắn trùng.

## 11. Telegram Bot

Giai đoạn phát triển dùng polling. Production có thể chuyển sang webhook HTTPS. Không chạy polling và webhook cùng lúc.

Các lệnh phiên bản đầu:

```text
/start
/help
/status
/signal FPT
/watch FPT
/unwatch FPT
/watchlist
/market
```

Bot phải:

- Lưu `chat_id` sau `/start`.
- Chỉ phục vụ `TELEGRAM_ALLOWED_CHAT_IDS` trong giai đoạn thử nghiệm.
- Không hiển thị token trong log.
- Escape nội dung do người dùng nhập trước khi dùng HTML/Markdown.
- Giới hạn tần suất lệnh.
- Đọc signal event đã lưu; không tự chạy chiến lược trong handler.
- Hiển thị thời điểm dữ liệu, chiến lược, phiên bản, lý do và mức rủi ro.

## 12. Backtest và tiêu chí bật tín hiệu thật

Backtest phải xử lý:

- Look-ahead bias của báo cáo tài chính.
- Phí, thuế và trượt giá.
- Giá điều chỉnh và corporate actions.
- Thanh khoản tối thiểu.
- Giới hạn biên độ và tình huống không thể khớp.
- Survivorship bias của danh sách cổ phiếu.
- So sánh với benchmark.

Các metric tối thiểu:

```text
Total Return
CAGR
Maximum Drawdown
Win Rate
Expectancy
Profit Factor
Sharpe Ratio
Số giao dịch
Turnover
Hiệu quả theo từng trạng thái thị trường
```

Không nối BUY/SELL thật vào Telegram trước khi:

1. Test unit và integration đều đạt.
2. Backtest không có data leakage.
3. Risk Gate đã được duyệt.
4. Chống gửi trùng hoạt động.
5. DATA WARNING luôn dẫn tới NO SIGNAL.
6. Nhóm đã duyệt mẫu tin và ngưỡng chiến lược.

## 13. Quy tắc làm việc nhóm

- Một pull request chỉ nên xử lý một workstream.
- Không commit `.env`, token, API key, database thật hoặc dữ liệu người dùng.
- Mọi thay đổi schema phải cập nhật `docs/data_contracts.md` và migration.
- Mọi thay đổi chiến lược phải tăng `strategy_version`.
- Không thay đổi ngưỡng chiến lược âm thầm trong code; đưa vào config có tên rõ ràng.
- Test không gọi API thật; sử dụng fixture cố định.
- Log phải có `run_id`, `symbol`, `source`, `strategy_version` và timestamp.
- Các lỗi bị bỏ qua phải được ghi vào `system_errors` hoặc quality report.
- Người review phải kiểm tra đường đi `DATA WARNING → NO SIGNAL` trước khi merge.

## 14. Definition of Done cho phiên bản MVP

MVP được xem là hoàn thành khi:

- Crawler chạy định kỳ và lưu trạng thái từng nguồn.
- Analytics tạo được quality report và stock snapshot.
- Có ít nhất một chiến lược đã quản lý phiên bản.
- Có backtest tái lập được từ dữ liệu fixture.
- Signal Engine không phát tín hiệu từ dữ liệu lỗi.
- Risk Gate có kết quả ALLOW/BLOCK và lý do.
- Telegram Bot hỗ trợ subscriber và watchlist.
- Tín hiệu không bị gửi trùng khi process restart.
- Có `/status` báo độ mới dữ liệu và tình trạng API.
- Có log, health check và hướng dẫn khôi phục sự cố.
- Secrets chỉ nằm trong environment hoặc secret manager.

## 15. Công việc ưu tiên ngay bây giờ

Trong lần tích hợp tiếp theo, ưu tiên theo thứ tự:

1. Nhóm A hoàn thành `docs/strategy_spec.md` cho `quality_trend_v1`.
2. Nhóm G chốt schema `signal_events`, `risk_decisions`, `subscribers` và `notifications`.
3. Nhóm C tạo `signals/models.py` và Signal Engine chạy được với fixture.
4. Nhóm D tạo Risk Gate độc lập với Telegram.
5. Nhóm F tạo Telegram Bot bằng tín hiệu giả lập.
6. Nhóm E tạo backtest và bộ tình huống DATA WARNING/NO SIGNAL.
7. Chỉ sau khi các phần trên đạt tiêu chí mới nối toàn bộ pipeline.
