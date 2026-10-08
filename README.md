# dieu_mist_compositor

Bộ ghép sương trắng độc lập, viết mới bằng Python stdlib và FFmpeg. Không phụ thuộc AYS, không chứa media bản quyền. Chưa tích hợp hoặc kiểm chứng trực tiếp với OpenMontage; API dưới đây là hợp đồng tích hợp tổng quát.

## Cài đặt

Yêu cầu Python >= 3.10, FFmpeg/ffprobe trong PATH, encoder `libx264` và `aac`.

```bash
sudo apt-get install ffmpeg
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
mist-compositor --help
```

Không có thư viện Python phụ thuộc lúc chạy. Cũng có thể chạy trực tiếp `python3 -m mist_compositor` từ thư mục repo.

## CLI

```bash
mist-compositor render --input input.mp4 --mist mist.mp4   --intensity 50 --output output.mp4 > report.json

# 0: bỏ hoàn toàn nhánh sương, không cần --mist (vẫn encode video)
mist-compositor render --input input.mp4 --intensity 0 --output clean.mp4
```

`--intensity` là số hữu hạn từ 0 đến 100. Mặc định từ chối ghi đè; chỉ `--overwrite` mới thay thế file đích. Thư mục đích phải tồn tại; đầu ra phải là `.mp4`. File tạm cùng thư mục được kiểm tra trước khi công bố bằng thao tác filesystem nguyên tử; lỗi encode/probe/check không để lại MP4 đích dở dang và giữ nguyên file cũ. Chế độ không ghi đè cần filesystem hỗ trợ hard link. Kill cưỡng bức/mất điện có thể để lại `.mist-*`, không phải file đích thành công.

STDOUT trả JSON gồm probe nguồn/mist/đầu ra, argv FFmpeg thực thi, filtergraph và kiểm tra thời lượng/FPS/kích thước/audio/codec. `command` lưu đúng đường dẫn file tạm đã dùng (file tạm được dọn sau render); `output` là đường dẫn cuối. Lỗi render trả JSON trên STDERR và exit 1; lỗi cú pháp argparse exit 2. Redirect report là thao tác shell riêng, không phải giao dịch nguyên tử cùng MP4.

## Công thức và màu

- Lấy video sương, đổi grayscale rồi chuẩn hóa alpha full-range: đen → trong suốt, trắng → đục.
- Nhân alpha với `intensity / 100`.
- Tạo canvas trắng → `alphamerge` với alpha → `overlay` lên nguồn.
- **Không sử dụng screen blend**, không lấy chroma của video sương làm màu lớp phủ.
- Video sương được scale đúng kích thước nguồn, đổi về FPS hữu tỉ nguồn, reset timestamp và lặp tới hết nguồn.
- Nguồn được reset timestamp video về 0; FPS đầu ra là CFR bằng `avg_frame_rate` hữu tỉ nguồn. Nguồn VFR sẽ được lấy mẫu lại, không giữ lịch timestamp VFR từng frame.
- Đầu ra H.264, yuv420p limited range, BT.709 SDR. Kích thước phải chẵn, pixel vuông, không rotation metadata; chuẩn hóa đầu vào trước nếu không đáp ứng.
- Chỉ nhận BT.709 SDR hoặc video thiếu tag màu (giả định BT.709 SDR được ghi rõ trong report). Từ chối PQ/HLG/BT.2020, metadata HDR và màu SDR khác đã khai báo; **không tone-map, không giả mạo HDR thành SDR bằng đổi tag**. Không thể phát hiện HDR bị xóa mọi metadata, người gọi chịu trách nhiệm với giả định màu của input untagged.
- Audio: map tùy chọn mọi track audio của nguồn (`0:a?`), không lấy âm thanh mist. Encode AAC 192 kb/s mỗi track, **không bit-exact**. Giữ độ lệch audio tương đối với timestamp bắt đầu video, cắt phần trước video, pad im lặng nếu audio ngắn và giới hạn tới thời lượng nguồn. Metadata/chapter không sao chép.

## API và OpenMontage

```python
from mist_compositor import render, probe, build_filtergraph

report = render('input.mp4', 'output.mp4', mist='mist.mp4', intensity=50)
assert report['checks']['passed']

# Bộ điều phối khác có thể dùng graph trực tiếp:
graph = build_filtergraph(1920, 1080, '30000/1001', 12.012, 50)
```

`render(input, output, *, intensity=50, mist=None, overwrite=False) -> dict` thực hiện toàn bộ validate/encode/verify/publish. `probe(path) -> dict` trả JSON ffprobe sau kiểm tra contract màu/hình; `build_filtergraph(width, height, fps, duration, intensity) -> str` là hàm thuần trả graph.

Để gắn builder vào OpenMontage, adapter phải cấp nguồn `[0:v:0]`, sương `[1:v:0]` với `-stream_loop -1` đặt **trước** `-i mist`, `-noautorotate` cho input, map `[outv]`, và giới hạn output `-t duration`. Nhánh intensity 0 chỉ cần input 0. Adapter tự validate màu/geometry bằng `probe`, xử lý audio như `render`, cấu hình encoder/tag BT.709 và transaction file đích. Không đưa input HDR trực tiếp vào builder. Builder không chạy ffprobe, không quản lý filesystem/audio. Không có tên node hoặc path legacy hardcode.

## Kiểm thử

```bash
python3 -m unittest discover -s tests -v
```

Tests tạo fixture lavfi trong thư mục tạm và tự xóa: nguồn xám, mask ba vùng đen/xám/trắng ngắn hơn nguồn, tone sin 440 Hz. Kiểm tra 0/50/100 với/cả không audio, pixel đầu/cuối (không magenta), FPS 30000/1001, thời lượng, correlation audio sau AAC, HDR, input lỗi, ghi đè và dọn file lỗi. Xem `docs/test-evidence.txt` cho kết quả chạy thật. Template GitHub Actions ở `docs/github-actions-tests.yml.template` cài FFmpeg và chạy unittest. Token upload hiện tại thiếu scope `workflow`, nên template chưa được kích hoạt. Để bật CI, dùng quyền GitHub phù hợp và chuyển template thành `.github/workflows/tests.yml`, rồi commit/push. Chưa có kết quả CI remote.
