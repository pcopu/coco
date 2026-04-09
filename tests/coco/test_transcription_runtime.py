"""Tests for transcription runtime profile resolution."""

import coco.transcription as transcription


def test_default_transcription_profile_is_compatible():
    assert transcription.get_default_transcription_profile() == "compatible"


def test_resolve_transcription_runtime_compatible_prefers_portable_cpu(monkeypatch):
    monkeypatch.setattr(transcription, "_cuda_device_count", lambda: 1)

    runtime = transcription.resolve_transcription_runtime("compatible")

    assert runtime.profile == "compatible"
    assert runtime.gpu_available is True
    assert runtime.device == "cpu"
    assert runtime.compute_type == "int8"
    assert runtime.model_name == "base"


def test_resolve_transcription_runtime_auto_falls_back_to_compatible(monkeypatch):
    monkeypatch.setattr(transcription, "_cuda_device_count", lambda: 1)
    monkeypatch.setattr(
        transcription,
        "_supported_compute_types",
        lambda device: {"float16", "int8_float16"} if device == "cuda" else {"int8"},
    )

    runtime = transcription.resolve_transcription_runtime("auto")

    assert runtime.profile == "compatible"
    assert runtime.gpu_available is True
    assert runtime.device == "cpu"
    assert runtime.compute_type == "int8"
    assert runtime.model_name == "base"
