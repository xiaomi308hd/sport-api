# sport-api — MHD Sports (M3U + XMLTV EPG)

Tự động lấy lịch thi đấu và các luồng phát từ những API được khai báo trong `config.json`, sau đó tạo đồng thời `thethao.m3u` và `epg.xml` (XMLTV). GitHub Actions chạy định kỳ mỗi 10 phút.

## Cấu trúc

```text
sport-api/
├── .github/workflows/update-sports.yml
├── scripts/update.py
├── config.json
├── referer_cache.json          # cache Referer tự dò theo host
├── logos/                      # logo nhà đài (chuoichientv.png, gavangtv.png)
├── thethao.m3u                 # tất cả nhà đài (tab "Tất cả")
├── thethao-chuoichientv.m3u    # chỉ trận lấy từ API Chuối Chiên
├── thethao-gavangtv.m3u        # chỉ trận lấy từ API Gà Vàng
├── sources.json                # danh sách nhà đài + logo + giải đấu + trận
└── epg.xml
```

## M3U và EPG hoạt động cùng nhau

- `thethao.m3u`: chứa các URL phát, tên trận, thời gian Việt Nam, tỉ số khi API có dữ liệu, `tvg-logo`, `group-title` và `tvg-id`.
- `epg.xml`: chứa các `<channel>` và `<programme>` XMLTV. Mỗi trận có một `tvg-id` ổn định; các luồng HD1/HD2/FHD của trận đó cùng tham chiếu đến ID này.
- XMLTV giữ riêng logo đội nhà, logo đội khách và logo giải đấu bằng các phần tử `<icon>`.
- `programme start` được chuyển về múi giờ Việt Nam (`+0700`). Chỉ ghi `stop` nếu API thực sự cung cấp thời gian kết thúc; script không tự đoán thời lượng trận đấu.

Lưu ý: khả năng hiển thị logo hai đội phụ thuộc trình phát IPTV. Một số trình phát chỉ hiển thị logo ở cấp channel; các trình phát XMLTV khác có thể bỏ qua nhiều `<icon>` trong một programme. `tvg-logo` trong M3U dùng logo giải đấu trước, sau đó mới fallback sang logo đội nhà/đội khách.

## Cấu hình API

`config.json` hiện cấu hình API ChuoiChienTV với tham số `type=live`, `page=1`, `limit=500`; gửi `Accept`, User-Agent trình duyệt, `Referer` và `Origin` của trang player đã dùng trong cấu hình trước. Script có timeout 25 giây, tối đa 2 lần retry cho lỗi tạm thời, các nhóm `blvs`, `blvs_bonglau`, `blvs_nguoitho`, và Referer riêng cho hostname `gckc0525.edgemaxcdn.org`.

HTTP 403 không được retry hoặc tìm cách vượt hạn chế truy cập. Cấu hình `Referer`/`Origin` chỉ giúp yêu cầu tương thích với luồng truy cập web thông thường; không đảm bảo máy chủ cấp quyền. Nếu API vẫn trả 403, cần nhà cung cấp cấp quyền hoặc thay bằng API công khai được phép truy cập. Các lỗi tạm thời như 408, 429, 500, 502, 503, 504 có thể được thử lại. Nếu một API lỗi, các luồng và lịch EPG đã ghi lần trước của riêng API đó được giữ lại, còn API khác vẫn cập nhật bình thường (`sources.json` đánh `ok: false`, `stale: true` cho nguồn lỗi). Nếu mọi API đều lỗi hoặc không có stream hợp lệ nào, các file M3U/EPG cũ được giữ nguyên và workflow báo thất bại.

## HTTP Referer

URL M3U8 được giữ nguyên. Referer được lấy từ `stream.headers` hoặc cấu hình `stream_header_rules` và xuất dưới dạng `#EXTVLCOPT:http-referrer=...`. Đây là cú pháp VLC; trình phát khác có thể không hỗ trợ. Ứng dụng MHD Sports cần tự gửi các HTTP headers tương ứng khi phát luồng.

Chỉ cấu hình nguồn và luồng mà bạn được phép truy cập, sử dụng và phân phối lại.

## Chạy thử

Yêu cầu Python 3.10+; không cần cài thư viện bên ngoài:

```bash
python scripts/update.py
```

Sau khi chạy thành công, repository có hai URL raw:

```text
https://raw.githubusercontent.com/USERNAME/sport-api/main/thethao.m3u
https://raw.githubusercontent.com/USERNAME/sport-api/main/epg.xml
```

