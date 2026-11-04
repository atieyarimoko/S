import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from mars_core.media import (
    MAX_IMAGE_BYTES,
    _requested_timestamps,
    extract_video_frames,
    validate_image,
)


def image_bytes(image_format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (32, 24), "navy").save(output, format=image_format)
    return output.getvalue()


class MediaTests(unittest.TestCase):
    def test_validate_image_checks_format_and_prepares_model_input(self):
        data, mime_type = validate_image(image_bytes(), ".png")

        self.assertEqual(mime_type, "image/png")
        self.assertEqual(Image.open(io.BytesIO(data)).size, (32, 24))

    def test_validate_image_rejects_mismatched_format_corruption_and_oversize(self):
        with self.assertRaisesRegex(ValueError, "拡張子"):
            validate_image(image_bytes(), ".jpg")
        with self.assertRaisesRegex(ValueError, "読み取れません"):
            validate_image(b"not an image", ".png")
        with self.assertRaisesRegex(ValueError, "12 MB"):
            validate_image(b"x" * (MAX_IMAGE_BYTES + 1), ".png")

    def test_video_timestamps_follow_question_range_and_overview(self):
        self.assertEqual(_requested_timestamps("冒頭3秒を見て", 10), [0, 1.5, 3])
        self.assertEqual(_requested_timestamps("10秒から20秒", 25), [10, 12, 14, 16, 18, 20])
        overview = _requested_timestamps("全体を要約", 90)
        self.assertEqual(len(overview), 7)
        self.assertAlmostEqual(overview[0], 0)
        self.assertAlmostEqual(overview[-1], 89.95)

    def test_video_frame_extraction_records_timestamps_and_caps_frames(self):
        jpeg = image_bytes("JPEG")

        def fake_run(command, **_kwargs):
            if command[0].endswith("ffmpeg"):
                Path(command[-1]).write_bytes(jpeg)

        with patch("mars_core.media.shutil.which", return_value="/usr/bin/ffmpeg"), patch(
            "mars_core.media._video_duration", return_value=10
        ), patch("mars_core.media.subprocess.run", side_effect=fake_run) as run:
            duration, frames = extract_video_frames(b"ftyp", ".mp4", "冒頭3秒を分析")

        self.assertEqual(duration, 10)
        self.assertEqual([timestamp for timestamp, _ in frames], [0, 1.5, 3])
        self.assertEqual(run.call_count, 3)
        self.assertTrue(all(Image.open(io.BytesIO(frame)).format == "JPEG" for _, frame in frames))

    def test_video_analysis_reports_missing_ffmpeg(self):
        with patch("mars_core.media.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "ffprobe と ffmpeg"):
                extract_video_frames(b"ftyp", ".mp4", "要約して")


if __name__ == "__main__":
    unittest.main()
