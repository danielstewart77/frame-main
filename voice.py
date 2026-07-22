"""Voice — Azure Whisper STT and an Azure neural voice for TTS, called inline.

The Azure endpoints reach the network, so they sit behind this interface with a
fake implementation for offline development. Switching `FRAME_VOICE=fake` to
`azure` and filling in the credentials is the whole of the wiring — no call site
changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


class VoiceError(RuntimeError):
    pass


@dataclass
class Transcript:
    text: str
    duration_ms: int = 0


class VoiceService(Protocol):
    async def transcribe(self, audio: bytes, content_type: str = "audio/ogg") -> Transcript: ...

    async def speak(self, text: str, voice: str | None = None) -> bytes: ...


class AzureVoice:
    """Azure Whisper (STT) + Azure Speech neural TTS. Requires network reachability."""

    def __init__(
        self,
        endpoint: str,
        key: str,
        region: str,
        default_voice: str,
        whisper_deployment: str,
        timeout: float = 60.0,
    ):
        if not endpoint or not key:
            raise VoiceError("AZURE_SPEECH_ENDPOINT and AZURE_SPEECH_KEY are required")
        self.endpoint = endpoint.rstrip("/")
        self.key = key
        self.region = region
        self.default_voice = default_voice
        self.whisper_deployment = whisper_deployment
        self.timeout = timeout

    async def transcribe(self, audio: bytes, content_type: str = "audio/ogg") -> Transcript:
        url = (
            f"{self.endpoint}/openai/deployments/{self.whisper_deployment}"
            "/audio/transcriptions?api-version=2024-06-01"
        )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                url,
                headers={"api-key": self.key},
                files={"file": ("audio", audio, content_type)},
                data={"response_format": "json"},
            )
        if response.status_code >= 400:
            raise VoiceError(f"azure stt {response.status_code}: {response.text[:200]}")
        return Transcript(text=response.json().get("text", ""))

    async def speak(self, text: str, voice: str | None = None) -> bytes:
        voice = voice or self.default_voice
        url = f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1"
        ssml = (
            "<speak version='1.0' xml:lang='en-US'>"
            f"<voice name='{voice}'>{_escape(text)}</voice></speak>"
        )
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                url,
                headers={
                    "Ocp-Apim-Subscription-Key": self.key,
                    "Content-Type": "application/ssml+xml",
                    "X-Microsoft-OutputFormat": "audio-24khz-48kbitrate-mono-mp3",
                },
                content=ssml.encode("utf-8"),
            )
        if response.status_code >= 400:
            raise VoiceError(f"azure tts {response.status_code}: {response.text[:200]}")
        return response.content


class FakeVoice:
    """Offline stand-in — round-trips text so surfaces can be driven end to end."""

    PREFIX = b"FAKE-TTS:"

    def __init__(self, default_voice: str = "fake-voice"):
        self.default_voice = default_voice
        self.spoken: list[tuple[str, str]] = []
        self.transcribed: list[int] = []

    async def transcribe(self, audio: bytes, content_type: str = "audio/ogg") -> Transcript:
        self.transcribed.append(len(audio))
        if audio.startswith(self.PREFIX):
            return Transcript(text=audio[len(self.PREFIX) :].decode("utf-8", "replace"))
        return Transcript(text=f"[fake transcript of {len(audio)} bytes]")

    async def speak(self, text: str, voice: str | None = None) -> bytes:
        voice = voice or self.default_voice
        self.spoken.append((voice, text))
        return self.PREFIX + text.encode("utf-8")


def get_voice(kind: str, settings) -> VoiceService:
    if kind == "azure":
        return AzureVoice(
            endpoint=settings.azure_speech_endpoint,
            key=settings.azure_speech_key,
            region=settings.azure_speech_region,
            default_voice=settings.azure_speech_voice,
            whisper_deployment=settings.azure_whisper_deployment,
        )
    if kind == "fake":
        return FakeVoice(settings.azure_speech_voice)
    raise ValueError(f"unknown voice backend: {kind!r}")


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