Thay `USERNAME` bằng tài khoản GitHub của bạn. Trong ứng dụng IPTV, nhập URL M3U làm playlist và URL XMLTV làm EPG (nếu ứng dụng có mục EPG/XMLTV).

## GitHub Actions

1. Giải nén và đưa nội dung thư mục `sport-api` vào repository hiện tại.
2. Settings → Actions → General → Workflow permissions: chọn `Read and write permissions`.
3. Vào Actions → **Update MHD Sports M3U + XMLTV EPG** → Run workflow **một lần duy nhất** để khởi động.

Cron của GitHub hay trễ hoặc bị bỏ lượt, nên workflow tự chạy **vòng lặp**: mỗi lượt kéo dài ~50 phút, cứ 3 phút cập nhật một lần (chỉ commit khi có thay đổi), cuối lượt tự kích hoạt lượt kế tiếp. Cron `7,22,37,52` chỉ là dự phòng để khởi động lại nếu chuỗi bị đứt. Sửa `config.json`/`scripts/` (push) sẽ chạy ngay một lần mà không phải chờ vòng lặp. Đổi tần suất bằng `INTERVAL_SECONDS` trong workflow.

## Hàng logo nhà đài (Chuối Chiên TV / Gà Vàng TV)

Mỗi API trong `config.json` là một "nhà đài". Các trường mới của mỗi API:

| Trường | Ý nghĩa |
|---|---|
| `id` | mã ổn định, dùng trong `x-source` và tên file (vd `chuoichientv`) |
| `display_name` | tên hiển thị cạnh logo (vd `Chuối Chiên TV`) |
| `logo` | URL `https://...` hoặc đường dẫn trong repo (vd `logos/chuoichientv.png`) |
| `playlist_file` | playlist riêng của nhà đài (mặc định `thethao-<id>.m3u`) |

Nếu `logo` là file chưa tồn tại, `sources.json` để `logo` rỗng và app nên hiện chữ cái đầu (`initial`) như các chip giải đấu.

### `sources.json`

```json
{ "sources": [ {
    "id": "chuoichientv", "display_name": "Chuối Chiên TV", "initial": "C",
    "logo": "logos/chuoichientv.png", "playlist": "thethao-chuoichientv.m3u",
    "ok": true, "stale": false, "error": "",
    "match_count": 2, "stream_count": 2,
    "leagues": [ { "name": "Premier League", "logo": "...",
      "matches": [ { "id": "mhd-match-c1", "title": "Arsenal vs Chelsea",
                     "time": "2026-11-09T21:00:00+07:00", "time_label": "09/11 21:00",
                     "status": "live", "score": "1-0", "stream_count": 1 } ] } ]
} ] }
```

`matches[].id` trùng `tvg-id` trong M3U/EPG. Nếu một API lỗi (vd 403), mục của nó giữ dữ liệu lần trước với `ok=false`, `stale=true`, và file playlist riêng không bị ghi đè.

### Cách app dùng

1. Tải `sources.json` → vẽ một hàng ngang logo + tên cho mỗi `sources[]`, đặt phía trên hàng phân loại môn (Tất cả / Bóng đá / Bóng rổ...).
2. Chưa chọn nhà đài: hiển thị `thethao.m3u` như hiện tại.
3. Bấm logo một nhà đài: hiển thị `leagues[]` → `matches[]` của đúng mục đó (hoặc tải `playlist` riêng của nó để lấy link phát); bấm lại logo để bỏ lọc.
4. Trong `thethao.m3u` mỗi dòng `#EXTINF` có thêm `x-source="<id>"` nếu app muốn lọc ngay trên playlist chung.

## Tự dò Referer theo nguồn phát m3u8

Một số host phát `.m3u8` chặn (HTTP 401/403) nếu thiếu `Referer`. Mỗi lần chạy, script kiểm tra từng host m3u8 chưa có Referer riêng hoặc rule thủ công:

1. Tải thử vài byte đầu của playlist **không** kèm Referer. Phát được thì host không cần Referer, không gắn gì.
2. Nếu bị 401/403, thử lần lượt các Referer lấy từ **chính nguồn của luồng đó** (Referer/Origin trong `headers` của API, `stream_referer` của API) rồi tới `referer_candidates` bạn tự khai báo. Referer đầu tiên phát được sẽ được lưu vào `referer_cache.json` và gắn (`#EXTVLCOPT:http-referrer=...`) cho mọi link của host đó, kể cả FLV.
3. Kết quả được nhớ: host cần Referer kiểm tra lại sau 3 ngày, host không cần sau 7 ngày, host chưa tìm ra Referer thử lại sau 6 giờ. Host không kết nối được thì không lưu, lần sau thử lại.

