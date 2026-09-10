"""Plain dataclasses passed between pipeline stages."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

ALLOWED_PRIVACY = ("private", "unlisted")  # "public" is deliberately not allowed


@dataclass
class VideoRequest:
    """One topic from topics/queue.yaml, ready to send to MoneyPrinterTurbo."""

    topic_id: str
    subject: str
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    category_id: str = "22"  # People & Blogs
    privacy_status: str = "private"
    made_for_kids: bool = False
    contains_synthetic_media: bool = True  # AI-generated -> always disclose
    # Fields forwarded verbatim to POST /api/v1/videos (TaskVideoRequest)
    video_aspect: str = "9:16"
    voice_name: str = "en-US-JennyNeural-Female"  # Edge TTS voice
    paragraph_number: int = 1
    video_source: str = "pexels"
    subtitle_enabled: bool = True
    bgm_type: str = ""  # "" = no background music (bundled songs removed)
    video_language: str = "en"
    extra_params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.topic_id or not self.subject:
            raise ValueError("topic_id and subject are required")
        if self.privacy_status not in ALLOWED_PRIVACY:
            raise ValueError(
                f"privacy_status={self.privacy_status!r} not allowed; use one of {ALLOWED_PRIVACY}"
            )

    def to_mpt_payload(self) -> dict[str, Any]:
        payload = {
            "video_subject": self.subject,
            "video_aspect": self.video_aspect,
            "voice_name": self.voice_name,
            "paragraph_number": self.paragraph_number,
            "video_source": self.video_source,
            "subtitle_enabled": self.subtitle_enabled,
            "bgm_type": self.bgm_type,
            "video_language": self.video_language,
            "video_count": 1,
        }
        payload.update(self.extra_params)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VideoRequest":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        extra = {k: v for k, v in data.items() if k not in known}
        if extra:
            kwargs["extra_params"] = {**kwargs.get("extra_params", {}), **extra}
        return cls(**kwargs)


@dataclass
class VideoResult:
    """A rendered video plus the metadata needed to upload it (sidecar JSON)."""

    request: VideoRequest
    task_id: str
    video_path: str
    sha256: str = ""
    script: str = ""
    terms: list[str] = field(default_factory=list)
    created_at: str = ""
    youtube_video_id: str = ""
    uploaded_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VideoResult":
        data = dict(data)
        data["request"] = VideoRequest.from_dict(data["request"])
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
