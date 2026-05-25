#!/usr/bin/env python3
"""Unit tests for confluence_api.py — no live API needed."""

import json
import os
import pathlib
import sys
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

# Import after setting up sys.path to find the module
import confluence_api  # noqa: E402


class TestLoadDotenv(unittest.TestCase):
    def test_parses_simple_key_value(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("KEY=value\nNAME=test\n")
            f.flush()
            result = confluence_api.load_dotenv(f.name)
        os.unlink(f.name)
        self.assertEqual(result["KEY"], "value")
        self.assertEqual(result["NAME"], "test")

    def test_skips_comments_and_blanks(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("# this is a comment\n\nKEY=value\n")
            f.flush()
            result = confluence_api.load_dotenv(f.name)
        os.unlink(f.name)
        self.assertEqual(result["KEY"], "value")
        self.assertNotIn("#", result)

    def test_strips_quotes(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write('TOKEN="abcdef"\nURL=\'https://example.com\'\n')
            f.flush()
            result = confluence_api.load_dotenv(f.name)
        os.unlink(f.name)
        self.assertEqual(result["TOKEN"], "abcdef")
        self.assertEqual(result["URL"], "https://example.com")

    def test_handles_export_prefix(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("export KEY=value\n")
            f.flush()
            result = confluence_api.load_dotenv(f.name)
        os.unlink(f.name)
        self.assertEqual(result["KEY"], "value")

    def test_missing_file_returns_empty(self):
        result = confluence_api.load_dotenv("/nonexistent/path.env")
        self.assertEqual(result, {})


class TestParser(unittest.TestCase):
    def test_get_page_requires_id(self):
        with self.assertRaises(SystemExit):
            confluence_api.build_parser().parse_args(["get-page"])

    def test_search_pages_defaults(self):
        args = confluence_api.build_parser().parse_args(["search-pages", "--title", "test"])
        self.assertEqual(args.title, "test")
        self.assertEqual(args.limit, 10)
        self.assertEqual(args.start, 0)

    def test_search_pages_with_pagination(self):
        args = confluence_api.build_parser().parse_args(
            ["search-pages", "--title", "test", "--start", "20", "--limit", "5"]
        )
        self.assertEqual(args.start, 20)
        self.assertEqual(args.limit, 5)

    def test_compact_flag(self):
        args = confluence_api.build_parser().parse_args(
            ["--compact", "search-pages", "--title", "test"]
        )
        self.assertTrue(args.compact)

    def test_list_attachments_requires_id(self):
        with self.assertRaises(SystemExit):
            confluence_api.build_parser().parse_args(["list-attachments"])

    def test_download_attachments_requires_id_and_out(self):
        with self.assertRaises(SystemExit):
            confluence_api.build_parser().parse_args(["download-attachments", "--id", "1"])


class TestConfluenceClientInit(unittest.TestCase):
    def test_trailing_slash_stripped(self):
        client = confluence_api.ConfluenceClient("https://example.com/")
        self.assertEqual(client.base_url, "https://example.com")

    def test_no_trailing_slash_preserved(self):
        client = confluence_api.ConfluenceClient("https://example.com")
        self.assertEqual(client.base_url, "https://example.com")


class TestOutputFormat(unittest.TestCase):
    """Verify main() outputs valid JSON to stdout and errors to stderr."""

    def test_compact_output(self):
        with patch.dict(os.environ, {"CONFLUENCE_BASE_URL": "https://example.com"}):
            with patch.object(confluence_api.ConfluenceClient, "get_page") as mock_get:
                mock_get.return_value = {"id": "123", "title": "Test"}
                with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
                    with patch("sys.argv", ["confluence_api.py", "--compact", "get-page", "--id", "123"]):
                        confluence_api.main()
                output = mock_stdout.getvalue().strip()
                self.assertEqual(output, '{"id": "123", "title": "Test"}')

    def test_pretty_output(self):
        with patch.dict(os.environ, {"CONFLUENCE_BASE_URL": "https://example.com"}):
            with patch.object(confluence_api.ConfluenceClient, "get_page") as mock_get:
                mock_get.return_value = {"id": "123", "title": "Test"}
                with patch("sys.stdout", new_callable=StringIO) as mock_stdout:
                    with patch("sys.argv", ["confluence_api.py", "get-page", "--id", "123"]):
                        confluence_api.main()
                output = mock_stdout.getvalue().strip()
                data = json.loads(output)
                self.assertEqual(data["id"], "123")
                self.assertIn("\n", output)

    def test_error_goes_to_stderr(self):
        with patch.dict(os.environ, {"CONFLUENCE_BASE_URL": "https://example.com"}):
            with patch.object(confluence_api.ConfluenceClient, "_request") as mock_req:
                mock_req.side_effect = RuntimeError("connection refused")
                with patch("sys.argv", ["confluence_api.py", "get-page", "--id", "1"]):
                    with self.assertRaises(RuntimeError):
                        confluence_api.main()


class TestResolveBodyImages(unittest.TestCase):
    def test_replaces_attachment_image(self):
        body = '<ac:image ac:alt="Diagram"><ri:attachment ri:filename="flow.png"/></ac:image>'
        attachments = [{"title": "flow.png", "download": "https://cwiki/flow.png"}]
        resolved, images = confluence_api.ConfluenceClient._resolve_body_images(body, attachments)
        self.assertIn("![Diagram](https://cwiki/flow.png)", resolved)
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["filename"], "flow.png")

    def test_replaces_url_image(self):
        body = '<ac:image ac:alt="External"><ri:url ri:value="https://e.com/img.jpg"/></ac:image>'
        resolved, images = confluence_api.ConfluenceClient._resolve_body_images(body, [])
        self.assertIn("![External](https://e.com/img.jpg)", resolved)

    def test_marks_missing_attachment(self):
        body = '<ac:image><ri:attachment ri:filename="missing.png"/></ac:image>'
        resolved, images = confluence_api.ConfluenceClient._resolve_body_images(body, [])
        self.assertIn("file not found: missing.png", resolved)
        self.assertTrue(images[0]["missing"])

    def test_no_images_returns_unchanged(self):
        body = "<p>Just text</p>"
        resolved, images = confluence_api.ConfluenceClient._resolve_body_images(body, [])
        self.assertEqual(resolved, body)
        self.assertEqual(images, [])

    def test_multiline_image_tag(self):
        body = '<ac:image ac:alt="multi">\n  <ri:attachment ri:filename="a.jpg" />\n</ac:image>'
        attachments = [{"title": "a.jpg", "download": "https://cwiki/a.jpg"}]
        resolved, images = confluence_api.ConfluenceClient._resolve_body_images(body, attachments)
        self.assertIn("![multi](https://cwiki/a.jpg)", resolved)
        self.assertEqual(len(images), 1)


if __name__ == "__main__":
    unittest.main()