Ưu tiên: Referer từ API cho từng luồng > `stream_header_rules` (thủ công) > kết quả tự dò.

Log mỗi lần chạy có dòng `[REFERER] host: ...`. Host báo "cần Referer nhưng chưa tìm ra" thì thêm Referer đúng vào `stream_header_rules` (theo host) hoặc `referer_candidates` trong `config.json`. Ép dò lại tất cả: `python scripts/update.py --detect-referers`. Tắt tính năng: `"auto_detect_referers": false`.

Script chỉ thử các Referer đến từ nguồn của luồng hoặc do bạn khai báo; nó không đoán Referer bừa. Chỉ dùng với nguồn bạn được phép truy cập.


## Xuất playlist sang `xiaomi308hd/hinh-nen` (repo công khai)

Repo `sport-api` có thể đặt **Private**. Workflow sẽ tạo playlist trong runner rồi đẩy duy nhất `thethao.m3u` sang repo `xiaomi308hd/hinh-nen` ở thư mục gốc. Repo `sport-api` không cần công khai và không giữ playlist được theo dõi trong Git.

Thiết lập một lần trong repo `sport-api`:

1. Mở **Settings → Secrets and variables → Actions → Secrets → New repository secret**.
2. Tên secret: `M3U_DEST_TOKEN`.
3. Giá trị: Fine-grained Personal Access Token được cấp quyền cho repo `xiaomi308hd/hinh-nen`, với **Contents: Read and write**. Không dán token vào file, workflow, playlist hoặc ứng dụng Android.
4. (Tuỳ chọn) Repo đích mặc định là `xiaomi308hd/hinh-nen`, thư mục gốc. Muốn đổi, vào **Settings → Secrets and variables → Actions → Variables** và tạo biến `M3U_DEST_REPO` (dạng `chủ-sở-hữu/tên-repo`), `M3U_DEST_PATH` (thư mục con, bỏ trống = gốc). Lưu ý đây là **Variables**, không phải Secrets.
5. Chạy workflow **Actions → Update MHD Sports M3U + XMLTV EPG → Run workflow** để kiểm tra lần đầu. Trong log tìm dòng `Đã đẩy thethao.m3u sang …`; nếu thiếu secret hoặc push thất bại, workflow báo đỏ kèm lý do.

URL nguồn công khai dành cho MHD Sports:

`https://raw.githubusercontent.com/xiaomi308hd/hinh-nen/main/thethao.m3u`

Ứng dụng chỉ tải URL Raw công khai này, **không gửi token**. Vì vậy có thể thay hoặc thu hồi token xuất bản trong GitHub Secrets mà không cần cập nhật APK. Điều kiện bắt buộc: repo `xiaomi308hd/hinh-nen` phải để **Public**; chỉ repo chứa mã nguồn `sport-api` được chuyển sang **Private**. Nếu đặt `hinh-nen` thành Private thì ứng dụng Android không thể tải playlist bằng URL Raw công khai.


## Cập nhật: cấu trúc nguồn Chuối Chiên TV, Referer Phá Làng TV, `x-sport`

- **Chuối Chiên TV** có parser riêng (`parser: "chuoichientv"`): đọc các nhóm BLV `blvs`, `blvs_bonglau`, `blvs_nguoitho` (và mọi khoá bắt đầu bằng `blvs`), mỗi BLV lấy luồng từ `streams/links/sources` hoặc khoá chất lượng `hd1/hd2/fhd/...`. `params_variants` gọi API với từng bộ tham số (`type=live`, `type=blv`) rồi gộp trận trùng `id`; một biến thể lỗi không làm hỏng cả nguồn. Nếu có bản ghi mà không đọc được luồng nào, log in ra danh sách khoá của bản ghi đầu để chỉnh parser.
- **Phá Làng TV**: gắn Referer theo thứ tự `referer_by_host` → `stream_referer` → `referer_self_origin`. Referer do API tự trả trong `stream.headers` luôn được giữ. Khi biết đúng trang player của Phá Làng, điền vào `stream_referer` (vd `"https://ten-mien-player/"`).
- **`x-sport`**: mỗi dòng `#EXTINF` có thêm `x-sport="FOOTBALL|BASKETBALL|VOLLEYBALL|TENNIS|BADMINTON"` để app lọc môn chính xác; môn ngoài danh sách (cricket, bóng bầu dục...) thì không ghi.
