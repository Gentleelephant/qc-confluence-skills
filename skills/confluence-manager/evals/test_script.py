#!/usr/bin/env python3
"""Unit tests for confluence_api.py — no live Confluence API needed."""

import json
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

import confluence_api  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_env(directory, content):
    path = pathlib.Path(directory) / ".env"
    path.write_text(content, encoding="utf-8")
    return path


def parse(*argv):
    return confluence_api.normalize_args(confluence_api.build_parser().parse_args(list(argv)))


def ns(**kwargs):
    defaults = {
        "base_url": None,
        "pat": None,
        "password": None,
        "username": None,
        "auth_mode": None,
        "timeout": None,
        "retries": None,
        "env_file": None,
        "cwd": None,
        "user_path": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class FakeClient(confluence_api.ConfluenceClient):
    """Client whose transport is fully scripted by the test."""

    def __init__(self, responses=None, pages=None):
        super().__init__("https://wiki.example.com", pat="token")
        self.responses = responses or {}
        self.pages = pages or {}
        self.calls = []

    def request(self, path, query=None, retries=None):
        self.calls.append((path, dict(query or {})))
        response = self.responses.get(path)
        if callable(response):
            return response(query or {})
        if response is None:
            raise AssertionError(f"unexpected request: {path}")
        return response


# ---------------------------------------------------------------------------
# dotenv and configuration
# ---------------------------------------------------------------------------


class TestLoadDotenv(unittest.TestCase):
    def test_parses_simple_key_value(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as handle:
            handle.write("KEY=value\nNAME=test\n")
            handle.flush()
            result = confluence_api.load_dotenv(handle.name)
        os.unlink(handle.name)
        self.assertEqual(result["KEY"], "value")
        self.assertEqual(result["NAME"], "test")

    def test_skips_comments_and_blanks(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as handle:
            handle.write("# comment\n\nKEY=value\n")
            handle.flush()
            result = confluence_api.load_dotenv(handle.name)
        os.unlink(handle.name)
        self.assertEqual(result, {"KEY": "value"})

    def test_strips_matching_quotes(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as handle:
            handle.write('TOKEN="abcdef"\nURL=\'https://example.com\'\n')
            handle.flush()
            result = confluence_api.load_dotenv(handle.name)
        os.unlink(handle.name)
        self.assertEqual(result["TOKEN"], "abcdef")
        self.assertEqual(result["URL"], "https://example.com")

    def test_handles_export_prefix(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as handle:
            handle.write("export KEY=value\n")
            handle.flush()
            result = confluence_api.load_dotenv(handle.name)
        os.unlink(handle.name)
        self.assertEqual(result["KEY"], "value")

    def test_missing_file_returns_empty(self):
        self.assertEqual(confluence_api.load_dotenv("/nonexistent/path.env"), {})


class TestResolveSettings(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = pathlib.Path(self.temp.name) / "work"
        self.cwd.mkdir()
        self.user = pathlib.Path(self.temp.name) / "user" / ".env"
        self.user.parent.mkdir()

    def resolve(self, args, env=None):
        return confluence_api.resolve_settings(
            args, env=env or {}, cwd=self.cwd, user_path=self.user
        )

    def test_cli_beats_env_and_files(self):
        write_env(self.cwd, "CONFLUENCE_BASE_URL=https://file.example.com\nCONFLUENCE_PAT=file\n")
        settings = self.resolve(
            ns(base_url="https://cli.example.com", pat="cli"),
            env={"CONFLUENCE_BASE_URL": "https://env.example.com", "CONFLUENCE_PAT": "env"},
        )
        self.assertEqual(settings.base_url, "https://cli.example.com")
        self.assertEqual(settings.pat, "cli")
        self.assertEqual(settings.sources["base_url"], "cli")
        self.assertEqual(settings.sources["pat"], "cli")

    def test_env_beats_files(self):
        write_env(self.cwd, "CONFLUENCE_BASE_URL=https://file.example.com\n")
        settings = self.resolve(
            ns(), env={"CONFLUENCE_BASE_URL": "https://env.example.com", "CONFLUENCE_PAT": "env"}
        )
        self.assertEqual(settings.base_url, "https://env.example.com")
        self.assertEqual(settings.sources["base_url"], "env")

    def test_cwd_dotenv_is_loaded(self):
        write_env(self.cwd, "CONFLUENCE_BASE_URL=https://cwd.example.com\nCONFLUENCE_PAT=cwd\n")
        settings = self.resolve(ns())
        self.assertEqual(settings.base_url, "https://cwd.example.com")
        self.assertEqual(settings.pat, "cwd")
        self.assertEqual(settings.sources["pat"], str((self.cwd / ".env").resolve()))

    def test_user_config_is_fallback(self):
        self.user.write_text(
            "CONFLUENCE_BASE_URL=https://user.example.com\nCONFLUENCE_PAT=user\n",
            encoding="utf-8",
        )
        settings = self.resolve(ns())
        self.assertEqual(settings.base_url, "https://user.example.com")
        self.assertEqual(settings.pat, "user")

    def test_explicit_env_file_beats_cwd(self):
        explicit = pathlib.Path(self.temp.name) / "explicit.env"
        explicit.write_text(
            "CONFLUENCE_BASE_URL=https://explicit.example.com\nCONFLUENCE_PAT=explicit\n",
            encoding="utf-8",
        )
        write_env(self.cwd, "CONFLUENCE_BASE_URL=https://cwd.example.com\n")
        settings = self.resolve(ns(env_file=str(explicit)))
        self.assertEqual(settings.base_url, "https://explicit.example.com")
        self.assertEqual(settings.sources["base_url"], str(explicit.resolve()))

    def test_missing_explicit_env_file_is_config_error(self):
        with self.assertRaises(confluence_api.ConfigError):
            self.resolve(ns(env_file=str(pathlib.Path(self.temp.name) / "nope.env")))

    def test_missing_base_url_is_config_error(self):
        with self.assertRaises(confluence_api.ConfigError) as caught:
            self.resolve(ns())
        self.assertIn("base URL", caught.exception.message)

    def test_invalid_base_url_is_config_error(self):
        with self.assertRaises(confluence_api.ConfigError):
            self.resolve(ns(), env={"CONFLUENCE_BASE_URL": "wiki.example.com"})

    def test_rest_api_suffix_is_stripped(self):
        settings = self.resolve(
            ns(),
            env={
                "CONFLUENCE_BASE_URL": "https://wiki.example.com/rest/api/",
                "CONFLUENCE_PAT": "x",
            },
        )
        self.assertEqual(settings.base_url, "https://wiki.example.com")

    def test_bearer_requires_pat(self):
        with self.assertRaises(confluence_api.ConfigError):
            self.resolve(
                ns(),
                env={"CONFLUENCE_BASE_URL": "https://wiki.example.com", "CONFLUENCE_AUTH_MODE": "bearer"},
            )

    def test_basic_requires_username_and_password(self):
        with self.assertRaises(confluence_api.ConfigError):
            self.resolve(
                ns(),
                env={
                    "CONFLUENCE_BASE_URL": "https://wiki.example.com",
                    "CONFLUENCE_AUTH_MODE": "basic",
                    "CONFLUENCE_USERNAME": "alice",
                },
            )

    def test_invalid_timeout_and_retries(self):
        base = {"CONFLUENCE_BASE_URL": "https://wiki.example.com", "CONFLUENCE_PAT": "x"}
        with self.assertRaises(confluence_api.ConfigError):
            self.resolve(ns(), env={**base, "CONFLUENCE_TIMEOUT": "abc"})
        with self.assertRaises(confluence_api.ConfigError):
            self.resolve(ns(), env={**base, "CONFLUENCE_RETRIES": "-1"})

    def test_sanitized_never_leaks_secrets(self):
        settings = self.resolve(
            ns(),
            env={
                "CONFLUENCE_BASE_URL": "https://wiki.example.com",
                "CONFLUENCE_PAT": "super-secret-token",
                "CONFLUENCE_PASSWORD": "super-secret-password",
                "CONFLUENCE_USERNAME": "alice",
            },
        )
        rendered = json.dumps(settings.sanitized())
        self.assertNotIn("super-secret-token", rendered)
        self.assertNotIn("super-secret-password", rendered)
        self.assertTrue(settings.sanitized()["pat_set"])
        self.assertTrue(settings.sanitized()["password_set"])


# ---------------------------------------------------------------------------
# Parser and command aliases
# ---------------------------------------------------------------------------


class TestParser(unittest.TestCase):
    def test_requires_subcommand(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                confluence_api.build_parser().parse_args([])

    def test_aliases_are_normalized(self):
        self.assertEqual(parse("get-page", "--id", "1").command, "get")
        self.assertEqual(parse("search-pages", "--title", "x").command, "search")
        self.assertEqual(parse("list-attachments", "--id", "1").command, "attachments")

    def test_search_defaults(self):
        args = parse("search", "--title", "test")
        self.assertEqual(args.title, "test")
        self.assertIsNone(args.limit)
        self.assertEqual(args.start, 0)
        self.assertEqual(args.sort, "relevance")
        self.assertFalse(args.all_results)

    def test_search_limit_and_pagination(self):
        args = parse("search", "--title", "test", "--start", "20", "--limit", "5")
        self.assertEqual(args.start, 20)
        self.assertEqual(args.limit, 5)

    def test_get_defaults_and_aliases(self):
        args = parse("get-page", "--id", "123")
        self.assertEqual(args.id, "123")
        self.assertEqual(args.format, "md")

    def test_global_flags_accepted_before_and_after_subcommand(self):
        self.assertTrue(parse("--pretty", "search", "--title", "x").pretty)
        self.assertTrue(parse("search", "--title", "x", "--pretty").pretty)
        self.assertEqual(
            parse("--base-url", "https://a.example.com", "search", "--title", "x").base_url,
            "https://a.example.com",
        )

    def test_read_ids_option_uses_distinct_dest(self):
        args = parse("read", "--ids", "1,2")
        self.assertEqual(args.ids_option, "1,2")
        self.assertEqual(args.ids, [])

    def test_read_accepts_query_alias(self):
        self.assertEqual(parse("read", "-q", "deploy").query, "deploy")
        self.assertEqual(parse("read", "--query", "deploy").query, "deploy")

    def test_download_attachments_requires_out(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                confluence_api.build_parser().parse_args(["download-attachments", "--id", "1"])

    def test_export_requires_out(self):
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                confluence_api.build_parser().parse_args(["export", "--id", "1"])

    def test_export_target_options(self):
        args = parse("export", "--cql", "type=page", "--out", "/tmp/x", "--dry-run")
        self.assertEqual(args.cql, "type=page")
        self.assertTrue(args.dry_run)


# ---------------------------------------------------------------------------
# CQL construction and normalization
# ---------------------------------------------------------------------------


class TestBuildCql(unittest.TestCase):
    def test_query_defaults_to_relevance(self):
        cql = confluence_api.ConfluenceClient.build_cql(query="kubesphere 离线安装")
        self.assertNotIn("order by", cql)
        self.assertIn('type=page', cql)
        self.assertIn('text ~ "kubesphere 离线安装"', cql)
        self.assertIn('title ~ "kubesphere 离线安装"', cql)

    def test_recent_sort_adds_order(self):
        cql = confluence_api.ConfluenceClient.build_cql(query="x", sort="recent")
        self.assertTrue(cql.endswith("order by lastmodified desc"))

    def test_title_takes_precedence_over_query(self):
        cql = confluence_api.ConfluenceClient.build_cql(query="a", title="b")
        self.assertIn('title ~ "b"', cql)
        self.assertNotIn('text ~ "a"', cql)

    def test_space_and_label_filters(self):
        cql = confluence_api.ConfluenceClient.build_cql(
            query="x", space="SC", label="k8s"
        )
        self.assertIn("space = SC", cql)
        self.assertIn('label = "k8s"', cql)

    def test_escape_cql(self):
        self.assertEqual(confluence_api.escape_cql('a"b\\c'), 'a\\"b\\\\c')

    def test_explicit_cql_is_passed_through(self):
        cql = 'type=page AND space = "SC"'
        self.assertEqual(confluence_api.ConfluenceClient.build_cql(cql=cql), cql)


class TestNormalization(unittest.TestCase):
    def setUp(self):
        self.client = confluence_api.ConfluenceClient("https://wiki.example.com", pat="x")

    def test_legacy_result_is_flattened(self):
        item = {
            "content": {"id": "42", "type": "page", "status": "current", "title": "Deploy"},
            "url": "/display/SC/Deploy",
            "lastModified": "2026-01-01T00:00:00Z",
            "excerpt": "a @@@hl@@@match@@@endhl@@@ b",
            "resultGlobalContainer": {
                "title": "Space Name",
                "displayUrl": "/display/SC",
            },
        }
        item["excerpt"] = "a @@@hl@@@match@@@endhl@@@ b"
        result = self.client._normalize_legacy(item)
        self.assertEqual(result["id"], "42")
        self.assertEqual(result["space"], "SC")
        self.assertEqual(result["space_name"], "Space Name")
        self.assertEqual(result["url"], "https://wiki.example.com/display/SC/Deploy")
        self.assertNotIn("@@@hl@@@", result["excerpt"])
        self.assertIn("match", result["excerpt"])

    def test_space_key_falls_back_to_expandable(self):
        item = {"content": {"_expandable": {"space": "/rest/api/space/SC"}}}
        self.assertEqual(self.client._space_key(item), "SC")

    def test_content_result_includes_excerpt_only_when_requested(self):
        item = {
            "id": "1",
            "title": "T",
            "type": "page",
            "status": "current",
            "space": {"key": "SC", "name": "Space"},
            "_links": {"webui": "/pages/1"},
            "version": {"when": "2026-01-01"},
            "excerpt": {"value": "<b>hello</b>"},
        }
        without = self.client._normalize_content(item, excerpt=False)
        with_excerpt = self.client._normalize_content(item, excerpt=True)
        self.assertIsNone(without["excerpt"])
        self.assertEqual(with_excerpt["excerpt"], "<b>hello</b>")
        self.assertEqual(with_excerpt["url"], "https://wiki.example.com/pages/1")


# ---------------------------------------------------------------------------
# Search pagination
# ---------------------------------------------------------------------------


class PagedClient(FakeClient):
    def __init__(self, total, page_size):
        super().__init__()
        self.total = total
        self.page_size = page_size

    def _search_once(self, cql, limit, start, explain):
        remaining = max(0, self.total - start)
        size = min(limit, self.page_size, remaining)
        results = [
            {"id": str(start + index), "title": f"p{start + index}"}
            for index in range(size)
        ]
        return {
            "count": len(results),
            "total": self.total,
            "start": start,
            "limit": limit,
            "has_more": start + size < self.total,
            "next_start": start + size,
            "truncated": False,
            "results": results,
        }


class TestSearchPagination(unittest.TestCase):
    def test_single_page_reports_more(self):
        client = PagedClient(total=25, page_size=10)
        result = client.search(title="x", limit=10)
        self.assertEqual(result["count"], 10)
        self.assertEqual(result["total"], 25)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_start"], 10)

    def test_all_collects_everything(self):
        client = PagedClient(total=25, page_size=10)
        result = client.search(title="x", all_results=True, max_results=100)
        self.assertEqual(result["count"], 25)
        self.assertFalse(result["has_more"])
        self.assertEqual(len({item["id"] for item in result["results"]}), 25)

    def test_all_respects_max_results_and_flags_truncation(self):
        client = PagedClient(total=100, page_size=10)
        result = client.search(title="x", all_results=True, max_results=30)
        self.assertEqual(result["count"], 30)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["has_more"])

    def test_all_dedupes_repeated_ids(self):
        class DuplicateClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.rounds = 0

            def _search_once(self, cql, limit, start, explain):
                self.rounds += 1
                if self.rounds == 1:
                    return {
                        "count": 2,
                        "total": 3,
                        "start": 0,
                        "limit": limit,
                        "has_more": True,
                        "next_start": 2,
                        "results": [{"id": "1"}, {"id": "2"}],
                    }
                return {
                    "count": 1,
                    "total": 3,
                    "start": 2,
                    "limit": limit,
                    "has_more": False,
                    "next_start": 3,
                    "results": [{"id": "2"}],
                }

        result = DuplicateClient().search(title="x", all_results=True)
        self.assertEqual([item["id"] for item in result["results"]], ["1", "2"])

    def test_all_preserves_explain_payload(self):
        class Client(FakeClient):
            def _search_once(self, cql, limit, start, explain):
                return {
                    "count": 1,
                    "total": 1,
                    "start": 0,
                    "limit": limit,
                    "has_more": False,
                    "next_start": 1,
                    "truncated": False,
                    "results": [{"id": "1"}],
                    "explain": {"endpoint": "/rest/api/search"},
                }

        result = Client().search(title="x", all_results=True, explain=True)
        self.assertEqual(result["explain"], {"endpoint": "/rest/api/search"})


class TestSinglePageHasMore(unittest.TestCase):
    def test_server_size_below_request_means_done(self):
        data = {"size": 2, "totalSize": 2}
        self.assertFalse(confluence_api._single_page_has_more(data, [1, 2], 10))

    def test_total_size_reports_more(self):
        data = {"size": 10, "totalSize": 25}
        self.assertTrue(
            confluence_api._single_page_has_more(data, list(range(10)), 10)
        )

    def test_missing_total_falls_back_to_requested_limit(self):
        self.assertTrue(confluence_api._single_page_has_more({"size": 5}, [0] * 5, 5))
        self.assertFalse(confluence_api._single_page_has_more({"size": 3}, [0] * 3, 5))

    def test_reported_start_is_honoured(self):
        data = {"size": 10, "totalSize": 25, "start": 15}
        self.assertFalse(confluence_api._single_page_has_more(data, [0] * 10, 10))


# ---------------------------------------------------------------------------
# Page references, filenames, truncation
# ---------------------------------------------------------------------------


class TestParsePageRef(unittest.TestCase):
    def test_plain_id(self):
        self.assertEqual(confluence_api.ConfluenceClient.parse_page_ref("12345"), "12345")

    def test_viewpage_url(self):
        ref = "https://wiki.example.com/pages/viewpage.action?pageId=55351551"
        self.assertEqual(confluence_api.ConfluenceClient.parse_page_ref(ref), "55351551")

    def test_pretty_url(self):
        ref = "https://wiki.example.com/pages/123456/Some+Title"
        self.assertEqual(confluence_api.ConfluenceClient.parse_page_ref(ref), "123456")

    def test_invalid_reference(self):
        self.assertIsNone(
            confluence_api.ConfluenceClient.parse_page_ref("https://example.com/doc")
        )


class TestSafeFilename(unittest.TestCase):
    def test_blocks_path_traversal(self):
        self.assertEqual(confluence_api.safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(confluence_api.safe_filename(r"..\..\windows.ini"), "windows.ini")

    def test_removes_leading_dots_and_control_chars(self):
        self.assertEqual(confluence_api.safe_filename("..hidden"), "hidden")
        self.assertEqual(confluence_api.safe_filename("a\x00b\x1fc"), "a_b_c")

    def test_empty_uses_fallback(self):
        self.assertEqual(confluence_api.safe_filename("..."), "attachment")
        self.assertEqual(confluence_api.safe_filename("", fallback="x-1"), "x-1")

    def test_long_names_keep_suffix(self):
        name = "a" * 200 + ".png"
        safe = confluence_api.safe_filename(name)
        self.assertLessEqual(len(safe), 180)
        self.assertTrue(safe.endswith(".png"))

    def test_slugify_preserves_unicode_words(self):
        self.assertEqual(confluence_api.slugify("部署 Docker"), "部署-Docker")
        self.assertEqual(confluence_api.slugify(""), "page")


class TestTruncateAndSections(unittest.TestCase):
    def test_truncate_short_text(self):
        text, truncated = confluence_api.truncate_text("abc", 10)
        self.assertEqual(text, "abc")
        self.assertFalse(truncated)

    def test_truncate_long_text(self):
        text, truncated = confluence_api.truncate_text("abcdef", 3)
        self.assertTrue(truncated)
        self.assertTrue(text.startswith("abc"))
        self.assertIn("[truncated]", text)

    def test_extract_section_stops_at_same_level(self):
        markdown = "## A\na\n\n### A1\na1\n\n## B\nb\n"
        section, headings = confluence_api.extract_section(markdown, "a")
        self.assertEqual(headings, ["A", "A1", "B"])
        self.assertIn("A1", section)
        self.assertNotIn("## B", section)

    def test_extract_section_missing(self):
        section, headings = confluence_api.extract_section("## A\na\n", "zzz")
        self.assertIsNone(section)
        self.assertEqual(headings, ["A"])

    def test_extract_section_ignores_headings_inside_code_fences(self):
        markdown = (
            "## Install\n"
            "```bash\n"
            "## not a heading\n"
            "```\n"
            "body text\n"
        )
        section, headings = confluence_api.extract_section(markdown, "install")
        self.assertEqual(headings, ["Install"])
        self.assertIn("## not a heading", section)

    def test_extract_section_prefers_exact_heading(self):
        markdown = "## Install Guide\nguide\n\n## Install\nsteps\n\n## Other\nx\n"
        section, _ = confluence_api.extract_section(markdown, "install")
        self.assertTrue(section.startswith("## Install\n"))
        self.assertIn("steps", section)
        self.assertNotIn("guide", section)

    def test_markdown_to_text_strips_real_front_matter(self):
        text = confluence_api.markdown_to_text(
            '---\nid: "1"\ntitle: "Deploy"\n---\n\n# Body\n'
        )
        self.assertNotIn("title:", text)
        self.assertIn("Body", text)

    def test_markdown_to_text_keeps_content_between_rules(self):
        text = confluence_api.markdown_to_text("---\nIntro\n\nDetails\n---\nTail\n")
        self.assertIn("Intro", text)
        self.assertIn("Details", text)
        self.assertIn("Tail", text)


# ---------------------------------------------------------------------------
# Markdown conversion
# ---------------------------------------------------------------------------


class TestMarkdownConverter(unittest.TestCase):
    def convert(self, html, base="https://wiki.example.com"):
        return confluence_api.html_to_markdown(html, base)

    def test_headings_paragraphs_and_inline_markup(self):
        markdown, _ = self.convert(
            "<h1>Title</h1><p>Hello <strong>bold</strong> and <em>italic</em>.</p>"
        )
        self.assertIn("# Title", markdown)
        self.assertIn("**bold**", markdown)
        self.assertIn("*italic*", markdown)

    def test_code_panel_uses_brush_language(self):
        html = (
            '<div class="code panel pdl"><div class="codeContent panelContent pdl">'
            '<pre class="syntaxhighlighter-pre" '
            'data-syntaxhighlighter-params="brush: bash; gutter: false; theme: Confluence">'
            "kubectl get pods\n</pre></div></div>"
        )
        markdown, _ = self.convert(html)
        self.assertIn("```bash", markdown)
        self.assertIn("kubectl get pods", markdown)
        self.assertNotIn("theme: Confluence", markdown)

    def test_information_macro_becomes_blockquote(self):
        html = (
            '<div class="confluence-information-macro confluence-information-macro-warning">'
            '<span class="confluence-information-macro-icon"></span>'
            '<div class="confluence-information-macro-body"><p>warn text</p></div></div>'
        )
        markdown, _ = self.convert(html)
        self.assertIn("> **Warning**", markdown)
        self.assertIn("> warn text", markdown)

    def test_expand_macro_keeps_title_and_content(self):
        html = (
            '<div class="expand-container"><div class="expand-control">'
            '<span class="expand-control-text">Click me</span></div>'
            '<div class="expand-content"><p>hidden</p></div></div>'
        )
        markdown, _ = self.convert(html)
        self.assertIn("**Click me**", markdown)
        self.assertIn("hidden", markdown)

    def test_nested_lists_keep_indentation(self):
        html = "<ul><li>a<ul><li>a1</li><li>a2</li></ul></li><li>b</li></ul>"
        markdown, _ = self.convert(html)
        self.assertIn("- a\n  - a1\n  - a2\n- b", markdown)

    def test_tables_use_first_row_as_header(self):
        html = (
            '<table class="confluenceTable"><tbody>'
            "<tr><th>H1</th><th>H2</th></tr>"
            "<tr><td>v1</td><td>v2</td></tr>"
            "</tbody></table>"
        )
        markdown, _ = self.convert(html)
        self.assertIn("| H1 | H2 |", markdown)
        self.assertIn("| --- | --- |", markdown)
        self.assertIn("| v1 | v2 |", markdown)

    def test_images_are_absolute_and_reported(self):
        html = (
            '<p><img class="confluence-embedded-image" '
            'src="/download/attachments/1/a.png?api=v2" alt="pic"></p>'
        )
        markdown, images = self.convert(html)
        self.assertIn(
            "![pic](https://wiki.example.com/download/attachments/1/a.png?api=v2)",
            markdown,
        )
        self.assertEqual(images[0]["alt"], "pic")
        self.assertEqual(images[0]["url"], "https://wiki.example.com/download/attachments/1/a.png?api=v2")

    def test_relative_links_are_absolute(self):
        markdown, _ = self.convert('<p><a href="/display/SC/Page">doc</a></p>')
        self.assertIn("[doc](https://wiki.example.com/display/SC/Page)", markdown)

    def test_toc_macro_is_skipped(self):
        html = (
            "<p><style>.toc{}</style><div class='toc-macro rbtoc'>"
            "<ul class='toc-indentation'><li><a href='#h'>Contents entry</a></li></ul>"
            "</div></p><h2 id='h'>Body</h2>"
        )
        markdown, _ = self.convert(html)
        self.assertNotIn("Contents entry", markdown)
        self.assertIn("## Body", markdown)

    def test_inline_code_and_quotes(self):
        markdown, _ = self.convert("<p>Use <code>kubectl</code>.</p><blockquote><p>note</p></blockquote>")
        self.assertIn("`kubectl`", markdown)
        self.assertIn("> note", markdown)

    def test_markdown_to_text_strips_markup(self):
        text = confluence_api.markdown_to_text(
            "# Title\n\n**bold** `code` [link](https://x)\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
        )
        self.assertIn("Title", text)
        self.assertNotIn("**", text)
        self.assertNotIn("`", text)
        self.assertNotIn("](", text)

    def test_storage_fallback_renders_code_macro(self):
        storage = (
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">yaml</ac:parameter>'
            "<ac:plain-text-body><![CDATA[key: value]]></ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        markdown = confluence_api.storage_to_markdown(storage)
        self.assertIn("```yaml", markdown)
        self.assertIn("key: value", markdown)

    def test_storage_fallback_marks_unknown_macros(self):
        storage = '<ac:structured-macro ac:name="toc"></ac:structured-macro>'
        self.assertIn("[macro: toc]", confluence_api.storage_to_markdown(storage))

    def test_storage_fallback_keeps_body_macro_content(self):
        storage = (
            '<ac:structured-macro ac:name="info"><ac:rich-text-body>'
            "<p>kept text</p></ac:rich-text-body></ac:structured-macro>"
        )
        markdown = confluence_api.storage_to_markdown(storage)
        self.assertIn("kept text", markdown)
        self.assertNotIn("[macro: info]", markdown)

    def test_storage_fallback_handles_self_closing_image(self):
        storage = '<p><ac:image ac:alt="x"/>text</p>'
        markdown = confluence_api.storage_to_markdown(storage)
        self.assertIn("[image]", markdown)
        self.assertIn("text", markdown)

    def test_root_relative_urls_keep_context_path(self):
        markdown, _ = self.convert(
            '<p><a href="/display/SC/Page">doc</a>'
            ' <img src="/download/x.png" alt="i"></p>',
            base="https://wiki.example.com/confluence",
        )
        self.assertIn(
            "https://wiki.example.com/confluence/display/SC/Page", markdown
        )
        self.assertIn("https://wiki.example.com/confluence/download/x.png", markdown)

    def test_relative_urls_keep_context_path(self):
        markdown, _ = self.convert(
            '<p><a href="other/page">doc</a></p>',
            base="https://wiki.example.com/confluence",
        )
        self.assertIn(
            "https://wiki.example.com/confluence/other/page", markdown
        )

    def test_colspan_pads_the_row(self):
        html = (
            "<table><tr><th>H1</th><th>H2</th></tr>"
            '<tr><td colspan="2">wide</td></tr>'
            "<tr><td>a</td><td>b</td></tr></table>"
        )
        markdown, _ = self.convert(html)
        self.assertIn("| wide |  |", markdown)
        self.assertIn("| a | b |", markdown)

    def test_br_becomes_markdown_hard_break(self):
        markdown, _ = self.convert("<p>line1<br/>line2</p>")
        self.assertIn("line1  \nline2", markdown)

    def test_code_block_stays_inside_table_cell(self):
        html = (
            "<table><tr><th>cmd</th></tr><tr><td>"
            '<div class="code panel pdl"><div class="codeContent panelContent pdl">'
            '<pre class="syntaxhighlighter-pre">ls -l</pre></div></div>'
            "</td></tr></table>"
        )
        markdown, _ = self.convert(html)
        self.assertIn("| --- |", markdown)
        self.assertIn("ls -l", markdown.splitlines()[-1])

    def test_nested_macros_keep_both_bodies(self):
        html = (
            '<div class="panel"><div class="panelContent">'
            '<div class="confluence-information-macro '
            'confluence-information-macro-note">'
            '<div class="confluence-information-macro-body"><p>inner</p></div>'
            "</div><p>outer</p></div></div>"
        )
        markdown, _ = self.convert(html)
        self.assertIn("> **Note**", markdown)
        self.assertIn("> inner", markdown)
        self.assertIn("outer", markdown)


# ---------------------------------------------------------------------------
# Page fetching
# ---------------------------------------------------------------------------


class TestGetPage(unittest.TestCase):
    def make_client(self, page_data, attachments=None):
        responses = {"/rest/api/content/1": page_data}
        if attachments is not None:
            responses["/rest/api/content/1/child/attachment"] = {
                "results": attachments,
                "size": len(attachments),
                "totalSize": len(attachments),
            }
        return FakeClient(responses=responses)

    def page(self, export="", storage="", view=""):
        return {
            "id": "1",
            "title": "Deploy",
            "type": "page",
            "status": "current",
            "space": {"key": "SC", "name": "Space"},
            "version": {"number": 3, "when": "2026-01-01", "by": {"displayName": "Alice"}},
            "ancestors": [{"id": "0", "title": "Root"}],
            "metadata": {"labels": {"results": [{"name": "k8s"}]}},
            "children": {"attachment": {"size": 2}},
            "_links": {"webui": "/pages/1"},
            "body": {
                "export_view": {"value": export},
                "storage": {"value": storage},
                "view": {"value": view},
            },
        }

    def test_markdown_from_export_view(self):
        client = self.make_client(self.page(export="<h1>Hello</h1>"))
        result = client.get_page("1")
        self.assertEqual(result["source_format"], "export_view")
        self.assertIn("# Hello", result["body"])
        self.assertEqual(result["labels"], ["k8s"])
        self.assertEqual(result["attachment_count"], 2)
        self.assertEqual(result["url"], "https://wiki.example.com/pages/1")

    def test_falls_back_to_view_then_storage(self):
        client = self.make_client(self.page(export="", storage="<p>storage text</p>"))
        result = client.get_page("1")
        self.assertEqual(result["source_format"], "storage")
        self.assertIn("storage text", result["body"])

    def test_section_filter(self):
        client = self.make_client(self.page(export="<h2>A</h2><p>a</p><h2>B</h2><p>b</p>"))
        result = client.get_page("1", section="A")
        self.assertIn("## A", result["body"])
        self.assertNotIn("## B", result["body"])

    def test_missing_section_raises_not_found(self):
        client = self.make_client(self.page(export="<h2>A</h2>"))
        with self.assertRaises(confluence_api.NotFoundError):
            client.get_page("1", section="zzz")

    def test_max_chars_truncates(self):
        client = self.make_client(self.page(export="<p>" + "x" * 200 + "</p>"))
        result = client.get_page("1", max_chars=50)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["body"]), 100)

    def test_text_format_strips_markdown(self):
        client = self.make_client(self.page(export="<h1>Hello</h1><p><strong>bold</strong></p>"))
        result = client.get_page("1", body_format="text")
        self.assertNotIn("#", result["body"])
        self.assertNotIn("**", result["body"])

    def test_scalar_format_returns_raw_body(self):
        client = self.make_client(self.page(storage="<p>raw</p>"))
        result = client.get_page("1", body_format="storage")
        self.assertEqual(result["body"], "<p>raw</p>")

    def test_with_attachments_merges_listing(self):
        client = self.make_client(
            self.page(export="<p>x</p>"),
            attachments=[
                {
                    "id": "9",
                    "title": "a.png",
                    "metadata": {"mediaType": "image/png"},
                    "extensions": {"fileSize": 10},
                    "_links": {"download": "/download/a.png", "webui": "/pages/1"},
                    "version": {"number": 1, "when": "2026-01-01"},
                }
            ],
        )
        result = client.get_page("1", with_attachments=True)
        self.assertEqual(len(result["attachments"]), 1)
        self.assertEqual(
            result["attachments"][0]["download"],
            "https://wiki.example.com/download/a.png",
        )

    def test_attachment_error_is_reported_not_raised(self):
        class Broken(FakeClient):
            def list_attachments(self, *args, **kwargs):
                raise confluence_api.ApiError("boom")

        client = Broken(responses={"/rest/api/content/1": self.page(export="<p>x</p>")})
        result = client.get_page("1", with_attachments=True)
        self.assertEqual(result["attachments_error"], "boom")


# ---------------------------------------------------------------------------
# Tree, export, attachments
# ---------------------------------------------------------------------------


class TestTree(unittest.TestCase):
    def test_breadth_first_walk_and_depth_limit(self):
        def content(path, query):
            if path == "/rest/api/content/1":
                return {"id": "1", "title": "Root", "space": {"key": "SC"}, "version": {"number": 1}}
            if path == "/rest/api/content/1/child/page":
                return {"results": [{"id": "2", "title": "Child"}], "size": 1, "totalSize": 1}
            if path == "/rest/api/content/2/child/page":
                return {"results": [{"id": "3", "title": "Grandchild"}], "size": 1, "totalSize": 1}
            if path == "/rest/api/content/3/child/page":
                return {"results": [], "size": 0, "totalSize": 0}
            raise AssertionError(path)

        client = FakeClient(responses={})
        client.request = content
        result = client.tree(root_id="1")
        self.assertEqual([node["id"] for node in result["nodes"]], ["1", "2", "3"])
        self.assertEqual(result["nodes"][2]["depth"], 2)

        client = FakeClient(responses={})
        client.request = content
        limited = client.tree(root_id="1", max_depth=1)
        self.assertEqual([node["id"] for node in limited["nodes"]], ["1", "2"])
        self.assertTrue(limited["depth_limited"])

    def test_requires_root_or_space(self):
        client = FakeClient()
        with self.assertRaises(confluence_api.ConfigError):
            client.tree()


class TestExport(unittest.TestCase):
    def args(self, **kwargs):
        base = {
            "id": None,
            "ids": None,
            "space": None,
            "cql": None,
            "query": None,
            "label": None,
            "out": "/tmp/export",
            "format": "md",
            "dry_run": True,
            "overwrite": False,
            "download_attachments": False,
            "max_depth": None,
            "max_pages": 100,
            "concurrency": 2,
        }
        base.update(kwargs)
        return SimpleNamespace(**base)

    def test_cql_takes_precedence_over_ids(self):
        class Client(FakeClient):
            def search(self, **kwargs):
                return {"results": [{"id": "1", "title": "A", "space": "SC"}]}

            def page_meta(self, page_id):
                raise AssertionError("--ids should not be used")

        targets = confluence_api.collect_export_targets(
            Client(), self.args(cql="type=page", ids="2")
        )
        self.assertEqual([item["id"] for item in targets], ["1"])

    def test_ids_take_precedence_over_tree(self):
        class Client(FakeClient):
            def page_meta(self, page_id):
                return {"id": page_id, "title": f"T{page_id}", "space": "SC"}

            def tree(self, **kwargs):
                raise AssertionError("tree should not be used")

        targets = confluence_api.collect_export_targets(
            Client(), self.args(ids="2,3")
        )
        self.assertEqual([item["id"] for item in targets], ["2", "3"])

    def test_requires_a_target(self):
        with self.assertRaises(confluence_api.ConfigError):
            confluence_api.collect_export_targets(FakeClient(), self.args())

    def test_dry_run_plans_paths(self):
        class Client(FakeClient):
            def page_meta(self, page_id):
                return {"id": "42", "title": "部署 Guide", "space": "SC"}

        result = confluence_api.run_export(Client(), self.args(ids="42"))
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["total"], 1)
        self.assertTrue(result["planned"][0]["path"].endswith("/SC/42-部署-Guide.md"))

    def test_export_path_never_escapes_out_dir(self):
        path = confluence_api.export_path(
            "/tmp/out", {"id": "1", "title": "../../evil", "space": ".."}, ".md"
        )
        self.assertEqual(path.parent.name, "_")
        self.assertEqual(path.name, "1-evil.md")
        self.assertNotIn("..", str(path))

    def test_export_path_sanitizes_page_id(self):
        path = confluence_api.export_path(
            "/tmp/out", {"id": "../../etc/passwd", "title": "T", "space": "SC"}, ".md"
        )
        self.assertEqual(path.parent.name, "SC")
        self.assertNotIn("..", str(path))
        self.assertTrue(path.name.startswith("passwd-"))


class TestAttachments(unittest.TestCase):
    def listing(self):
        return {
            "results": [
                {
                    "id": "1",
                    "title": "../../evil.png",
                    "metadata": {"mediaType": "image/png"},
                    "_links": {"download": "/download/evil.png", "webui": "/pages/1"},
                    "version": {"number": 1},
                }
            ],
            "size": 1,
            "totalSize": 1,
        }

    def test_download_sanitizes_filename(self):
        class Client(FakeClient):
            def download(self, url, target, overwrite=False):
                return {"path": str(target), "bytes": 1, "status": "written"}

        client = Client(responses={"/rest/api/content/1/child/attachment": self.listing()})
        result = client.download_attachments("1", "/tmp/attachments")
        self.assertTrue(result["saved"][0]["path"].endswith("/evil.png"))
        self.assertNotIn("..", result["saved"][0]["path"])

    def test_missing_download_url_is_failed(self):
        class Client(FakeClient):
            def download(self, url, target, overwrite=False):
                raise AssertionError("should not download")

        client = Client(
            responses={
                "/rest/api/content/1/child/attachment": {
                    "results": [{"id": "1", "title": "x"}],
                    "size": 1,
                    "totalSize": 1,
                }
            }
        )
        result = client.download_attachments("1", "/tmp/attachments")
        self.assertEqual(result["failed"][0]["error"], "no download URL")


class TestDownloadSecurity(unittest.TestCase):
    def client(self):
        return confluence_api.ConfluenceClient(
            "https://wiki.example.com/confluence", pat="tok"
        )

    def test_rejects_non_http_schemes(self):
        client = self.client()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(confluence_api.ConfigError):
                client.download("file:///etc/passwd", pathlib.Path(tmp) / "x")

    def test_credentials_only_go_to_the_configured_origin(self):
        class Response:
            def __init__(self, payload=b"data"):
                self.payload = payload

            def read(self, size=-1):
                chunk, self.payload = self.payload, b""
                return chunk

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        calls = []

        class Opener:
            def __init__(self, handler):
                self.handler = handler

            def open(self, request, timeout=None):
                calls.append((dict(request.header_items()), self.handler))
                return Response()

        def fake_build_opener(*handlers):
            return Opener(handlers[0] if handlers else None)

        client = self.client()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            confluence_api.urllib.request, "build_opener", fake_build_opener
        ):
            client.download(
                "https://wiki.example.com/confluence/download/a.png",
                pathlib.Path(tmp) / "a.png",
            )
            client.download("https://cdn.example.net/a.png", pathlib.Path(tmp) / "b.png")

        same_headers, same_handler = calls[0]
        external_headers, external_handler = calls[1]
        self.assertEqual(same_headers.get("Authorization"), "Bearer tok")
        self.assertIsInstance(
            same_handler, confluence_api.SameOriginRedirectHandler
        )
        self.assertNotIn("Authorization", external_headers)
        self.assertIsNone(external_handler)

    def test_cross_origin_redirect_is_refused(self):
        handler = confluence_api.SameOriginRedirectHandler(
            ("https", "wiki.example.com")
        )
        request = confluence_api.urllib.request.Request(
            "https://wiki.example.com/a"
        )
        with self.assertRaises(confluence_api.urllib.error.HTTPError):
            handler.redirect_request(
                request, None, 302, "Found", {}, "https://evil.example.com/a"
            )


# ---------------------------------------------------------------------------
# Auth, transport error mapping, and output
# ---------------------------------------------------------------------------


class TestAuth(unittest.TestCase):
    def test_bearer_headers(self):
        client = confluence_api.ConfluenceClient(
            "https://wiki.example.com", pat="abc", auth_mode="bearer"
        )
        self.assertEqual(client._auth_headers("bearer")["Authorization"], "Bearer abc")

    def test_basic_headers(self):
        client = confluence_api.ConfluenceClient(
            "https://wiki.example.com",
            username="alice",
            password="secret",
            auth_mode="basic",
        )
        self.assertEqual(
            client._auth_headers("basic")["Authorization"],
            "Basic YWxpY2U6c2VjcmV0",
        )

    def test_auto_mode_prefers_bearer_then_basic(self):
        client = confluence_api.ConfluenceClient(
            "https://wiki.example.com", pat="x", username="u", password="p"
        )
        self.assertEqual(client._modes(), ["bearer", "basic"])

    def test_auto_without_credentials_is_config_error(self):
        client = confluence_api.ConfluenceClient("https://wiki.example.com")
        with self.assertRaises(confluence_api.ConfigError):
            client._modes()

    def test_explicit_modes_do_not_fall_back(self):
        client = confluence_api.ConfluenceClient(
            "https://wiki.example.com", pat="x", username="u", password="p", auth_mode="bearer"
        )
        self.assertEqual(client._modes(), ["bearer"])


class TestErrorMapping(unittest.TestCase):
    def test_exit_codes(self):
        self.assertEqual(confluence_api.ConfigError("x").exit_code, 2)
        self.assertEqual(confluence_api.AuthError("x").exit_code, 3)
        self.assertEqual(confluence_api.PermissionDeniedError("x").exit_code, 3)
        self.assertEqual(confluence_api.NotFoundError("x").exit_code, 4)
        self.assertEqual(confluence_api.NetworkError("x").exit_code, 5)
        self.assertEqual(confluence_api.ApiError("x").exit_code, 6)

    def test_error_payload_shape(self):
        payload = confluence_api.AuthError("bad", status=401, hint="check token").to_dict()
        self.assertEqual(payload["error"]["type"], "auth")
        self.assertEqual(payload["error"]["status"], 401)
        self.assertEqual(payload["error"]["hint"], "check token")

    def test_run_writes_json_error_to_stderr(self):
        stderr = StringIO()
        with patch.object(
            confluence_api,
            "resolve_settings",
            side_effect=confluence_api.AuthError("bad credentials"),
        ):
            with redirect_stderr(stderr):
                code = confluence_api.run(["check"])
        self.assertEqual(code, 3)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["error"]["type"], "auth")


class TestOutput(unittest.TestCase):
    def test_compact_is_default(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            confluence_api.write_json({"a": 1, "b": [1, 2]})
        self.assertEqual(stdout.getvalue(), '{"a":1,"b":[1,2]}\n')

    def test_pretty_output(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            confluence_api.write_json({"a": 1}, pretty=True)
        self.assertEqual(json.loads(stdout.getvalue()), {"a": 1})
        self.assertIn("\n", stdout.getvalue().strip())

    def test_version_matches_version_file(self):
        root = pathlib.Path(__file__).resolve().parents[3]
        expected = (root / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(confluence_api.__version__, expected)


if __name__ == "__main__":
    unittest.main()
