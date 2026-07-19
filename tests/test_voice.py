import pytest

import voice as voice_mod
from config import Settings


@pytest.mark.asyncio
async def test_fake_voice_round_trips_text():
    fake = voice_mod.FakeVoice()
    audio = await fake.speak("hold my beer")
    assert (await fake.transcribe(audio)).text == "hold my beer"
    assert fake.spoken == [("fake-voice", "hold my beer")]


@pytest.mark.asyncio
async def test_fake_transcribe_handles_opaque_audio():
    fake = voice_mod.FakeVoice()
    result = await fake.transcribe(b"\x00\x01\x02")
    assert "3 bytes" in result.text


def test_get_voice_selects_the_backend_by_name():
    settings = Settings(voice="fake")
    assert isinstance(voice_mod.get_voice("fake", settings), voice_mod.FakeVoice)
    with pytest.raises(ValueError):
        voice_mod.get_voice("carrier-pigeon", settings)


def test_azure_backend_requires_credentials():
    settings = Settings(voice="azure", azure_speech_endpoint="", azure_speech_key="")
    with pytest.raises(voice_mod.VoiceError):
        voice_mod.get_voice("azure", settings)


def test_azure_backend_constructs_when_credentials_are_present():
    settings = Settings(
        voice="azure",
        azure_speech_endpoint="https://example.openai.azure.com/",
        azure_speech_key="k",
        azure_speech_region="eastus",
    )
    service = voice_mod.get_voice("azure", settings)
    assert isinstance(service, voice_mod.AzureVoice)
    assert service.endpoint == "https://example.openai.azure.com"


def test_ssml_escapes_markup_in_the_text():
    assert voice_mod._escape('a & b < c "d"') == "a &amp; b &lt; c &quot;d&quot;"
