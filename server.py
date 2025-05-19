 # Import các thư viện
from fastapi import FastAPI, UploadFile, File, HTTPException
from azure.storage.blob import BlobServiceClient
import os
import uuid
import ffmpeg
import json
from pathlib import Path
import whisper
import nest_asyncio
import uvicorn
from pyngrok import ngrok
import warnings

# Bỏ qua FutureWarning từ torch.load
warnings.filterwarnings("ignore", category=FutureWarning)

# Cấu hình FastAPI
app = FastAPI()
# Cấu hình Azure Blob Storage
AZURE_STORAGE_CONNECTION_STRING = "YOUR_AZURE_STORAGE_CONNECTION_STRING"
CONTAINER_NAME = "wdp"

blob_service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
container_client = blob_service_client.get_container_client(CONTAINER_NAME)

# Thư mục tạm
TEMP_DIR = "temp"
OUTPUT_DIR = "output"
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Load Whisper model
model = whisper.load_model("base")
@app.post("/upload")
async def upload_video(file: UploadFile = File(...), content_id: str = None):
    file_extension = Path(file.filename).suffix.lower()
    if file_extension != ".mp4":
        raise HTTPException(status_code=400, detail="Only MP4 files are allowed")
    video_filename = f"{uuid.uuid4()}.mp4"
    temp_video_path = os.path.join(TEMP_DIR, video_filename)
    with open(temp_video_path, "wb") as buffer:
        buffer.write(await file.read())
    video_id = str(uuid.uuid4())
    output_m3u8_path = os.path.join(OUTPUT_DIR, f"{video_id}.m3u8")
    output_ts_pattern = os.path.join(OUTPUT_DIR, f"{video_id}_%03d.ts")

    (
        ffmpeg.input(temp_video_path)
        .output(output_m3u8_path, format="hls", start_number=0, hls_time=10, hls_list_size=0, hls_segment_filename=output_ts_pattern)
        .run()
    )
    hls_files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith(video_id) and f.endswith(".ts")])
    audio_path = os.path.join(OUTPUT_DIR, f"{video_id}.mp3")
    ffmpeg.input(temp_video_path).output(audio_path, format="mp3", acodec="mp3").run()
    result = model.transcribe(audio_path)
    subtitles = []
    segment_duration = 10
    for i, segment in enumerate(result["segments"]):
        start_time = segment["start"]
        end_time = segment["end"]
        text = segment["text"]
        ts_index = int(start_time // segment_duration)
        if ts_index >= len(hls_files):
            break
        ts_file = hls_files[ts_index]
        subtitles.append({
            "segment": ts_file,
            "start": start_time,
            "end": end_time,
            "text": text
        })
    subtitles_path = os.path.join(OUTPUT_DIR, f"{video_id}.json")
    with open(subtitles_path, "w", encoding="utf-8") as f:
        json.dump(subtitles, f, ensure_ascii=False, indent=4)
    ts_urls = []
    for hls_file in hls_files:
        blob_name = f"{video_id}/{hls_file}"
        blob_client = container_client.get_blob_client(blob_name)
        with open(os.path.join(OUTPUT_DIR, hls_file), "rb") as data:
            blob_client.upload_blob(data, overwrite=True, content_type="video/MP2T")
        ts_urls.append(f"https://sdnmma.blob.core.windows.net/{CONTAINER_NAME}/{video_id}/{hls_file}")
    m3u8_blob_name = f"{video_id}/{video_id}.m3u8"
    m3u8_blob_client = container_client.get_blob_client(m3u8_blob_name)
    with open(output_m3u8_path, "rb") as data:
        m3u8_blob_client.upload_blob(data, overwrite=True, content_type="application/vnd.apple.mpegurl")
    m3u8_url = f"https://sdnmma.blob.core.windows.net/{CONTAINER_NAME}/{video_id}/{video_id}.m3u8"
    subtitles_blob_name = f"{video_id}/{video_id}.json"
    subtitles_blob_client = container_client.get_blob_client(subtitles_blob_name)
    with open(subtitles_path, "rb") as data:
        subtitles_blob_client.upload_blob(data, overwrite=True, content_type="application/json")
    subtitles_url = f"https://sdnmma.blob.core.windows.net/{CONTAINER_NAME}/{video_id}/{video_id}.json"
    os.remove(temp_video_path)
    os.remove(audio_path)
    for hls_file in hls_files:
        os.remove(os.path.join(OUTPUT_DIR, hls_file))
    os.remove(output_m3u8_path)
    os.remove(subtitles_path)
    return {
        "video_id": video_id,
        "fileUrl": m3u8_url,
        "segments": ts_urls,
        "subtitlesUrl": subtitles_url,
        "message": "Upload, HLS conversion, and transcription successful"
    }
@app.put("/update_script/{video_id}")
async def update_script(video_id: str, file: UploadFile = File(...)):
    subtitles_blob_name = f"{video_id}/{video_id}.json"
    subtitles_blob_client = container_client.get_blob_client(subtitles_blob_name)
    subtitles_data = await file.read()
    subtitles_blob_client.upload_blob(subtitles_data, overwrite=True, content_type="application/json")
    return {"message": "Subtitles updated successfully"}

@app.put("/update_video/{video_id}")
async def update_video(video_id: str, file: UploadFile = File(...)):
    file_extension = Path(file.filename).suffix.lower()
    if file_extension != ".mp4":
        raise HTTPException(status_code=400, detail="Only MP4 files are allowed")

    # Tạo thư mục tạm nếu chưa tồn tại
    os.makedirs(TEMP_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Lưu video tạm thời
    temp_video_path = os.path.join(TEMP_DIR, f"{video_id}.mp4")
    with open(temp_video_path, "wb") as buffer:
        buffer.write(await file.read())

    # Chuyển đổi video sang HLS
    output_m3u8_path = os.path.join(OUTPUT_DIR, f"{video_id}.m3u8")
    output_ts_pattern = os.path.join(OUTPUT_DIR, f"{video_id}_%03d.ts")

    (
        ffmpeg.input(temp_video_path)
        .output(output_m3u8_path, format="hls", start_number=0, hls_time=10, hls_list_size=0, hls_segment_filename=output_ts_pattern)
        .run()
    )

    # Lấy danh sách các file .ts đã tạo
    hls_files = sorted([f for f in os.listdir(OUTPUT_DIR) if f.startswith(video_id) and f.endswith(".ts")])

    # Tải các file .ts lên Azure Blob Storage
    ts_urls = []
    for hls_file in hls_files:
        blob_name = f"{video_id}/{hls_file}"
        blob_client = container_client.get_blob_client(blob_name)
        with open(os.path.join(OUTPUT_DIR, hls_file), "rb") as data:
            blob_client.upload_blob(data, overwrite=True, content_type="video/MP2T")
        ts_urls.append(f"https://sdnmma.blob.core.windows.net/{CONTAINER_NAME}/{video_id}/{hls_file}")

    # Tải file .m3u8 lên Azure Blob Storage
    m3u8_blob_name = f"{video_id}/{video_id}.m3u8"
    m3u8_blob_client = container_client.get_blob_client(m3u8_blob_name)
    with open(output_m3u8_path, "rb") as data:
        m3u8_blob_client.upload_blob(data, overwrite=True, content_type="application/vnd.apple.mpegurl")
    m3u8_url = f"https://sdnmma.blob.core.windows.net/{CONTAINER_NAME}/{video_id}/{video_id}.m3u8"

    # Xóa các file tạm
    os.remove(temp_video_path)
    for hls_file in hls_files:
        os.remove(os.path.join(OUTPUT_DIR, hls_file))
    os.remove(output_m3u8_path)

    return {
        "video_id": video_id,
        "fileUrl": m3u8_url,
        "segments": ts_urls,
        "message": "Video updated and HLS conversion successful"
    }
    file_extension = Path(file.filename).suffix.lower()
    if file_extension != ".mp4":
        raise HTTPException(status_code=400, detail="Only MP4 files are allowed")
    video_blob_name = f"{video_id}/{video_id}.mp4"
    video_blob_client = container_client.get_blob_client(video_blob_name)
    video_data = await file.read()
    video_blob_client.upload_blob(video_data, overwrite=True, content_type="video/mp4")
    return {"message": "Video updated successfully"}

@app.delete("/delete_video/{video_id}")
async def delete_video(video_id: str):
    blobs = container_client.list_blobs(name_starts_with=f"{video_id}/")
    for blob in blobs:
        blob_client = container_client.get_blob_client(blob.name)
        blob_client.delete_blob()
    return {"message": "Video and related files deleted successfully"}

@app.delete("/delete_script/{video_id}")
async def delete_script(video_id: str):
    # Tạo tên blob cho file script (subtitles)
    subtitles_blob_name = f"{video_id}/{video_id}.json"
    subtitles_blob_client = container_client.get_blob_client(subtitles_blob_name)

    # Kiểm tra xem file script có tồn tại không
    if not subtitles_blob_client.exists():
        raise HTTPException(status_code=404, detail="Script not found")

    # Xóa file script
    subtitles_blob_client.delete_blob()

    return {"message": "Script deleted successfully"}
@app.get("/")
def root():
    return {"message": "API is running"}
ngrok.set_auth_token("YOUR_NGROK_AUTH_TOKEN")  # Thay bằng token của bạn

# Áp dụng nest_asyncio để chạy uvicorn trong Colab
nest_asyncio.apply()

# Hàm chạy server FastAPI
def run():
    uvicorn.run(app, host="0.0.0.0", port=8000)

# Chạy server trong một thread riêng
import threading
thread = threading.Thread(target=run)
thread.start()

# Mở tunnel bằng ngrok
public_url = ngrok.connect(8000)
print(f"Public URL: {public_url}")