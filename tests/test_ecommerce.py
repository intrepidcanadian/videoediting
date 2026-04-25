"""Tests for ecommerce.py — URL importer module + /api/ecommerce/extract endpoint.

Covers HTML cleanup, URL absolutization, image download validation, schema-shape
of the Claude response, and endpoint error paths. Claude calls are mocked so tests
don't hit the network or require an API key."""

import base64
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))


# ── HTML strip + URL absolutization ─────────────────────────────────────

class TestHtmlStrip:

    def test_strip_removes_scripts(self):
        import ecommerce
        html = '<html><script>alert(1)</script><p>hello</p></html>'
        out = ecommerce._strip_html(html)
        assert 'alert(1)' not in out
        assert 'hello' in out

    def test_strip_preserves_jsonld(self):
        import ecommerce
        html = '<html><script type="application/ld+json">{"name":"Gizmo"}</script><p>x</p></html>'
        out = ecommerce._strip_html(html)
        assert '"name":"Gizmo"' in out

    def test_strip_removes_styles(self):
        import ecommerce
        html = '<html><style>body{color:red}</style><p>visible</p></html>'
        out = ecommerce._strip_html(html)
        assert 'body{color:red}' not in out
        assert 'visible' in out

    def test_strip_removes_comments(self):
        import ecommerce
        html = '<html><!-- secret --><p>shown</p></html>'
        out = ecommerce._strip_html(html)
        assert 'secret' not in out


class TestAbsoluteUrl:

    def test_absolute_passthrough(self):
        import ecommerce
        assert ecommerce._absolute_url('https://x.com/p', 'https://cdn.com/a.jpg') == 'https://cdn.com/a.jpg'

    def test_protocol_relative(self):
        import ecommerce
        assert ecommerce._absolute_url('https://x.com/p', '//cdn.com/a.jpg') == 'https://cdn.com/a.jpg'

    def test_path_relative(self):
        import ecommerce
        assert ecommerce._absolute_url('https://x.com/products/widget', '/img/a.jpg') == 'https://x.com/img/a.jpg'

    def test_data_url_passthrough(self):
        import ecommerce
        d = 'data:image/png;base64,iVBORw0KGgo='
        assert ecommerce._absolute_url('https://x.com/p', d) == d

    def test_empty_returns_empty(self):
        import ecommerce
        assert ecommerce._absolute_url('https://x.com/p', '') == ''


# ── Image extension sniff ───────────────────────────────────────────────

class TestImageExt:

    def test_png_ext(self):
        import ecommerce
        assert ecommerce._ext_for_image(b'\x89PNG\r\n\x1a\n' + b'\x00' * 8) == '.png'

    def test_jpg_ext(self):
        import ecommerce
        assert ecommerce._ext_for_image(b'\xff\xd8\xff\xe0' + b'\x00' * 8) == '.jpg'

    def test_gif_ext(self):
        import ecommerce
        assert ecommerce._ext_for_image(b'GIF89a' + b'\x00' * 8) == '.gif'

    def test_webp_ext(self):
        import ecommerce
        assert ecommerce._ext_for_image(b'RIFF\x00\x00\x00\x00WEBP' + b'\x00' * 4) == '.webp'

    def test_unknown_falls_back_to_jpg(self):
        import ecommerce
        assert ecommerce._ext_for_image(b'\x00' * 16) == '.jpg'


# ── fetch_html validation ───────────────────────────────────────────────

class TestFetchHtml:

    def test_rejects_non_http_scheme(self):
        import ecommerce
        with pytest.raises(ValueError, match="scheme"):
            ecommerce.fetch_html("ftp://example.com/x")

    def test_rejects_file_scheme(self):
        import ecommerce
        with pytest.raises(ValueError, match="scheme"):
            ecommerce.fetch_html("file:///etc/passwd")

    def test_rejects_empty_host(self):
        import ecommerce
        with pytest.raises(ValueError, match="host"):
            ecommerce.fetch_html("http:///just-a-path")


# ── _download_image rejects non-images ──────────────────────────────────

class TestDownloadImage:

    def test_data_url_skipped(self):
        import ecommerce
        assert ecommerce._download_image('data:image/png;base64,xxx') is None

    def test_empty_url_skipped(self):
        import ecommerce
        assert ecommerce._download_image('') is None

    def test_non_image_response_skipped(self):
        """If the URL returns HTML/JS, we reject it — no magic-byte match."""
        import ecommerce
        with patch('httpx.Client') as mock_client:
            ctx = mock_client.return_value.__enter__.return_value
            mock_resp = MagicMock()
            mock_resp.content = b'<html>not an image</html>' + b'\x00' * 32
            mock_resp.raise_for_status = MagicMock()
            ctx.get.return_value = mock_resp
            result = ecommerce._download_image('https://cdn.com/fake.jpg')
            assert result is None

    def test_png_image_accepted(self):
        import ecommerce
        with patch('httpx.Client') as mock_client:
            ctx = mock_client.return_value.__enter__.return_value
            mock_resp = MagicMock()
            mock_resp.content = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
            mock_resp.raise_for_status = MagicMock()
            ctx.get.return_value = mock_resp
            result = ecommerce._download_image('https://cdn.com/hero.png')
            assert result is not None
            filename, data = result
            assert filename.endswith('.png')
            assert data.startswith(b'\x89PNG')

    def test_oversized_image_rejected(self):
        import ecommerce
        with patch('httpx.Client') as mock_client:
            ctx = mock_client.return_value.__enter__.return_value
            mock_resp = MagicMock()
            mock_resp.content = b'\x89PNG\r\n\x1a\n' + b'\x00' * (ecommerce._MAX_IMAGE_DOWNLOAD_BYTES + 1)
            mock_resp.raise_for_status = MagicMock()
            ctx.get.return_value = mock_resp
            assert ecommerce._download_image('https://cdn.com/huge.png') is None


