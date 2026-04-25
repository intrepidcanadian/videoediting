"""Credential redaction — protects error messages surfaced to users / logs."""

import textutils


def test_redact_bearer_token():
    body = 'Authorization: Bearer sk-ant-abc123def456ghijklmnopqrstuvwxyz'
    out = textutils.safe_err_body(body)
    assert "sk-ant" not in out
    assert "[REDACTED" in out


def test_redact_xi_api_key_json():
    body = '{"xi-api-key":"abcdef0123456789abcdef0123456789"}'
    out = textutils.safe_err_body(body)
    assert "abcdef0123456789abcdef0123456789" not in out


def test_redact_ark_key_anywhere():
    body = 'received request with ark-abcdefghijklmnopqrstuvwxyz in header'
    out = textutils.safe_err_body(body)
    assert "ark-abcdefghij" not in out


def test_redact_authorization_json():
    body = '{"Authorization": "Bearer verylongtokenabcdefghijklmn"}'
    out = textutils.safe_err_body(body)
    assert "verylongtokenabc" not in out


def test_redact_preserves_legitimate_content():
    body = "Seedance task status=failed duration=13s"
    out = textutils.safe_err_body(body)
    assert "status=failed" in out
    assert "duration=13s" in out


def test_clamps_long_output():
    body = "x" * 5000
    out = textutils.safe_err_body(body, max_len=100)
    assert len(out) <= 100


def test_strips_control_chars():
    body = "hello\x00\x01world"
    out = textutils.safe_err_body(body)
    assert "\x00" not in out
    assert "\x01" not in out
    assert "hello" in out
    assert "world" in out


def test_empty_input_roundtrips():
    assert textutils.safe_err_body("") == ""
    assert textutils.safe_err_body(None) == ""  # type: ignore[arg-type]


def test_strip_json_fences_basic():
    assert textutils.strip_json_fences('```json\n{"a":1}\n```') == '{"a":1}'


def test_strip_json_fences_no_fence():
    assert textutils.strip_json_fences('{"a":1}') == '{"a":1}'
