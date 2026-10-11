# Hướng dẫn sử dụng Dataset Foundry Starter

Tài liệu này hướng dẫn đầy đủ cách cài đặt, cấu hình, thu thập, xử lý, duyệt và
đóng gói dữ liệu bằng Dataset Foundry Starter. Phần cuối giải thích luồng chạy
trong codebase để người vận hành và người phát triển có thể lần theo từng bước.

> Dataset Foundry là công cụ thu thập **candidate** đa nguồn, đa phương tiện.
> Tool không tự gán nhãn, không tự chia train/validation/test và không biến dữ
> liệu thu được thành dataset đã được phê duyệt.

## 1. Phạm vi và nguyên tắc vận hành

Tool phù hợp cho các nguồn đã được kiểm tra quyền sử dụng:

- thư mục cục bộ;
- tệp ZIP cục bộ;
- danh sách URL HTTPS trỏ thẳng tới tệp;
- repository dataset công khai trên Hugging Face;
- danh sách trang HTML đã được phê duyệt, có thể cần JavaScript.

Tool không phải crawler tự do. Nó không tự khám phá toàn bộ website, không đăng
nhập, không giải CAPTCHA, không xoay proxy, không dùng stealth browser và không
né `robots.txt`, rate limit hoặc access control. Khi gặp nguồn bị chặn, tool dừng
truy cập host liên quan, lưu nguyên nhân và tạo cảnh báo để người vận hành xử lý.

Ba điều kiện khác nhau cần được đánh giá trước khi bật một nguồn:

1. Được phép truy cập/crawl nguồn đó.
2. Được phép tải và lưu nội dung.
3. Được phép dùng nội dung cho mục đích downstream dự kiến.

`rights_confirmed = true` chỉ là xác nhận của người vận hành; tool không thể tự
xác minh giá trị pháp lý của giấy phép.

## 2. Kiến trúc và luồng dữ liệu tổng quát

```text
configs/*.toml
    |
    | dsforge validate
    v
config.py -> Settings / Source / Limits
    |
    | dsforge collect
    v
adapter registry -> Candidate discovery
    |
    v
bounded worker queue -> downloader.py -> policy.py
    |                  |               |
    |                  |               +-- HTTPS, robots, anti-bot, pacing
    |                  +-- stream, retry, quota, MIME signature
    v
checkpoints + raw/ + manifest.jsonl + events/alerts + hash index
    |
    | dsforge process
    v
processor registry -> processed.jsonl + derived/ + processing_summary.json
    |
    +----------------------+-------------------------+
    |                      |                         |
    | dsforge review       | dsforge export          | downstream riêng
    v                      v                         v
review.sqlite3        candidate-batch-v1 ZIP    human labeling/split
```

Các tài sản raw đã thu thập là bất biến trong một run. Mỗi record giữ nguồn,
origin, license note và SHA-256. Quyết định review được lưu riêng, không sửa
manifest và không tự động trở thành label.

## 3. Cài đặt

### 3.1. Yêu cầu

- Python 3.11 trở lên.
- PowerShell trên Windows cho các ví dụ trong tài liệu này.
- FFmpeg trong `PATH` nếu cần trích frame video.
- Chromium do Playwright quản lý nếu cần render trang JavaScript.

Từ thư mục repository:

```powershell
cd D:\dataset-foundry-starter
py -3.11 -m venv .venv
```

### 3.2. Chọn nhóm dependency

Cài bản tối thiểu cho nguồn local, ZIP và HTTPS:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

Cài metadata ảnh, Hugging Face và công cụ phát triển:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[images,huggingface,dev]"
```

Cài browser engine cho trang cần JavaScript:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[browser]"
.\.venv\Scripts\playwright.exe install chromium
```

Có thể kết hợp các extra nếu cần:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[images,huggingface,browser,dev]"
.\.venv\Scripts\playwright.exe install chromium
```

Kiểm tra CLI:

```powershell
.\.venv\Scripts\dsforge.exe --help
```

Nếu chưa cài editable package, có thể dùng module trực tiếp:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m dataset_foundry --help
```

## 4. Quickstart an toàn, không dùng mạng

Config mẫu chỉ bật nguồn synthetic trong repository. Chạy lần lượt:

```powershell
.\.venv\Scripts\dsforge.exe validate --config configs/sources.example.toml
.\.venv\Scripts\dsforge.exe collect `
  --config configs/sources.example.toml `
  --run workspace/demo-001
.\.venv\Scripts\dsforge.exe process --run workspace/demo-001
.\.venv\Scripts\dsforge.exe review --run workspace/demo-001
```

Lệnh `review` chạy foreground. Mở URL được in ra terminal, nhập token được in
kèm theo, review xong thì nhấn `Ctrl+C` để dừng server.

Sau đó export:

```powershell
.\.venv\Scripts\dsforge.exe export `
  --run workspace/demo-001 `
  --output exports/demo-001.zip
```

Không chạy lại `collect` thường vào cùng thư mục. Nếu lần chạy bị ngắt, dùng:

```powershell
.\.venv\Scripts\dsforge.exe collect `
  --config configs/sources.example.toml `
  --run workspace/demo-001 `
  --resume
