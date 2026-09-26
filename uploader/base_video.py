from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path


class BaseVideoUploader:
    SUPPORTED_VIDEO_EXTENSIONS = {
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
        ".m4v",
        ".webm",
        ".flv",
        ".wmv",
    }
    SUPPORTED_IMAGE_EXTENSIONS = {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".bmp",
    }
    MIN_SCHEDULE_LEAD_TIME = timedelta(hours=2)

    @classmethod
    def validate_video_file(cls, file_path: str | Path) -> Path:
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"视频文件不存在: {path}")
        if not path.is_file():
            raise ValueError(f"视频路径不是文件: {path}")
        if path.suffix.lower() not in cls.SUPPORTED_VIDEO_EXTENSIONS:
            raise ValueError(
                f"不支持的视频格式: {path.suffix}，当前支持: {', '.join(sorted(cls.SUPPORTED_VIDEO_EXTENSIONS))}"
            )
        # MP4 无 moov 原子 = FFmpeg 未写完/被中断的残缺文件；抖音会静默拒绝，页面一直停在拖拽上传区
        if path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise ValueError(f"无法读取视频文件: {path} ({exc})") from exc
            if len(data) < 64:
                raise ValueError(f"视频文件过小或损坏: {path}（{len(data)} bytes）")
            if b"moov" not in data:
                raise ValueError(
                    f"视频文件不完整（缺少 moov，多为二创中断残留）: {path}。"
                    "请重新二创生成成片后再发布，勿继续空等上传页。"
                )

        return path

    @classmethod
    def validate_image_file(cls, file_path: str | Path) -> Path:
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"图片文件不存在: {path}")
        if not path.is_file():
            raise ValueError(f"图片路径不是文件: {path}")
        if path.suffix.lower() not in cls.SUPPORTED_IMAGE_EXTENSIONS:
            raise ValueError(
                f"不支持的图片格式: {path.suffix}，当前支持: {', '.join(sorted(cls.SUPPORTED_IMAGE_EXTENSIONS))}"
            )
        return path

    @classmethod
    def validate_publish_date(cls, publish_date: datetime | int | None) -> datetime | int:
        if publish_date in (None, 0):
            return 0

        if not isinstance(publish_date, datetime):
            raise TypeError("publish_date 必须是 datetime 类型或 0")

        now = datetime.now(tz=publish_date.tzinfo) if publish_date.tzinfo else datetime.now()
        if publish_date <= now:
            raise ValueError("定时发布时间必须晚于当前时间")

        min_publish_time = now + cls.MIN_SCHEDULE_LEAD_TIME
        if publish_date <= min_publish_time:
            raise ValueError("定时发布时间必须大于当前时间 2 小时")

        return publish_date
