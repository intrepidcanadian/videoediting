"""Upload magic-byte sniffing — prevents disguised uploads from reaching disk."""

import server


def test_png():
    assert server._sniff_kind(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16) == "image"


def test_jpeg():
    assert server._sniff_kind(b"\xff\xd8\xff\xe0" + b"\x00" * 16) == "image"


def test_gif():
    assert server._sniff_kind(b"GIF89a" + b"\x00" * 16) == "image"


def test_webp_is_image_not_video():
    assert server._sniff_kind(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8) == "image"


def test_wav_is_audio():
    assert server._sniff_kind(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 8) == "audio"


def test_avi_is_video():
    assert server._sniff_kind(b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 8) == "video"


def test_mp4():
    # ISO base media: first 4 bytes are box size, then "ftyp" at offset 4.
    assert server._sniff_kind(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 16) == "video"


def test_webm():
    assert server._sniff_kind(b"\x1a\x45\xdf\xa3" + b"\x00" * 16) == "video"


def test_mp3_with_id3():
    assert server._sniff_kind(b"ID3\x03\x00" + b"\x00" * 16) == "audio"


def test_mp3_raw_frame():
    assert server._sniff_kind(b"\xff\xfb\x90\x00" + b"\x00" * 16) == "audio"


def test_flac():
    assert server._sniff_kind(b"fLaC" + b"\x00" * 16) == "audio"


def test_ogg():
    assert server._sniff_kind(b"OggS" + b"\x00" * 16) == "audio"


def test_empty_is_unknown():
    assert server._sniff_kind(b"") == "unknown"


def test_executable_is_unknown():
    assert server._sniff_kind(b"MZ\x90\x00" + b"\x00" * 16) == "unknown"


def test_shell_script_is_unknown():
    assert server._sniff_kind(b"#!/bin/sh\nrm -rf /\n") == "unknown"


def test_html_is_unknown():
    assert server._sniff_kind(b"<!DOCTYPE html><html>...") == "unknown"


def test_require_kind_raises_on_mismatch():
    from fastapi import HTTPException
    import pytest
    with pytest.raises(HTTPException) as exc_info:
        server._require_kind(b"#!/bin/sh", "image", field="asset")
    assert exc_info.value.status_code == 400
    assert "image" in exc_info.value.detail
