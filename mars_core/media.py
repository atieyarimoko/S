import io
import math
import re
import shutil
import subprocess
import tempfile
import warnings
from pathlib import Path

from PIL import Image


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm"}
MEDIA_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_VIDEO_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_VIDEO_SECONDS = 30 * 60
MAX_VIDEO_FRAMES = 8
MAX_VIDEO_EDGE = 1280


def media_limit(suffix):
    suffix = suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return MAX_IMAGE_BYTES
    if suffix in VIDEO_SUFFIXES:
        return MAX_VIDEO_BYTES
    raise ValueError("対応するメディア形式は PNG、JPG、JPEG、WEBP、MP4、MOV、WEBM です")


def validate_image(data, suffix):
    if suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError("対応する画像形式は PNG、JPG、JPEG、WEBP です")
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("画像は12 MB以内にしてください")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                expected_format = {
                    ".png": "PNG",
                    ".jpg": "JPEG",
                    ".jpeg": "JPEG",
                    ".webp": "WEBP",
                }[suffix.lower()]
                if image.format != expected_format:
                    raise ValueError("画像の内容と拡張子が一致しません")
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise ValueError("画像の画素数が上限（4000万画素）を超えています")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.thumbnail((2048, 2048))
                if suffix.lower() in {".png"}:
                    output_format = "PNG"
                else:
                    output_format = "JPEG"
                output = io.BytesIO()
                if output_format == "PNG":
                    image.save(output, format="PNG", optimize=True)
                    mime_type = "image/png"
                else:
                    image.convert("RGB").save(output, format="JPEG", quality=90, optimize=True)
                    mime_type = "image/jpeg"
    except ValueError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError) as error:
        raise ValueError("画像を読み取れません。対応形式の画像か確認してください") from error
    return output.getvalue(), mime_type


def _video_demuxer(suffix):
    return "matroska" if suffix.lower() == ".webm" else "mov"


def _video_duration(path):
    executable = shutil.which("ffprobe")
    if not executable:
        raise ValueError("動画分析には ffprobe と ffmpeg が必要です。FFmpegをインストールしてください")
    try:
        result = subprocess.run(
            [
                executable,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                "-protocol_whitelist", "file,data",
                "-f", _video_demuxer(path.suffix),
                "-max_alloc", "50000000",
                "-probesize", "10000000",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        duration = float(result.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise ValueError("動画の長さを読み取れません。対応する動画ファイルか確認してください") from error
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_VIDEO_SECONDS:
        raise ValueError("動画の長さは30分以内にしてください")
    return duration


def _requested_timestamps(question, duration):
    timestamps = []
    for hours, minutes, seconds in re.findall(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})", question):
        value = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
        if value < duration:
            timestamps.append(float(value))
    seconds = [float(value) for value in re.findall(r"(\d+(?:\.\d+)?)\s*秒", question)]
    if seconds and re.search(r"冒頭|最初|はじめ|前半|beginning|first", question, re.IGNORECASE):
        end = min(max(seconds[0], 0.1), duration)
        count = min(MAX_VIDEO_FRAMES, max(2, math.ceil(end / 1.5) + 1))
        timestamps.extend(end * index / (count - 1) for index in range(count))
    elif len(seconds) >= 2:
        start, end = sorted(seconds[:2])
        start = min(start, duration - 0.01)
        end = min(max(end, start + 0.01), duration)
        count = min(MAX_VIDEO_FRAMES, max(2, math.ceil((end - start) / 2) + 1))
        timestamps.extend(start + (end - start) * index / (count - 1) for index in range(count))
    elif seconds:
        center = min(seconds[0], duration - 0.01)
        timestamps.extend((max(0, center - 0.5), center, min(duration - 0.01, center + 0.5)))
    if timestamps:
        return sorted(set(round(value, 2) for value in timestamps))[:MAX_VIDEO_FRAMES]
    count = min(MAX_VIDEO_FRAMES, max(2, math.ceil(duration / 15) + 1))
    return [round((duration - 0.05) * index / (count - 1), 2) for index in range(count)]


def extract_video_frames(data, suffix, question=""):
    if suffix.lower() not in VIDEO_SUFFIXES:
        raise ValueError("対応する動画形式は MP4、MOV、WEBM です")
    if not isinstance(data, bytes) or not data or len(data) > MAX_VIDEO_BYTES:
        raise ValueError("動画は25 MB以内にしてください")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("動画分析には ffprobe と ffmpeg が必要です。FFmpegをインストールしてください")
    with tempfile.TemporaryDirectory(prefix="mars-video-") as directory:
        source = Path(directory) / f"source{suffix.lower()}"
        source.write_bytes(data)
        source.chmod(0o600)
        duration = _video_duration(source)
        frames = []
        for index, timestamp in enumerate(_requested_timestamps(question, duration)):
            output = Path(directory) / f"frame-{index:02d}.jpg"
            try:
                subprocess.run(
                    [
                        ffmpeg, "-nostdin", "-v", "error", "-threads", "1",
                        "-max_alloc", "50000000", "-probesize", "10000000",
                        "-protocol_whitelist", "file,data", "-ss", f"{timestamp:.2f}",
                        "-f", _video_demuxer(suffix), "-i", str(source),
                        "-an", "-frames:v", "1", "-vf",
                        f"scale={MAX_VIDEO_EDGE}:{MAX_VIDEO_EDGE}:force_original_aspect_ratio=decrease",
                        "-q:v", "3",
                        "-y", str(output),
                    ],
                    capture_output=True,
                    timeout=12,
                    check=True,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise ValueError(f"{timestamp:.2f}秒の動画フレームを抽出できません") from error
            try:
                frame_data = output.read_bytes()
                encoded, _ = validate_image(frame_data, ".jpg")
            except (OSError, ValueError) as error:
                raise ValueError(f"{timestamp:.2f}秒の動画フレームを読み取れません") from error
            frames.append((timestamp, encoded))
        return duration, frames


def model_images(file_store, paths, question):
    items = []
    for path in paths:
        suffix = Path(path).suffix.lower()
        data = file_store.read_bytes(path)
        label = Path(path).name
        if suffix in IMAGE_SUFFIXES:
            encoded, mime_type = validate_image(data, suffix)
            items.append({"label": label, "data": encoded, "mime_type": mime_type})
        elif suffix in VIDEO_SUFFIXES:
            duration, frames = extract_video_frames(data, suffix, question)
            for timestamp, frame in frames:
                items.append({
                    "label": f"{label} · {timestamp:.2f}秒 / {duration:.2f}秒",
                    "data": frame,
                    "mime_type": "image/jpeg",
                })
        else:
            raise ValueError(f"画像・動画ではありません: {label}")
    return items