```

Resume yêu cầu config hiệu dụng giống hệt lần chạy ban đầu.

## 5. Cấu hình TOML

Nên sao chép config mẫu và chỉ sửa bản local:

```powershell
Copy-Item configs/sources.example.toml configs/sources.local.toml
```

Khung cấu hình:

```toml
[project]
name = "my-multimodal-dataset"

[limits]
max_asset_mb = 100
max_source_mb = 1024
max_assets_per_source = 100
max_zip_compression_ratio = 100
min_request_delay_seconds = 1.5
max_workers = 4
retry_attempts = 3
retry_backoff_seconds = 1.0
mime_validation = "warn"

[[sources]]
id = "source-01"
adapter = "local_dir"
enabled = true
rights_confirmed = true
license = "permission-documented"
rights_note = "Approval reference and permitted use."
path = "../imports/source-01"
```

Đường dẫn `path` tương đối được tính từ vị trí của file TOML, không phải từ
working directory hiện tại.

### 5.1. `[project]`

| Thuộc tính | Mặc định | Tác dụng |
|---|---:|---|
| `name` | `unnamed` | Tên logic của collection, xuất hiện trong summary. |

### 5.2. `[limits]`

| Thuộc tính | Mặc định | Miền hợp lệ | Tác dụng |
|---|---:|---:|---|
| `max_asset_mb` | `100` | `1..2048` | Số MB tối đa của một asset. |
| `max_source_mb` | `1024` | từ `max_asset_mb` đến `1048576` | Tổng byte tối đa cho một source trong một run. |
| `max_assets_per_source` | `100` | `1..100000` | Số candidate tối đa mà adapter được phép phát hiện. |
| `max_zip_compression_ratio` | `100` | `1..10000` | Chặn ZIP member có tỷ lệ giải nén đáng ngờ. |
| `min_request_delay_seconds` | `1.5` | `0.5..3600` | Khoảng cách tối thiểu giữa request tới cùng host. |
| `max_workers` | `4` | `1..32` | Số fetch worker chạy song song. |
| `retry_attempts` | `3` | `1..10` | Tổng số lần thử cho lỗi HTTP/network tạm thời. |
| `retry_backoff_seconds` | `1.0` | `0..60` | Base delay của exponential backoff. |
| `mime_validation` | `warn` | `off`, `warn`, `strict` | Cách xử lý MIME tên/header khác chữ ký nội dung. |

`max_source_mb` tính cả byte của candidate bị xác định là duplicate sau khi tải
thành công. Phần stream chưa hoàn tất và bị từ chối sẽ giải phóng reservation.

### 5.3. Thuộc tính chung của `[[sources]]`

| Thuộc tính | Bắt buộc | Ý nghĩa |
|---|---|---|
| `id` | Có | ID duy nhất, tối đa 64 ký tự chữ/số/`_`/`-`. |
| `adapter` | Có | Tên adapter built-in hoặc entry-point plugin. |
| `enabled` | Có để collect | `false` khiến nguồn được ghi nhận là `skipped`. |
| `rights_confirmed` | Có để collect | Phải là `true` trước khi tải. |
| `license` | Có khi collect | Không được để `unknown` khi nguồn đã bật và xác nhận quyền. |
| `rights_note` | Có khi bật | Ghi căn cứ/quyền/phạm vi sử dụng để audit. |
| `include_extensions` | Không | Chỉ nhận các đuôi file được liệt kê, viết dạng `.jpg`. |
| `allowed_mime_types` | Không | Allowlist MIME dựa trên nội dung, ví dụ `image/*`. |
| `recursive` | Không | Cho `local_dir`; mặc định quét thư mục con. |
| `render_engine` | Không | Chỉ cho `web_page`: `direct`, `auto`, `browser`. |

Nguồn `enabled = true` nhưng `rights_confirmed = false` không bị tải; pipeline
ghi event `rights_not_confirmed`. Tuy nhiên config đã bật phải có `rights_note`.

### 5.4. MIME allowlist

Ví dụ chỉ nhận PNG và JPEG:

```toml
allowed_mime_types = ["image/png", "image/jpeg"]
```

Ví dụ nhận mọi loại ảnh:

```toml
allowed_mime_types = ["image/*"]
```

Khi có `allowed_mime_types`, chữ ký không nhận diện được hoặc không nằm trong
allowlist luôn bị từ chối, bất kể `mime_validation` là `off`, `warn` hay
`strict`.

## 6. Các adapter nguồn

### 6.1. `local_dir`: thư mục cục bộ

```toml
[[sources]]
id = "approved-local"
adapter = "local_dir"
enabled = true
rights_confirmed = true
license = "internal-approved"
rights_note = "Owned by the organization; approval ticket DATA-123."
path = "../imports/approved-local"
recursive = true
include_extensions = [".jpg", ".jpeg", ".png", ".mp4", ".txt"]
```

Tác dụng: phát hiện file trong thư mục, áp dụng giới hạn số file/kích thước, rồi
copy theo stream vào `RUN/raw/<source-id>/`. Nguồn local không phải filesystem
snapshot; không nên thay đổi file nguồn khi collection đang chạy.

### 6.2. `local_zip`: ZIP cục bộ

```toml
[[sources]]
id = "approved-archive"
adapter = "local_zip"
enabled = true
rights_confirmed = true
license = "permission-documented"
rights_note = "Archive supplied by data owner under agreement DATA-456."
path = "../imports/dataset.zip"
include_extensions = [".jpg", ".png", ".json"]
```

Tác dụng: đọc từng ZIP member mà không giải nén hàng loạt. Adapter từ chối path
không an toàn, symlink, member mã hóa, file quá lớn và compression ratio vượt
giới hạn. Mọi skip đều được lưu cùng lý do.

### 6.3. `http_list`: danh sách URL tệp HTTPS

```toml
[[sources]]
id = "approved-assets"
adapter = "http_list"
enabled = true
rights_confirmed = true
license = "source-license"
rights_note = "Specific file URLs approved; terms checked on 2026-10-11."
urls = [
  "https://approved.example/data/image-001.jpg",
  "https://approved.example/data/metadata.json",
]
allowed_mime_types = ["image/*", "application/json"]
```

Adapter này chỉ tải đúng các URL được khai báo. Nó không parse HTML để tìm link
và không spider website. Chỉ HTTPS, cổng chuẩn, không credential trong URL,
không private/internal IP và không redirect tự động.

### 6.4. `hf_dataset`: Hugging Face public dataset API

```toml
[[sources]]
id = "hf-approved"
adapter = "hf_dataset"
enabled = true
rights_confirmed = true
license = "apache-2.0"
rights_note = "Dataset card and intended use reviewed."
repo_id = "organization/repository"
revision = "main"
include_extensions = [".jpg", ".png", ".json", ".parquet"]
```

Tác dụng: dùng API dataset công khai để liệt kê rồi tải file phù hợp. Adapter
không dùng token và không vượt qua gated/private repository. Nếu repo cần quyền
truy cập, để nguồn tắt hoặc dùng quy trình cấp quyền bên ngoài tool.

### 6.5. `web_page`: trang HTML đã được phê duyệt

```toml
[[sources]]
id = "approved-web-page"
adapter = "web_page"
enabled = true
rights_confirmed = true
license = "permission-documented"
rights_note = "Owner approved collection of this exact page."
urls = ["https://approved.example/app"]
render_engine = "auto"
allowed_mime_types = ["text/html"]
```

Ba render engine:

| Chế độ | Hành vi | Khi nên dùng |
|---|---|---|
| `direct` | Request HTML trực tiếp, không chạy JavaScript. | Trang static/API-first, nhanh và ít tài nguyên. |
| `auto` | Request trực tiếp trước; chỉ dùng Chromium nếu nội dung là JS shell nhỏ. | Trang có thể static hoặc client-rendered. |
| `browser` | Luôn render bằng Playwright/Chromium. | Trang được duyệt và chắc chắn cần JavaScript. |

Browser chặn service worker, download tự động, redirect và subresource khác host;
chỉ cho same-host public HTTPS. `auto` không bao giờ chuyển sang browser sau khi
phát hiện access denial, bot challenge hoặc CAPTCHA.

Lưu ý: adapter lưu trang HTML được cấu hình, không tự khám phá và tải toàn bộ
ảnh/video/link xuất hiện trong trang.

## 7. Các lệnh CLI

Mỗi lệnh đều nạp adapter/processor plugin đã cài qua Python entry point trước
khi xử lý tham số nghiệp vụ.

### 7.1. `validate`: kiểm tra config

```powershell
.\.venv\Scripts\dsforge.exe validate --config configs/sources.local.toml
```

Lệnh này kiểm tra kiểu dữ liệu, giới hạn, source ID, adapter, các trường bắt buộc,
license/rights note và render engine. Kết quả JSON cho biết nguồn nào `enabled`
và `collectible`. Lệnh không tải dữ liệu và không chứng minh rằng remote source
đang truy cập được.

### 7.2. `collect`: thu thập candidate

```powershell
.\.venv\Scripts\dsforge.exe collect `
  --config configs/sources.local.toml `
  --run workspace/run-2026-10-11
```

| Option | Tác dụng |
|---|---|
| `--config PATH` | Registry TOML cần dùng. |
| `--run PATH` | Thư mục run mới; phải rỗng khi collect lần đầu. |
| `--resume` | Khôi phục đúng run và config cũ sau gián đoạn. |
| `--no-progress` | Tắt progress trên stderr, hữu ích cho automation. |
| `--hash-index PATH` | Đổi vị trí SQLite exact-hash index. |

Progress được in ra `stderr`; JSON summary cuối cùng được in ra `stdout`. Vì vậy
script có thể redirect JSON mà không trộn progress:

```powershell
.\.venv\Scripts\dsforge.exe collect `
  --config configs/sources.local.toml `
  --run workspace/run-001 `
  1> collect-result.json
```

Progress ở cấp asset hiển thị số candidate đã xử lý và số byte đã dùng so với
quota. Progress ở cấp source hiển thị `current/total`, phần trăm và trạng thái.
Phần trăm source không phải phần trăm byte download chính xác.

### 7.3. `process`: enrichment và derived frame

Chạy processor mặc định:

```powershell
.\.venv\Scripts\dsforge.exe process --run workspace/run-001
```

Mặc định chọn `image_metadata` và `video_frames`. Khi
`--video-interval` bằng `0`, video frame extraction bị tắt:

```powershell
.\.venv\Scripts\dsforge.exe process `
  --run workspace/run-001 `
  --video-interval 5 `
  --max-frames-per-video 40
```

Ví dụ chọn processor cụ thể hoặc plugin processor; lặp `--processor` để tạo
pipeline theo thứ tự khai báo:

```powershell
.\.venv\Scripts\dsforge.exe process `
  --run workspace/run-001 `
  --processor image_metadata `
  --processor my_processor
```

`image_metadata` dùng Pillow nếu có để verify ảnh, lấy width/height, aHash 64-bit
và flag ảnh nhỏ. Không có Pillow sẽ ghi `pillow_not_installed`. `video_frames`
dùng FFmpeg, tạo file trong `derived/<asset-id>/`; lỗi/thiếu FFmpeg được ghi bằng
quality flag.

Mỗi lần process sẽ tạo lại `derived/` và atomically thay `processed.jsonl`.
Processor không được sửa provenance raw, review status, label, split hoặc khai
checksum sai; validation sẽ từ chối toàn bộ kết quả.

### 7.4. `review`: giao diện duyệt cục bộ

```powershell
.\.venv\Scripts\dsforge.exe review --run workspace/run-001
```

Mặc định server bind `127.0.0.1:8765`, sinh session token rồi in URL/token ra
terminal. Có thể chọn port và token:

```powershell
.\.venv\Scripts\dsforge.exe review `
  --run workspace/run-001 `
  --port 8877 `
  --token use-a-long-random-local-token
```

UI ưu tiên `processed.jsonl`; nếu chưa process thì dùng `manifest.jsonl`. Chỉ
asset `downloaded`/`derived` được hiển thị. Người review chọn:

- `accepted`: chấp nhận về mặt review;
- `rejected`: loại khỏi quy trình downstream;
- `pending`: chưa quyết định;
- note: ghi chú tối đa 4.000 ký tự.

Quyết định được lưu trong `review.sqlite3`. Server kiểm tra path, size và SHA-256
trước khi phục vụ asset, dùng bearer token và security headers. Chỉ địa chỉ
loopback IPv4 được chấp nhận.

Giới hạn quan trọng: Review UI hiện lưu trạng thái quyết định mới nhất, chưa phải
audit-history đầy đủ; quyết định chưa được merge vào export v1 và không tự đổi
`review_status=unreviewed` trong manifest.

### 7.5. `export`: đóng gói candidate batch

```powershell
.\.venv\Scripts\dsforge.exe export `
  --run workspace/run-001 `
  --output exports/run-001.zip
```

Exporter kiểm tra lại file tồn tại, path an toàn, size và SHA-256 trước khi ghi.
ZIP được tạo ở file tạm rồi atomically replace output khi hoàn tất. Output phải
nằm ngoài run directory.

Contract hiện tại là `candidate-batch-v1`. Asset vẫn là candidate chưa được gán
nhãn/chia split, kể cả khi Review UI đã có decision. Downstream importer phải
tự đọc/quản lý quyết định review theo workflow riêng.

## 8. Giải thích chi tiết từng tính năng độ tin cậy

### 8.1. Quota byte theo source

`max_asset_mb` giới hạn từng file; `max_source_mb` giới hạn tổng byte của một
source. `SourceByteQuota` là bộ đếm thread-safe nên nhiều worker không thể cùng
vượt quota. Với HTTP, quota được reserve theo chunk trong lúc stream. Với nguồn
biết trước size, quota được kiểm tra trước khi copy.

Khi vượt quota, record bị skip hoặc source chuyển degraded; `alerts.jsonl` có
category `source_quota_exceeded`. Tác dụng là ngăn một source chiếm hết disk dù
nó có rất nhiều file nhỏ hơn quota per-file.

### 8.2. Progress reporting

CLI nhận callback từ pipeline và in hai loại sự kiện:

- asset: source ID, số candidate đã xử lý, byte đã dùng/quota, status gần nhất;
- source: vị trí source, tổng source, phần trăm, status.

Tắt bằng `--no-progress` khi gọi từ job runner. Kết quả máy đọc vẫn là JSON trên
stdout, còn progress nằm trên stderr.

### 8.3. Retry policy

Tool retry có giới hạn cho exception mạng tạm thời và HTTP `408`, `425`, `500`,
`502`, `503`, `504`. Delay theo exponential backoff:

```text
retry_backoff_seconds * 2^(attempt - 1)
```

Delay tối đa 60 giây. Nếu server gửi `Retry-After` dạng số, tool tôn trọng giá
trị đó trong giới hạn 60 giây. Tổng số lần thử do `retry_attempts` quyết định và
được ghi vào record.

Tool không retry `401`, `403`, `429`, `451`, robots denial, redirect, CAPTCHA hay
bot challenge. Sau khi hết retry, cảnh báo `retry_exhausted` được ghi.

### 8.4. Download song song có giới hạn

`ThreadPoolExecutor` chạy tối đa `max_workers`. Pipeline chỉ giữ một cửa sổ công
việc có giới hạn, không submit vô hạn. Fetch có thể hoàn tất song song nhưng
checkpoint/manifest/hash index được commit theo thứ tự discovery để kết quả ổn
định và dễ audit.

Pacing theo host vẫn thread-safe, nên tăng worker không vô hiệu
`min_request_delay_seconds`. Tăng `max_workers` chủ yếu có lợi khi có nhiều host
hoặc nguồn local; không đảm bảo một host sẽ được request nhanh hơn.

### 8.5. Kiểm tra MIME bằng content signature

Tool không chỉ tin đuôi file. Nó ghi:

- `mime_guess`: đoán từ tên/đuôi file;
- `mime_declared`: `Content-Type` do server khai báo, nếu có;
- `mime_detected`: phát hiện từ byte signature/nội dung;
- `mime_validation`: kết quả so sánh.

Các chữ ký phổ biến gồm ảnh, PDF, ZIP/gzip/bzip/7z/RAR, audio, Ogg, WAV, WebP,
AVI, MP4, XML, HTML, JSON và text. Ba policy:

- `off`: không từ chối chỉ vì mismatch, vẫn ghi metadata;
- `warn`: giữ asset và ghi mismatch;
- `strict`: skip khi chữ ký không phù hợp.

Response HTML trả về cho một direct asset URL luôn bị skip, kể cả policy `off`
hoặc `warn`, để tránh lưu trang lỗi/challenge như một asset hợp lệ. Đây là
signature screening, không thay thế việc decode/validate media đầy đủ.

### 8.6. Python entry-point plugin

Package đáng tin cậy có thể tự đăng ký adapter và processor khi được cài bằng
pip. Khai báo trong `pyproject.toml` của package plugin:

```toml
[project.entry-points.dataset_foundry.adapters]
my_adapter = my_package.adapter:discover

[project.entry-points.dataset_foundry.processors]
my_processor = my_package.processor:process_record
```

Sau `pip install`, tên entry point dùng trực tiếp trong `adapter = my_adapter`
hoặc `--processor my_processor`. Loader chạy deterministic/idempotent và fail
closed nếu trùng tên, import lỗi hoặc object không callable.

Cài plugin đồng nghĩa cho phép chạy Python code của package đó. Chỉ cài package
được tin cậy. Adapter plugin hiện dùng các field cố định của `Source` và các
candidate kind/downloader hiện hữu; transport hoặc TOML field hoàn toàn mới cần
thay đổi core API có kiểm soát.

### 8.7. Exact dedup trong và xuyên run

Trong một run, `seen_hashes` phát hiện SHA-256 đã xuất hiện và ghi record
`duplicate` trỏ tới `duplicate_of`; file copy thừa bị xóa.

Giữa các run, SQLite index mặc định nằm tại:

```text
<thư mục cha của run>/.dataset-foundry-hashes.sqlite3
```

Ví dụ cả `workspace/run-001` và `workspace/run-002` dùng chung
`workspace/.dataset-foundry-hashes.sqlite3`. Khi hash trùng, record có:

- `duplicate_scope = cross_run`;
- `duplicate_run`;
- `duplicate_of`.

Index kiểm tra file đã lập chỉ mục còn tồn tại và đúng size/hash trước khi dùng;
entry stale bị loại. Có thể tách index theo workspace/job:

```powershell
.\.venv\Scripts\dsforge.exe collect `
  --config configs/sources.local.toml `
  --run workspace/run-003 `
  --hash-index indexes/project-a.sqlite3
```

Đây chỉ là exact SHA-256 dedup, không phát hiện ảnh resize/crop, video gần giống
hoặc nội dung semantic giống nhau. aHash ảnh trong processing chỉ là metadata
gợi ý cho bước dedup downstream.

### 8.8. Crash resume và checkpoint

Mỗi candidate outcome được ghi vào checkpoint riêng bằng atomic replace trước
khi xuất hiện trong `manifest.jsonl`. `--resume`:

1. so config fingerprint với run cũ;
2. đọc checkpoint theo thứ tự;
3. xác minh raw file đã checkpoint bằng size/hash;
4. dựng lại manifest;
5. bỏ qua source đã terminal;
6. tiếp tục phần chưa commit.

Asset đã ghi xong ngay trước interruption có thể được phục hồi mà không overwrite.
Nếu resume commit candidate mới, output processing cũ bị invalidated và cần chạy
`process` lại. Resume không phải cách đổi config, thêm source hoặc vượt access
control.

### 8.9. Phát hiện block, CAPTCHA và anti-bot

`policy.py` và downloader phân loại theo status, header và prefix nội dung có
giới hạn. Tool có heuristic cho Cloudflare, Akamai, Imperva, DataDome và các dấu
hiệu CAPTCHA/bot challenge phổ biến.

Kết quả có thể là:

- `captcha_required`;
- `bot_challenge`;
- `rate_limited`;
- `access_denied`;
- `robots_policy`.

Khi gặp tình huống này, host circuit mở để các candidate sau của cùng host không
tiếp tục gây tải; source thường kết thúc `degraded`. Heuristic nhận diện vendor
không phải bằng chứng chắc chắn rằng IP đang bị block. Muốn xác định IP hiện tại
có bị block hay không phải chạy source cụ thể đã được ủy quyền và xem response,
`events.jsonl`/`alerts.jsonl`; không thể kết luận chỉ từ code.

Tool cố ý không giải CAPTCHA hay bypass anti-bot. Cách xử lý hợp lệ là dùng API
chính thức, xin allowlist/quyền truy cập từ chủ nguồn, giảm tần suất, sửa URL hoặc
để nguồn bị skip.

### 8.10. Chuyển direct/browser linh hoạt

Với `web_page`, chế độ `auto` thực hiện chiến lược tiết kiệm tài nguyên:

```text
direct HTTPS fetch
    |
    +-- nội dung HTML đầy đủ ------> lưu kết quả direct
    |
    +-- JS shell nhỏ --------------> Playwright Chromium
    |
    +-- denial/challenge/CAPTCHA --> dừng, ghi alert; không browser fallback
```

`browser` phù hợp khi biết rõ trang cần JavaScript. Việc render headless không
làm thay đổi chính sách URL/quyền/robots và không phải cơ chế né anti-bot.

### 8.11. Monitoring, cảnh báo và tự khắc phục

Tool tạo ba lớp quan sát:

- progress tức thời trên terminal;
- `events.jsonl` cho lifecycle source: `started`, `resumed`, `completed`,
  `degraded`, `skipped`, `error`;
- `alerts.jsonl` cho CAPTCHA, challenge, rate limit, access denial, robots,
  source quota và retry exhaustion.

`collection_summary.json` tổng hợp downloaded/duplicate/skipped/error, source
degraded/failed, alert và collected byte. `run_state.json` có trạng thái cuối
`completed` hoặc `degraded`.

Tự khắc phục hiện có:

- retry bounded cho lỗi tạm thời;
- exponential backoff/`Retry-After`;
- host circuit để tránh lặp truy cập bị chặn;
- checkpoint và resume sau crash;
- stale cross-run index validation.

Chưa có dashboard, Prometheus, email/Slack/PagerDuty, scheduler tự resume hay
quy trình tự xin quyền/allowlist. Có thể để hệ thống ngoài đọc JSONL/summary và
phát cảnh báo.

## 9. Các file đầu ra

Ví dụ cấu trúc một run:

```text
workspace/run-001/
  raw/
    source-id/
      <asset-id>.<ext>
  derived/                         # có sau process nếu sinh asset con
  checkpoints/
    00000000.json
  manifest.jsonl
  processed.jsonl                 # có sau process
  sources.json
  events.jsonl
  alerts.jsonl                    # chỉ có khi phát sinh alert
  run_state.json
  collection_summary.json
  processing_summary.json         # có sau process
  review.sqlite3                  # có sau review
workspace/.dataset-foundry-hashes.sqlite3
```

| File | Vai trò |
|---|---|
| `raw/` | Bản raw đã stream/copy thành công, không sửa trong run. |
| `derived/` | Frame hoặc output do processor sinh ra. |
| `checkpoints/` | Recovery record theo candidate. |
| `manifest.jsonl` | Mọi outcome: downloaded, duplicate, skipped, error. |
| `processed.jsonl` | Manifest đã enrichment và thêm derived record. |
| `sources.json` | Attribution, license và rights note của nguồn. |
| `events.jsonl` | Lifecycle event cấp source. |
| `alerts.jsonl` | Cảnh báo chính sách/vận hành khi có. |
| `run_state.json` | Fingerprint và trạng thái run. |
| `collection_summary.json` | Tổng hợp collection. |
| `processing_summary.json` | Tổng hợp processing/quality flags. |
| `review.sqlite3` | Quyết định review sidecar. |
| `.dataset-foundry-hashes.sqlite3` | Exact-hash index dùng chung giữa run. |

Các status chính trong manifest:

| Status | Ý nghĩa |
|---|---|
| `downloaded` | Asset đã được lưu và có checksum. |
| `derived` | Asset con được processor tạo từ asset raw. |
| `duplicate` | Nội dung SHA-256 trùng trong/cross-run, không giữ bản raw thừa. |
| `skipped` | Bỏ qua có chủ đích do policy/quota/unsafe input. |
| `error` | Lỗi ngoài skip policy, được giữ để audit. |

## 10. Luồng hoạt động trong codebase

### 10.1. Entry point và plugin loading

1. `src/dataset_foundry/cli.py` tạo parser và nhận subcommand.
2. `load_entry_point_plugins()` trong `plugins.py` nạp adapter/processor package.
3. CLI gọi `load_settings()`, `collect()`, `process()`, `serve_review()` hoặc
   `export_batch()` tùy lệnh.
4. Kết quả thông thường được serialize JSON ra stdout.

### 10.2. Luồng `validate`

```text
cli.py
  -> plugins.py: load entry points
  -> config.py: parse TOML bằng tomllib
  -> models.py: tạo immutable Settings, Limits, Source
  -> adapters/discovery.py: xác minh adapter name đã đăng ký
  -> cli.py: in project/source/collectible JSON
```

`config.py` fail closed nếu sai type, trùng ID, quota ngoài giới hạn, quyền thiếu,
adapter lạ, extension/MIME sai format hoặc dùng render engine sai adapter.

### 10.3. Luồng `collect`

```text
pipeline.collect()
  -> _prepare_run(): tạo/kiểm tra run và config fingerprint
  -> load + verify checkpoints
  -> HashIndex(SQLite) + requests.Session(trust_env=False)
  -> WebPolicy(per-host pace, URL/DNS/robots/circuit)
  -> mỗi Source:
       -> kiểm tra enabled + rights_confirmed
       -> adapters.discovery.discover()
       -> adapter yield Candidate
       -> _bounded_fetches(ThreadPoolExecutor)
       -> downloader.fetch()
            -> local/ZIP/HF/HTTP/web-page transport
            -> bounded stream + source quota
            -> retry/backoff nếu lỗi tạm thời
            -> challenge/CAPTCHA classification
            -> MIME signature validation
       -> SHA-256 dedup trong run
       -> HashIndex lookup cho cross-run dedup
       -> atomic checkpoint
       -> manifest.jsonl
       -> hash index registration
       -> progress + alerts
  -> collection_summary.json + run_state.json
```

Các adapter chỉ tập trung discovery/data access. Download và policy tập trung ở
core để mọi adapter dùng cùng quota, checksum, MIME, retry và audit behavior.

### 10.4. Luồng `process`

```text
processors.process()
  -> đọc manifest.jsonl
  -> xóa/tạo lại derived/
  -> với mỗi record downloaded:
       -> chạy processor theo thứ tự registry
       -> enrich record hoặc tạo child record
  -> kiểm tra protected fields, parent/path/checksum/review state
  -> atomic write processed.jsonl
  -> processing_summary.json
```

Processor built-in nằm trong `processors.py`. Registry cho phép mở rộng nhưng
mọi output vẫn đi qua invariant validation của core.

### 10.5. Luồng `review`

```text
review.serve_review()
  -> ReviewStore đọc processed.jsonl hoặc manifest.jsonl
  -> whitelist downloaded/derived asset IDs
  -> tạo/mở review.sqlite3
  -> ThreadingHTTPServer trên IPv4 loopback
  -> xác thực token cho API/UI
  -> verify path + size + SHA-256 trước khi serve file
  -> upsert accepted/rejected/pending + note vào SQLite
```

Review sidecar tách biệt nhằm giữ raw/manifest bất biến và tránh silent label
promotion.

### 10.6. Luồng `export`

```text
exporter.export_batch()
  -> chọn processed.jsonl nếu có, ngược lại manifest.jsonl
  -> validate schema status/review/label/split
  -> verify từng asset path + size + SHA-256
  -> ghi ZIP tạm:
       schema.json
       manifest.jsonl
       sources.json
       collection_summary.json
       processing_summary.json (nếu có)
       events.jsonl
       README.txt
       assets/raw/... và assets/derived/...
  -> atomic replace output ZIP
```

Chi tiết contract xem `docs/EXPORT-CONTRACT.md`.

### 10.7. Bản đồ module

| Module | Trách nhiệm chính |
|---|---|
| `cli.py` | Command-line interface và routing. |
| `config.py` | Parse/validate TOML, fail-closed config. |
| `models.py` | `Settings`, `Limits`, `Source`, `Candidate`. |
| `adapters/discovery.py` | Adapter registry và dispatch. |
| `adapters/*.py` | Discovery cho từng loại source. |
| `downloader.py` | Fetch/copy stream, quota, retry, MIME, browser. |
| `policy.py` | URL/DNS/robots/pacing/circuit/challenge policy. |
| `content_types.py` | Nhận diện MIME từ content signature. |
| `pipeline.py` | Điều phối collection, workers, checkpoints, manifests. |
| `hash_index.py` | SQLite SHA-256 index xuyên run. |
| `plugins.py` | Nạp Python entry-point plugin. |
| `processors.py` | Processor registry, enrichment và invariant checks. |
| `review.py` | Token-protected local UI và review SQLite. |
| `exporter.py` | Validate và tạo candidate-batch ZIP. |
| `manifest.py` | Đọc/ghi JSON/JSONL atomically. |

## 11. Quy trình vận hành đề xuất

### 11.1. Nguồn local/ZIP

1. Đặt dữ liệu vào thư mục import riêng, không commit vào repository.
2. Tạo config local và ghi rõ license/rights note.
3. Chạy `validate`.
4. Chạy `collect` vào run mới.
5. Đọc `collection_summary.json`, record skip/error và event degraded.
6. Chạy `process`.
7. Chạy Review UI và ghi quyết định.
8. Export candidate batch hoặc đưa quyết định sang workflow downstream.

### 11.2. Nguồn HTTP/API

1. Ưu tiên API/export chính thức hơn HTML scraping.
2. Chỉ khai báo URL cụ thể được phê duyệt.
3. Đặt MIME allowlist và quota thấp khi thử nghiệm.
4. Bắt đầu với worker nhỏ và delay phù hợp điều khoản nguồn.
5. Kiểm tra `alerts.jsonl`; không tự động retry lại nguồn access-denied.
6. Liên hệ chủ nguồn hoặc dùng allowlist/API nếu bị challenge.

### 11.3. Trang JavaScript

1. Xác nhận quyền với đúng page/host.
2. Cài Playwright Chromium.
3. Dùng `render_engine = auto` trước nếu chưa chắc cần browser.
4. Dùng `browser` khi đã xác minh trang cần JS.
5. Nếu có CAPTCHA/challenge, dừng và xử lý quyền truy cập ngoài tool.

### 11.4. Automation/CI

```powershell
.\.venv\Scripts\dsforge.exe validate --config configs/sources.local.toml
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

.\.venv\Scripts\dsforge.exe collect `
  --config configs/sources.local.toml `
  --run workspace/job-001 `
  --no-progress `
  1> workspace/job-001-result.json
```

Job runner nên parse `collection_summary.json` và coi `errors > 0`,
`failed_sources > 0`, `degraded_sources > 0` hoặc `alerts > 0` là cần kiểm tra,
kể cả khi process đã tạo đủ artifact audit.

## 12. Troubleshooting

### `Fresh run directory must be empty`

Chọn tên run mới. Chỉ dùng `--resume` nếu đó chính là run cũ bị gián đoạn.

### Resume báo config fingerprint không khớp

Khôi phục đúng config ban đầu hoặc tạo run mới. Không chỉnh quota/source rồi
resume vào run cũ.

### Nguồn bị `rights_not_confirmed`

Kiểm tra quyền, điền `license`, `rights_note`, đặt `rights_confirmed = true` và
tạo run mới. Không sửa config để vượt quyền khi chưa có phê duyệt.

### `browser`/`auto` cần Playwright

```powershell
.\.venv\Scripts\python.exe -m pip install -e .[browser]
.\.venv\Scripts\playwright.exe install chromium
```

### `ffmpeg_not_installed`

Cài FFmpeg và đảm bảo lệnh `ffmpeg -version` chạy được trong cùng shell. Sau đó
chạy lại `process`.

### `pillow_not_installed`

```powershell
.\.venv\Scripts\python.exe -m pip install -e .[images]
```

### Có `captcha_required`, `bot_challenge`, `access_denied` hoặc `rate_limited`

Không tăng worker, dùng proxy hoặc browser fallback để cố vượt chặn. Kiểm tra
URL/quyền/robots, giảm tải, dùng API chính thức hoặc xin allowlist từ chủ nguồn.

### MIME mismatch

Đối chiếu `mime_guess`, `mime_declared`, `mime_detected` và URL. Nếu server trả
HTML thay vì asset thì thường là lỗi/challenge. Chỉ đổi `strict` sang `warn` khi
đã hiểu và chấp nhận nội dung thực tế; không dùng `off` để che response sai.

### Run kết thúc `degraded`

Đọc theo thứ tự:

1. `collection_summary.json`;
2. `alerts.jsonl` nếu có;
3. các event `degraded`/`error` trong `events.jsonl`;
4. các record `skipped`/`error` và `reason` trong `manifest.jsonl`.

`degraded` không đồng nghĩa toàn bộ run thất bại; nó cho biết ít nhất một phần
cần operator review.

## 13. Kiểm thử khi phát triển tool/plugin

Chạy toàn bộ unit test:

```powershell
$env:PYTHONPATH = src
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Chạy Ruff nếu đã cài extra `dev`:

```powershell
.\.venv\Scripts\python.exe -m ruff check src tests
```

Dùng fixture synthetic; không commit dataset thật, token, PII, `workspace/`,
`exports/` hoặc cache nặng. Khi thêm adapter, processor hoặc thay đổi contract,
đọc `AGENTS.md`, `docs/ARCHITECTURE.md` và `docs/EXPORT-CONTRACT.md` trước.

## 14. Giới hạn hiện tại

- Không crawl/spider site không được liệt kê cụ thể.
- Không giải CAPTCHA, bypass anti-bot/login/access control hoặc xoay proxy.
- Không thể kết luận IP có bị block nếu chưa chạy nguồn được phép và quan sát
  response thực tế.
- Browser chỉ render page đã duyệt, không phải full browser automation platform.
- Audio/document được ingest và phân loại, chưa transcript/decode chuyên sâu.
- Review decision chưa được đưa vào `candidate-batch-v1` và chưa có audit history.
- Chưa có external alert integration hoặc dashboard.
- Exact hash index không thay thế perceptual/semantic dedup.
- Không tự gán ontology, label hoặc train/validation/test split.
- Một run directory là single-writer workspace; không chạy đồng thời nhiều lệnh
  ghi vào cùng run.

Các giới hạn này giúp raw asset, provenance và quyền sử dụng luôn audit được,
đồng thời ngăn collection vô tình biến thành crawler không kiểm soát.