# ── extract_product end-to-end with mocked Claude ───────────────────────

class TestExtractProduct:

    def test_no_api_key_raises(self, monkeypatch):
        import ecommerce
        monkeypatch.setattr(ecommerce, 'ANTHROPIC_API_KEY', '')
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            ecommerce.extract_product("https://example.com/p")

    def test_full_extract_with_mocked_claude(self, monkeypatch):
        import ecommerce
        monkeypatch.setattr(ecommerce, 'ANTHROPIC_API_KEY', 'sk-test')

        # Mock fetch_html to return canned HTML (no real network)
        monkeypatch.setattr(ecommerce, 'fetch_html', lambda url: '<html><body>fake</body></html>')

        # Mock Claude response
        claude_json = {
            "product_name": "Aurora Wireless Earbuds",
            "brand": "Aurora Audio",
            "price": "$129",
            "category": "wireless earbuds",
            "key_selling_points": ["40h battery", "ANC", "IPX5"],
            "image_urls": ["https://cdn.shop/img/hero.jpg"],
            "ad_concept": "A runner at dawn, the city waking up. Earbuds in. The world recedes — only breath, footfalls, and the music driving forward.",
            "style_intent": "Apple-style minimal, soft top light, anamorphic",
            "suggested_title": "Hear Forward",
            "suggested_shots": 6,
            "suggested_ratio": "9:16",
            "extraction_notes": "Clean Shopify product page with JSON-LD.",
        }
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text=json.dumps(claude_json), type='text')]
        mock_resp.usage = MagicMock(input_tokens=1500, output_tokens=400, cache_read_input_tokens=0)

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp

        # Mock image download to return a fake PNG
        def fake_dl(url):
            return ('hero.jpg', b'\x89PNG\r\n\x1a\n' + b'\x00' * 50)
        monkeypatch.setattr(ecommerce, '_download_image', fake_dl)

        with patch('anthropic.Anthropic', return_value=mock_client):
            result = ecommerce.extract_product("https://shop.example.com/p/aurora")

        assert result["product_name"] == "Aurora Wireless Earbuds"
        assert result["suggested_shots"] == 6
        assert result["suggested_ratio"] == "9:16"
        assert len(result["images"]) == 1
        assert result["images"][0]["filename"] == 'hero.jpg'
        # b64 should be valid base64
        decoded = base64.b64decode(result["images"][0]["b64"])
        assert decoded.startswith(b'\x89PNG')

    def test_non_product_page_returns_empty_images(self, monkeypatch):
        """If Claude says it's not a product, we don't try to download images."""
        import ecommerce
        monkeypatch.setattr(ecommerce, 'ANTHROPIC_API_KEY', 'sk-test')
        monkeypatch.setattr(ecommerce, 'fetch_html', lambda url: '<html><body>category</body></html>')

        claude_json = {
            "product_name": "",
            "brand": "",
            "price": "",
            "category": "",
            "key_selling_points": [],
            "image_urls": [],
            "ad_concept": "",
            "style_intent": "",
            "suggested_title": "",
            "suggested_shots": 6,
            "suggested_ratio": "16:9",
            "extraction_notes": "This is a category list, not a product detail page.",
        }
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text=json.dumps(claude_json), type='text')]
        mock_resp.usage = MagicMock(input_tokens=500, output_tokens=100, cache_read_input_tokens=0)
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp

        with patch('anthropic.Anthropic', return_value=mock_client):
            result = ecommerce.extract_product("https://shop.example.com/category/all")

        assert result["product_name"] == ""
        assert result["images"] == []
        assert "category list" in result["extraction_notes"]


# ── Endpoint /api/ecommerce/extract ─────────────────────────────────────

fastapi_testclient = pytest.importorskip("fastapi.testclient", reason="fastapi not installed")


@pytest.fixture
def client(tmp_output_root):
    from fastapi.testclient import TestClient
    import server
    with TestClient(server.app) as c:
        yield c


class TestEcommerceEndpoint:

    def test_missing_url_400(self, client):
        resp = client.post("/api/ecommerce/extract", json={})
        assert resp.status_code == 400
        assert "url" in resp.json()["detail"]

    def test_empty_url_400(self, client):
        resp = client.post("/api/ecommerce/extract", json={"url": "  "})
        assert resp.status_code == 400

    def test_oversized_url_400(self, client):
        resp = client.post("/api/ecommerce/extract", json={"url": "https://x.com/" + "a" * 3000})
        assert resp.status_code == 400
        assert "too long" in resp.json()["detail"]

    def test_bad_scheme_400(self, client):
        resp = client.post("/api/ecommerce/extract", json={"url": "ftp://example.com/x"})
        assert resp.status_code == 400

    def test_successful_extract(self, client, monkeypatch):
        import ecommerce
        fake_result = {
            "product_name": "Test Widget",
            "brand": "TestCo",
            "price": "$10",
            "category": "widget",
            "key_selling_points": ["a", "b"],
            "image_urls": [],
            "ad_concept": "test concept",
            "style_intent": "test style",
            "suggested_title": "Test",
            "suggested_shots": 6,
            "suggested_ratio": "16:9",
            "extraction_notes": "ok",
            "images": [],
        }
        monkeypatch.setattr(ecommerce, 'extract_product', lambda url, run_id="_ecommerce": fake_result)
        resp = client.post("/api/ecommerce/extract", json={"url": "https://shop.example.com/p"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["product_name"] == "Test Widget"
        assert body["suggested_shots"] == 6
