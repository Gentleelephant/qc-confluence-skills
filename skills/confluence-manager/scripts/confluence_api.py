#!/usr/bin/env python3
"""Confluence REST API client for the confluence-manager skill.

Contract: a single JSON document is written to stdout on success. Failures are
written to stderr as JSON and mapped to stable exit codes:

    0  success
    2  configuration or usage problem
    3  authentication / permission problem
    4  resource not found
    5  network / timeout problem
    6  server or API problem
    1  unexpected error

Only the Python standard library is used so the script stays portable.
"""

import argparse
import base64
import concurrent.futures
import html as html_lib
import json
import os
import pathlib
import re
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from html.parser import HTMLParser

__version__ = "1.0.1"

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_NOT_FOUND = 4
EXIT_NETWORK = 5
EXIT_API = 6

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
HIGHLIGHT_RE = re.compile(r"@@@(?:end)?hl@@@")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConfluenceError(Exception):
    """Base class for errors that map to a stable exit code."""

    exit_code = EXIT_UNEXPECTED
    error_type = "error"

    def __init__(self, message, *, status=None, hint=None, url=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.hint = hint
        self.url = url

    def to_dict(self):
        payload = {"type": self.error_type, "message": self.message}
        if self.status is not None:
            payload["status"] = self.status
        if self.hint:
            payload["hint"] = self.hint
        if self.url:
            payload["url"] = self.url
        return {"error": payload}


class ConfigError(ConfluenceError):
    exit_code = EXIT_CONFIG
    error_type = "config"


class AuthError(ConfluenceError):
    exit_code = EXIT_AUTH
    error_type = "auth"


class PermissionDeniedError(ConfluenceError):
    exit_code = EXIT_AUTH
    error_type = "permission"


class NotFoundError(ConfluenceError):
    exit_code = EXIT_NOT_FOUND
    error_type = "not_found"


class NetworkError(ConfluenceError):
    exit_code = EXIT_NETWORK
    error_type = "network"


class ApiError(ConfluenceError):
    exit_code = EXIT_API
    error_type = "api"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENV_KEYS = {
    "base_url": "CONFLUENCE_BASE_URL",
    "pat": "CONFLUENCE_PAT",
    "password": "CONFLUENCE_PASSWORD",
    "username": "CONFLUENCE_USERNAME",
    "auth_mode": "CONFLUENCE_AUTH_MODE",
    "timeout": "CONFLUENCE_TIMEOUT",
    "retries": "CONFLUENCE_RETRIES",
}

CONFIG_KEYS = set(ENV_KEYS.values())


def load_dotenv(path):
    """Read a dotenv file. Missing files yield an empty mapping."""
    values = {}
    dotenv_path = pathlib.Path(path).expanduser()
    if not dotenv_path.exists():
        return values

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def user_config_path():
    """Return the per-user configuration path, independent of the cwd."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = pathlib.Path(xdg).expanduser() if xdg else pathlib.Path.home() / ".config"
    return root / "confluence-manager" / ".env"


def config_dir():
    return user_config_path().parent


class Settings:
    def __init__(self, base_url, pat, password, username, auth_mode, timeout, retries, sources, files):
        self.base_url = base_url
        self.pat = pat
        self.password = password
        self.username = username
        self.auth_mode = auth_mode
        self.timeout = timeout
        self.retries = retries
        self.sources = sources
        self.files = files

    def sanitized(self):
        """Return a mapping safe to print; secret values are never included."""
        return {
            "base_url": self.base_url,
            "auth_mode": self.auth_mode,
            "username": self.username,
            "pat_set": bool(self.pat),
            "pat_source": self.sources.get("pat"),
            "password_set": bool(self.password),
            "password_source": self.sources.get("password"),
            "timeout": self.timeout,
            "retries": self.retries,
            "sources": {k: v for k, v in self.sources.items() if k in ("base_url", "username", "auth_mode")},
            "config_files": [
                {"path": str(path), "exists": path.exists()} for path in self.files
            ],
        }


def resolve_settings(args, env=None, cwd=None, user_path=None):
    """Resolve configuration with CLI > env > explicit file > cwd .env > user config."""
    env = os.environ if env is None else env
    cwd = pathlib.Path.cwd() if cwd is None else pathlib.Path(cwd)
    user_path = user_config_path() if user_path is None else pathlib.Path(user_path)

    layers = []
    cli_values = {
        "base_url": getattr(args, "base_url", None),
        "pat": getattr(args, "pat", None),
        "password": getattr(args, "password", None),
        "username": getattr(args, "username", None),
        "auth_mode": getattr(args, "auth_mode", None),
        "timeout": getattr(args, "timeout", None),
        "retries": getattr(args, "retries", None),
    }
    layers.append(("cli", cli_values))
    layers.append(("env", {key: env.get(env_key) for key, env_key in ENV_KEYS.items()}))

    searched = []
    seen_paths = set()

    def add_file(path):
        """Append a .env layer unless it was already added (first wins)."""
        resolved = path.expanduser().resolve()
        if resolved in seen_paths:
            return
        seen_paths.add(resolved)
        searched.append(resolved)
        if resolved.exists():
            raw = load_dotenv(resolved)
            layers.append(
                (
                    str(resolved),
                    {key: raw.get(env_key) for key, env_key in ENV_KEYS.items()},
                )
            )

    explicit = getattr(args, "env_file", None)
    if explicit:
        explicit_path = pathlib.Path(explicit).expanduser()
        if not explicit_path.exists():
            raise ConfigError(
                f"env file not found: {explicit_path}",
                hint="Pass --env-file only when the file exists, or use `config init`.",
            )
        add_file(explicit_path)
    add_file(cwd / ".env")
    add_file(user_path)

    values = {}
    sources = {}
    for key in ENV_KEYS:
        for name, layer in layers:
            value = layer.get(key)
            if value is not None and str(value).strip() != "":
                values[key] = str(value).strip()
                sources[key] = name
                break
        else:
            values[key] = None
            sources[key] = None

    base_url = values["base_url"]
    if not base_url:
        raise ConfigError(
            "missing Confluence base URL",
            hint=(
                "Set CONFLUENCE_BASE_URL or run "
                "`confluence_api.py config init` to create ~/.config/confluence-manager/.env."
            ),
        )
    if not re.match(r"^https?://", base_url):
        raise ConfigError(
            f"invalid base URL: {base_url}",
            hint="CONFLUENCE_BASE_URL must start with http:// or https://.",
        )
    base_url = base_url.rstrip("/")
    if base_url.endswith("/rest/api"):
        base_url = base_url[: -len("/rest/api")]

    auth_mode = (values["auth_mode"] or "auto").lower()
    if auth_mode not in ("auto", "bearer", "basic"):
        raise ConfigError(
            f"unsupported auth mode: {auth_mode}",
            hint="Use one of: auto, bearer, basic.",
        )

    try:
        timeout = int(values["timeout"]) if values["timeout"] else 30
    except ValueError:
        raise ConfigError(f"invalid timeout: {values['timeout']}", hint="Use an integer number of seconds.")
    try:
        retries = int(values["retries"]) if values["retries"] else 2
    except ValueError:
        raise ConfigError(f"invalid retries: {values['retries']}", hint="Use a non-negative integer.")
    if timeout <= 0:
        raise ConfigError("timeout must be positive")
    if retries < 0:
        raise ConfigError("retries must be non-negative")

    pat = values["pat"]
    password = values["password"]
    username = values["username"]

    if auth_mode == "bearer" and not pat:
        raise ConfigError(
            "bearer auth requires CONFLUENCE_PAT",
            hint="Set CONFLUENCE_PAT, or use CONFLUENCE_AUTH_MODE=basic with username and password.",
        )
    if auth_mode == "basic":
        if not username or not password:
            raise ConfigError(
                "basic auth requires CONFLUENCE_USERNAME and CONFLUENCE_PASSWORD",
                hint=(
                    "A personal access token is only valid for bearer auth. "
                    "Basic auth needs your login password."
                ),
            )
    if auth_mode == "auto" and not pat and not (username and password):
        raise ConfigError(
            "no usable credentials",
            hint=(
                "Set CONFLUENCE_PAT (preferred), or set both CONFLUENCE_USERNAME and "
                "CONFLUENCE_PASSWORD for basic auth."
            ),
        )

    return Settings(
        base_url=base_url,
        pat=pat,
        password=password,
        username=username,
        auth_mode=auth_mode,
        timeout=timeout,
        retries=retries,
        sources=sources,
        files=searched,
    )


# ---------------------------------------------------------------------------
# Markdown conversion
# ---------------------------------------------------------------------------


def _abs_url(base_url, value):
    if not value:
        return value
    if value.startswith(("#", "mailto:", "tel:", "data:")):
        return value
    base = (base_url or "").rstrip("/")
    # Confluence is commonly served under a context path such as
    # https://host/confluence. ``urljoin`` would discard that path for
    # root-relative values, so concatenate them instead.
    if value.startswith("//"):
        return urllib.parse.urljoin(base + "/", value)
    if value.startswith("/"):
        return base + value
    return urllib.parse.urljoin(base + "/", value)


def _origin(url):
    """Return ``(scheme, netloc)`` for an absolute URL, lowercased."""
    parts = urllib.parse.urlsplit(url or "")
    return parts.scheme.lower(), parts.netloc.lower()


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects away from the configured Confluence origin.

    ``urllib`` replays the original request headers — including the
    ``Authorization`` header — on every redirect, so an authenticated
    request that is redirected to another host would leak the PAT.
    """

    def __init__(self, origin):
        super().__init__()
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != self.origin:
            raise urllib.error.HTTPError(
                newurl,
                code,
                f"refusing cross-origin redirect to {newurl}",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


HARD_BREAK = "\u0000"


def _normalize_inline(text):
    text = text.replace("\xa0", " ")
    # Preserve Markdown hard breaks (two trailing spaces) across whitespace
    # normalisation.
    text = text.replace("  \n", HARD_BREAK)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = text.replace(HARD_BREAK, "  \n")
    return text.strip()


class MarkdownConverter(HTMLParser):
    """Small, dependency-free HTML to Markdown converter.

    It targets Confluence ``body.export_view`` markup: rendered macros,
    syntax-highlighted code panels, tables, and absolute media URLs.
    """

    SKIP_TAGS = {"style", "script", "noscript", "head", "meta", "link"}
    VOID_TAGS = {"meta", "link", "br", "hr", "img", "input", "col", "area", "base", "embed", "source", "track", "wbr"}

    def __init__(self, base_url=""):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url.rstrip("/")
        self.blocks = []
        self.inline = []
        self.buffers = [self.inline]
        self.images = []
        self.list_stack = []
        self.li_stack = []
        self.quote_depth = 0
        self.pre_depth = 0
        self.pre_buf = []
        self.pre_lang = None
        self.table = None
        self.row = None
        self.cell = None
        self.skip_depth = 0
        self.toc_depth = 0
        self.macro_stack = []
        self.heading_level = 0
        self.capture = None

    # -- buffer helpers ----------------------------------------------------

    def _append(self, text):
        if not text or self.skip_depth or self.toc_depth:
            return
        if self.capture is not None:
            self.capture["buf"].append(text)
            return
        self.buffers[-1].append(text)

    def _push(self, buf):
        self.buffers.append(buf)

    def _pop(self):
        if len(self.buffers) > 1:
            self.buffers.pop()

    def _flush(self):
        if self.pre_depth or self.table is not None:
            return
        text = _normalize_inline("".join(self.inline))
        self.inline = []
        self.buffers[0] = self.inline
        if not text:
            return
        self._emit(text)

    def _emit(self, text):
        text = text.strip("\n")
        if not text.strip():
            return
        if self.quote_depth:
            lines = text.splitlines()
            text = "\n".join(("> " + line) if line else ">" for line in lines)
        is_item = bool(re.match(r"^\s*(?:[-*+]|\d+\.)\s", text))
        if (
            self.quote_depth == 0
            and is_item
            and self.blocks
            and re.match(r"^\s*(?:[-*+]|\d+\.)\s", self.blocks[-1])
        ):
            # Keep consecutive list items in one block so Markdown renders
            # nested lists without a paragraph break.
            self.blocks[-1] += "\n" + text
            return
        self.blocks.append(text)

    def _take_blocks(self, start):
        chunk = [b for b in self.blocks[start:] if b.strip()]
        del self.blocks[start:]
        return chunk

    # -- inline markers ----------------------------------------------------

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if self.pre_depth:
            if tag == "pre":
                self.pre_depth += 1
            return
        if tag in self.SKIP_TAGS:
            if tag not in self.VOID_TAGS:
                self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if self.toc_depth:
            if tag == "div":
                self.toc_depth += 1
            return

        # A title/control capture only keeps text; nested markup is ignored.
        if self.capture is not None:
            if tag == self.capture["tag"]:
                self.capture["depth"] += 1
            if tag == "div" and self.macro_stack:
                self.macro_stack[-1]["div_depth"] += 1
            return

        if "toc-macro" in classes:
            self._flush()
            self.toc_depth = 1
            return

        if tag == "pre":
            if self._in_list_item():
                self._flush_current_li()
            else:
                self._flush()
            self.pre_depth = 1
            self.pre_buf = []
            self.pre_lang = self._code_language(attrs)
            self._push(self.pre_buf)
            return

        if self.table is not None:
            self._table_start(tag, attrs)
            return

        if tag == "table":
            self._flush()
            self.table = {"rows": []}
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush()
            self.heading_level = int(tag[1])
            return

        if tag == "br":
            self._append("  \n")
            return

        if tag == "hr":
            self._flush()
            self._emit("---")
            return

        if tag == "blockquote":
            self._flush()
            self.quote_depth += 1
            return

        if tag in ("ul", "ol"):
            if self._in_list_item():
                self._flush_current_li()
            else:
                self._flush()
            if self.li_stack:
                parent = self.li_stack[-1]
                indent = parent["indent"] + " " * len(parent["marker"])
            else:
                indent = ""
            self.list_stack.append({"type": tag, "index": 0, "indent": indent})
            return

        if tag == "li":
            if not self.list_stack:
                return
            if not self._in_list_item():
                self._flush()
            ctx = self.list_stack[-1]
            ctx["index"] += 1
            marker = "- " if ctx["type"] == "ul" else f"{ctx['index']}. "
            self.li_stack.append({"marker": marker, "indent": ctx["indent"]})
            return

        if tag in ("strong", "b"):
            self._append("**")
            return
        if tag in ("em", "i"):
            self._append("*")
            return
        if tag in ("del", "s", "strike"):
            self._append("~~")
            return
        if tag == "code":
            self._append("`")
            return
        if tag == "a":
            self._append("[")
            self._push_link(attrs.get("href") or "")
            return
        if tag == "img":
            src = _abs_url(
                self.base_url, attrs.get("src") or attrs.get("data-image-src") or ""
            )
            alt = attrs.get("alt") or attrs.get("title") or ""
            if src:
                self._append(f"![{alt}]({src})")
                self.images.append({"alt": alt, "url": src})
            return
        if tag == "input" and (attrs.get("type") or "").lower() == "checkbox":
            checked = "checked" in attrs or attrs.get("checked") is not None
            self._append("[x] " if checked else "[ ] ")
            return

        # Macro containers -------------------------------------------------
        # A nested macro's own closing </div> is consumed by its own
        # finalize step, so it must not be counted against the parent.
        if "confluence-information-macro" in classes:
            self._flush()
            self.macro_stack.append(
                {
                    "kind": self._macro_kind(classes),
                    "start": len(self.blocks),
                    "title": None,
                    "div_depth": 1,
                }
            )
            return
        if tag == "div" and "panel" in classes and "code" not in classes:
            self._flush()
            self.macro_stack.append(
                {"kind": "panel", "start": len(self.blocks), "title": None, "div_depth": 1}
            )
            return
        if "expand-control" in classes and "expand-control-text" not in classes:
            if self.macro_stack:
                self.macro_stack[-1]["div_depth"] += 1
            self._start_capture("expand", "div")
            return
        if tag == "div" and "panelHeader" in classes and self.macro_stack:
            self.macro_stack[-1]["div_depth"] += 1
            self._start_capture("macro-title", "div")
            return

        if tag in ("p", "div"):
            if tag == "div" and self.macro_stack:
                self.macro_stack[-1]["div_depth"] += 1
            if tag == "p" and "title" in classes and self.macro_stack:
                self._start_capture("macro-title", "p")
                return
            if self._in_list_item():
                return
            self._flush()
            return

    def handle_endtag(self, tag):
        if self.pre_depth:
            if tag == "pre":
                self.pre_depth -= 1
                if self.pre_depth == 0:
                    self._pop()
                    code = "".join(self.pre_buf).strip("\n")
                    self.pre_buf = []
                    if code.strip():
                        fence = "```"
                        while fence in code:
                            fence += "`"
                        lang = self.pre_lang or ""
                        code_block = f"{fence}{lang}\n{code}\n{fence}"
                        if self.table is not None or self._in_list_item():
                            # Keep the code inside its cell or list item
                            # instead of escaping to the top level.
                            self._append("\n" + code_block + "\n")
                        else:
                            self.blocks.append(code_block)
            return
        if tag in self.SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if self.toc_depth:
            if tag == "div":
                self.toc_depth -= 1
            return

        if self.capture is not None:
            if tag == self.capture["tag"]:
                self.capture["depth"] -= 1
                if self.capture["depth"] <= 0:
                    self._finish_capture()
            if tag == "div" and self.macro_stack:
                self.macro_stack[-1]["div_depth"] -= 1
                if self.macro_stack[-1]["div_depth"] <= 0 and self.capture is None:
                    self._flush()
                    self._finalize_macro()
            return

        if self.table is not None:
            self._table_end(tag)
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = _normalize_inline("".join(self.inline))
            self.inline = []
            self.buffers[0] = self.inline
            if text:
                self._emit("#" * self.heading_level + " " + text)
            self.heading_level = 0
            return

        if tag == "div":
            if self.macro_stack:
                self.macro_stack[-1]["div_depth"] -= 1
                if self.macro_stack[-1]["div_depth"] <= 0:
                    self._flush()
                    self._finalize_macro()
                    return
            if self._in_list_item():
                return
            self._flush()
            return

        if tag == "p":
            if self._in_list_item():
                return
            self._flush()
            return

        if tag == "blockquote":
            self._flush()
            self.quote_depth = max(0, self.quote_depth - 1)
            return

        if tag in ("ul", "ol"):
            self._flush()
            if self.list_stack:
                self.list_stack.pop()
            return

        if tag == "li":
            if self.li_stack:
                item = self.li_stack.pop()
                text = _normalize_inline("".join(self.inline))
                self.inline = []
                self.buffers[0] = self.inline
                if text:
                    self._emit(self._li_text(item, text))
            return

        if tag in ("strong", "b"):
            self._append("**")
            return
        if tag in ("em", "i"):
            self._append("*")
            return
        if tag in ("del", "s", "strike"):
            self._append("~~")
            return
        if tag == "code":
            self._append("`")
            return
        if tag == "a":
            href = self._pop_link()
            self._append(f"]({_abs_url(self.base_url, href)})")
            return

    # -- capture / list helpers -------------------------------------------

    def _start_capture(self, kind, tag):
        self.capture = {"buf": [], "kind": kind, "tag": tag, "depth": 1}

    def _finish_capture(self):
        capture = self.capture
        self.capture = None
        if not capture:
            return
        text = _normalize_inline("".join(capture["buf"]))
        if capture["kind"] == "expand":
            if text:
                self._emit(f"**{text}**")
        elif capture["kind"] == "macro-title" and self.macro_stack:
            self.macro_stack[-1]["title"] = text or None

    def _flush_current_li(self):
        if not self.li_stack:
            self._flush()
            return
        text = _normalize_inline("".join(self.inline))
        self.inline = []
        self.buffers[0] = self.inline
        if text:
            self._emit(self._li_text(self.li_stack[-1], text))

    @staticmethod
    def _li_text(item, text):
        lines = text.splitlines()
        first = item["indent"] + item["marker"] + lines[0]
        continuation = item["indent"] + " " * len(item["marker"])
        rest = [continuation + line for line in lines[1:]]
        return "\n".join([first] + rest)

    def handle_data(self, data):
        if self.skip_depth or self.toc_depth:
            return
        self._append(data)

    # -- links -------------------------------------------------------------

    def _push_link(self, href):
        self._link_stack = getattr(self, "_link_stack", [])
        self._link_stack.append(href)

    def _pop_link(self):
        stack = getattr(self, "_link_stack", [])
        return stack.pop() if stack else ""

    # -- helpers -----------------------------------------------------------

    def _in_list_item(self):
        return bool(self.li_stack)

    @staticmethod
    def _macro_kind(classes):
        for kind in ("warning", "note", "tip", "info"):
            if f"confluence-information-macro-{kind}" in classes:
                return kind
        return "info"

    @staticmethod
    def _code_language(attrs):
        params = attrs.get("data-syntaxhighlighter-params") or ""
        match = re.search(r"brush:\s*([A-Za-z0-9_+#.-]+)", params)
        if match:
            return match.group(1).lower()
        classes = (attrs.get("class") or "").split()
        for name in classes:
            if name.startswith("language-"):
                return name[len("language-") :].lower()
        return None

    # -- tables ------------------------------------------------------------

    def _table_start(self, tag, attrs):
        if tag in ("thead", "tbody", "tfoot", "colgroup", "col"):
            return
        if tag == "tr":
            self.row = []
            return
        if tag in ("th", "td"):
            self.cell = []
            self.cell_span = 1
            try:
                self.cell_span = max(1, int(attrs.get("colspan") or 1))
            except (TypeError, ValueError):
                self.cell_span = 1
            self._push(self.cell)
            return
        if tag == "br":
            self._append("\n")
            return
        if tag in ("strong", "b"):
            self._append("**")
            return
        if tag in ("em", "i"):
            self._append("*")
            return
        if tag == "code":
            self._append("`")
            return
        if tag == "a":
            self._append("[")
            self._push_link(attrs.get("href") or "")
            return
        if tag == "img":
            src = _abs_url(self.base_url, attrs.get("src") or attrs.get("data-image-src") or "")
            alt = attrs.get("alt") or ""
            if src:
                self._append(f"![{alt}]({src})")
                self.images.append({"alt": alt, "url": src})
            return

    def _table_end(self, tag):
        if tag in ("thead", "tbody", "tfoot", "colgroup", "col"):
            return
        if tag in ("strong", "b"):
            self._append("**")
            return
        if tag in ("em", "i"):
            self._append("*")
            return
        if tag == "code":
            self._append("`")
            return
        if tag == "a":
            href = self._pop_link()
            self._append(f"]({_abs_url(self.base_url, href)})")
            return
        if tag in ("th", "td") and self.cell is not None:
            text = _normalize_inline("".join(self.cell))
            self._pop()
            self.cell = None
            if self.row is not None:
                self.row.append(text)
                # A colspan cell occupies several columns; pad the row so
                # later cells keep their intended alignment.
                self.row.extend([""] * (self.cell_span - 1))
            self.cell_span = 1
            return
        if tag == "tr" and self.row is not None:
            if self.row:
                self.table["rows"].append(self.row)
            self.row = None
            return
        if tag == "table" and self.table is not None:
            rows = self.table["rows"]
            self.table = None
            self._render_table(rows)

    def _render_table(self, rows):
        if not rows:
            return
        width = max(len(row) for row in rows)
        matrix = [row + [""] * (width - len(row)) for row in rows]

        def cell(value):
            value = value.replace("|", "\\|").replace("\n", "<br>")
            return value.strip()

        lines = []
        header = matrix[0]
        lines.append("| " + " | ".join(cell(v) for v in header) + " |")
        lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
        for row in matrix[1:]:
            lines.append("| " + " | ".join(cell(v) for v in row) + " |")
        self.blocks.append("\n".join(lines))

    # -- macro finalization ------------------------------------------------

    def close(self):
        super().close()
        # Unclosed inline content still belongs in the output.
        self._flush()

        # Wrap info/panel macros that were opened but not closed by malformed HTML.
        while self.macro_stack:
            self._finalize_macro()

    def _finalize_macro(self):
        macro = self.macro_stack.pop()
        chunk = self._take_blocks(macro["start"])
        if not chunk:
            return
        label = macro["title"]
        if not label:
            kind = macro["kind"]
            label = "Panel" if kind == "panel" else kind.capitalize()
        lines = [f"**{label}**", ""]
        for block in chunk:
            lines.extend(block.splitlines())
        quoted = "\n".join(("> " + line) if line else ">" for line in lines)
        self.blocks.append(quoted)

    def markdown(self):
        self.close()
        parts = [block.strip("\n") for block in self.blocks if block.strip()]
        return "\n\n".join(parts).strip("\n") + "\n"


def html_to_markdown(html, base_url=""):
    """Convert Confluence export_view HTML into Markdown."""
    converter = MarkdownConverter(base_url=base_url)
    converter.feed(html or "")
    markdown = converter.markdown()
    images = converter.images
    # Deduplicate while preserving order.
    seen = set()
    unique = []
    for image in images:
        key = (image.get("alt"), image.get("url"))
        if key not in seen:
            seen.add(key)
            unique.append(image)
    return markdown, unique


def _unwrap_paragraph_macros(html):
    return re.sub(r"(?is)<p[^>]*>\s*(<style\b.*?</style>)?\s*(<div class=['\"]toc-macro)", r"\1\2", html)


def storage_to_markdown(html):
    """Best-effort fallback for body.storage, used only when rendering fails."""
    html = re.sub(r"(?is)<(style|script)\b.*?</\1>", "", html or "")

    # Macros whose body is real content should keep it instead of collapsing
    # to a placeholder.
    body_macros = {
        "info",
        "note",
        "tip",
        "warning",
        "panel",
        "expand",
        "section",
        "column",
        "div",
        "details",
        "html",
    }

    def code_macro(match):
        block = match.group(0)
        lang_match = re.search(r'ac:name="code".*?<ac:parameter ac:name="language">(.*?)</ac:parameter>', block, re.S)
        lang = lang_match.group(1).strip() if lang_match else ""
        body_match = re.search(r"<ac:plain-text-body>\s*<!\[CDATA\[(.*?)\]\]>\s*</ac:plain-text-body>", block, re.S)
        if not body_match:
            return "\n[macro: code]\n"
        code = body_match.group(1)
        fence = "```"
        while fence in code:
            fence += "`"
        return f"\n{fence}{lang}\n{code}\n{fence}\n"

    def structured_macro(match):
        name = match.group(1)
        inner = match.group(2)
        if name in body_macros:
            body = re.search(r"(?is)<ac:rich-text-body>(.*)</ac:rich-text-body>", inner)
            if body:
                return "\n" + body.group(1) + "\n"
        return f"\n[macro: {name}]\n"

    html = re.sub(
        r"(?is)<ac:structured-macro[^>]*ac:name=\"code\".*?</ac:structured-macro>",
        code_macro,
        html,
    )
    html = re.sub(
        r"(?is)<ac:structured-macro[^>]*ac:name=\"([^\"]+)\"[^>]*>(.*?)</ac:structured-macro>",
        structured_macro,
        html,
    )
    html = re.sub(r"(?is)<ac:image\b[^>]*/\s*>", "\n[image]\n", html)
    html = re.sub(r"(?is)<ac:image[^>]*>.*?</ac:image>", "\n[image]\n", html)
    html = re.sub(r"(?is)</?ac:[^>]+>", "", html)
    html = re.sub(r"(?is)<br\s*/?>", "\n", html)
    html = re.sub(r"(?is)</t[dh]>", " | ", html)
    html = re.sub(r"(?is)</(p|div|li|tr|h[1-6])>", "\n", html)
    html = re.sub(r"(?is)<[^>]+>", " ", html)
    html = html_lib.unescape(html).replace("\xa0", " ")
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n+", "\n\n", html)
    return html.strip()


def markdown_to_text(markdown):
    """Flatten Markdown for answer-oriented output."""
    text = markdown or ""
    # Only strip a YAML front matter block (``key: value`` lines between
    # ``---`` fences); a leading horizontal rule must survive.
    text = re.sub(
        r"\A---\n(?:[A-Za-z_][\w-]*:[^\n]*\n)+---\n\n?",
        "",
        text,
    )
    text = re.sub(r"```[^\n]*\n(.*?)```", lambda m: m.group(1), text, flags=re.S)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(.*?)__", r"\1", text, flags=re.S)
    text = re.sub(r"\*(.*?)\*", r"\1", text, flags=re.S)
    text = re.sub(r"~~(.*?)~~", r"\1", text, flags=re.S)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^>\s?", "", text, flags=re.M)
    text = re.sub(r"^\s*\|?\s*---[-\s|]*$", "", text, flags=re.M)
    text = text.replace("|", " ")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + ("\n" if text.strip() else "")


def extract_section(markdown, name):
    """Return the heading section matching ``name`` (case-insensitive)."""
    lines = (markdown or "").splitlines()
    heading_re = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
    fence_open_re = re.compile(r"^(`{3,}|~{3,})")
    fence_close_re = re.compile(r"^(`{3,}|~{3,})\s*$")
    target = name.strip().casefold()
    headings = []
    matches = []
    protected = [False] * len(lines)
    fence = None
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if fence is not None:
            protected[index] = True
            close = fence_close_re.match(stripped)
            if close and close.group(1)[0] == fence[0] and len(close.group(1)) >= len(fence):
                fence = None
            continue
        opening = fence_open_re.match(stripped)
        if opening:
            protected[index] = True
            fence = opening.group(1)
            continue
        match = heading_re.match(line)
        if not match:
            continue
        title = re.sub(r"[*_`]", "", match.group(2)).strip()
        headings.append(title)
        matches.append((index, len(match.group(1)), title))

    # An exact heading beats a heading that merely contains the name.
    start = None
    level = 0
    for exact_only in (True, False):
        for index, depth, title in matches:
            folded = title.casefold()
            hit = folded == target if exact_only else target in folded
            if hit:
                start, level = index, depth
                break
        if start is not None:
            break
    if start is None:
        return None, headings
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if protected[index]:
            # A ``#`` line inside a code fence is content, not a heading.
            continue
        match = heading_re.match(lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end]).strip() + "\n", headings


def truncate_text(text, max_chars):
    if not max_chars or len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip() + "\n\n… [truncated]\n", True


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def clean_excerpt(text, limit=280):
    text = HIGHLIGHT_RE.sub("", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text or None


def _single_page_has_more(data, items, want):
    """Whether a single (non-paginated) API response has further results.

    Prefer the server-reported ``size`` over the requested limit: Confluence
    returns fewer rows than requested when the result set is exhausted, and
    ``totalSize`` is missing on some endpoints.
    """
    size = data.get("size")
    if not isinstance(size, int) or size < 0:
        size = len(items)
    total = data.get("totalSize")
    if isinstance(total, int):
        start = data.get("start")
        start = start if isinstance(start, int) and start >= 0 else 0
        return start + size < total
    return size >= want


def slugify(title, limit=80):
    slug = re.sub(r"[^\w.-]+", "-", title or "", flags=re.UNICODE)
    slug = re.sub(r"-{2,}", "-", slug).strip("-._")
    return slug[:limit].rstrip("-._") or "page"


def safe_filename(name, fallback="attachment"):
    """Return a single path component that cannot escape its directory."""
    name = CONTROL_RE.sub("_", str(name or ""))
    name = name.replace("\\", "/").split("/")[-1]
    name = name.replace("\x00", "").strip()
    name = name.lstrip(".")
    if not name:
        return fallback
    if len(name) > 180:
        stem, dot, suffix = name.rpartition(".")
        if dot and len(suffix) <= 16:
            name = stem[: 180 - len(suffix) - 1] + "." + suffix
        else:
            name = name[:180]
    return name or fallback


def escape_cql(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


class ConfluenceClient:
    def __init__(
        self,
        base_url,
        pat=None,
        password=None,
        auth_mode="auto",
        username=None,
        timeout=30,
        retries=2,
    ):
        self.base_url = base_url.rstrip("/")
        self.pat = pat
        self.password = password
        self.auth_mode = auth_mode
        self.username = username
        self.timeout = timeout
        self.retries = retries
        self.last_auth_mode = None

    # -- transport ---------------------------------------------------------

    def _modes(self):
        if self.auth_mode == "bearer":
            return ["bearer"]
        if self.auth_mode == "basic":
            return ["basic"]
        modes = []
        if self.pat:
            modes.append("bearer")
        if self.username and self.password:
            modes.append("basic")
        if not modes:
            raise ConfigError(
                "no usable credentials",
                hint="Set CONFLUENCE_PAT or username/password.",
            )
        return modes

    def _auth_headers(self, mode):
        headers = {"Accept": "application/json"}
        if mode == "bearer":
            headers["Authorization"] = f"Bearer {self.pat}"
        elif mode == "basic":
            token = base64.b64encode(
                f"{self.username}:{self.password}".encode("utf-8")
            ).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        else:
            raise ConfigError(f"unsupported auth mode: {mode}")
        return headers

    def _absolute(self, value):
        return _abs_url(self.base_url, value)

    def request(self, path, query=None, retries=None):
        """Perform a GET request and return decoded JSON."""
        url = self._absolute(path)
        if query:
            clean = {key: value for key, value in query.items() if value is not None}
            url = f"{url}?{urllib.parse.urlencode(clean, doseq=True)}"
        retries = self.retries if retries is None else retries
        modes = self._modes()
        last_error = None

        for mode_index, mode in enumerate(modes):
            attempt = 0
            while True:
                request = urllib.request.Request(url, headers=self._auth_headers(mode))
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        data = response.read()
                        content_type = response.headers.get("Content-Type", "")
                        self.last_auth_mode = mode
                        if "json" in content_type or data[:1] in (b"{", b"["):
                            try:
                                return json.loads(data.decode("utf-8"))
                            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                                raise ApiError(
                                    f"invalid JSON response from {url}: {exc}",
                                    url=url,
                                )
                        return {
                            "status": response.status,
                            "content_type": content_type,
                            "body": data.decode("utf-8", errors="replace"),
                        }
                except urllib.error.HTTPError as exc:
                    if exc.code == 401 and mode_index + 1 < len(modes):
                        last_error = self._map_http_error(exc, url, mode)
                        break
                    if exc.code in RETRY_STATUSES and attempt < retries:
                        delay = self._retry_delay(exc, attempt)
                        exc.close()
                        time.sleep(delay)
                        attempt += 1
                        continue
                    raise self._map_http_error(exc, url, mode)
                except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                    if attempt < retries:
                        time.sleep(min(2**attempt, 8))
                        attempt += 1
                        continue
                    raise NetworkError(
                        f"cannot reach {self.base_url}: {exc}",
                        hint="Check VPN, network connectivity, and CONFLUENCE_BASE_URL.",
                    )
        if last_error:
            raise last_error
        raise NetworkError(f"cannot reach {self.base_url}")

    @staticmethod
    def _retry_delay(exc, attempt):
        header = exc.headers.get("Retry-After") if exc.headers else None
        if header:
            try:
                return max(1, int(header))
            except ValueError:
                pass
        return min(2**attempt, 8)

    def _map_http_error(self, exc, url, mode):
        body = exc.read().decode("utf-8", errors="replace")
        message = self._server_message(body) or str(exc.reason)
        if exc.code == 401:
            return AuthError(
                f"authentication failed ({mode}): {message}",
                status=401,
                url=url,
                hint=(
                    "Check CONFLUENCE_PAT. If using basic auth, set CONFLUENCE_USERNAME "
                    "and CONFLUENCE_PASSWORD; a PAT is not a password."
                ),
            )
        if exc.code == 403:
            return PermissionDeniedError(
                f"permission denied: {message}",
                status=403,
                url=url,
                hint="The credentials are valid but this account cannot access the resource.",
            )
        if exc.code == 404:
            return NotFoundError(
                f"not found: {message}",
                status=404,
                url=url,
                hint="Verify the page id, space key, or CQL query.",
            )
        if exc.code == 400:
            return ApiError(
                f"bad request: {message}",
                status=400,
                url=url,
                hint="Check the CQL syntax and query parameters.",
            )
        return ApiError(
            f"HTTP {exc.code}: {message}",
            status=exc.code,
            url=url,
        )

    @staticmethod
    def _server_message(body):
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            text = re.sub(r"(?is)<[^>]+>", " ", body or "")
            return re.sub(r"\s+", " ", text).strip()[:300]
        if isinstance(data, dict):
            for key in ("message", "errorMessage"):
                if data.get(key):
                    return str(data[key])[:300]
            errors = data.get("errorMessages")
            if isinstance(errors, list) and errors:
                return str(errors[0])[:300]
        return None

    # -- user / diagnostics ------------------------------------------------

    def whoami(self):
        return self.request("/rest/api/user/current")

    def check(self, settings):
        user = self.whoami()
        user_type = user.get("type")
        authenticated = user_type == "known"
        search_ok = True
        search_error = None
        try:
            self.request(
                "/rest/api/search",
                {"cql": "type=page", "limit": 1},
                retries=0,
            )
        except PermissionDeniedError as exc:
            search_ok = False
            search_error = exc.message
        except ConfluenceError as exc:
            search_ok = False
            search_error = exc.message

        if not authenticated:
            raise AuthError(
                "credentials were not accepted; Confluence treated the request as anonymous",
                hint=(
                    "Verify CONFLUENCE_PAT and its source. In auto mode, basic auth is "
                    "tried only when CONFLUENCE_USERNAME and CONFLUENCE_PASSWORD are set."
                ),
            )
        warnings = []
        if self.base_url.lower().startswith("http://"):
            warnings.append(
                "base URL uses plain HTTP; credentials and page content travel unencrypted"
            )
        return {
            "ok": authenticated and search_ok,
            "authenticated": authenticated,
            "search_ok": search_ok,
            "search_error": search_error,
            "base_url": self.base_url,
            "auth_mode": self.last_auth_mode or settings.auth_mode,
            "user": {
                "username": user.get("username"),
                "display_name": user.get("displayName"),
                "type": user_type,
                "account_id": user.get("accountId"),
            },
            "warnings": warnings,
            "config": settings.sanitized(),
        }

    # -- search ------------------------------------------------------------

    @staticmethod
    def build_cql(query=None, cql=None, title=None, space=None, label=None, sort="relevance"):
        if cql:
            return cql
        clauses = ["type=page"]
        if title:
            clauses.append(f'title ~ "{escape_cql(title)}"')
        elif query:
            value = escape_cql(query)
            clauses.append(f'(text ~ "{value}" OR title ~ "{value}")')
        if space:
            clauses.append(f"space = {space}")
        if label:
            clauses.append(f'label = "{escape_cql(label)}"')
        statement = " AND ".join(clauses)
        if sort == "recent":
            statement += " order by lastmodified desc"
        return statement

    def search(
        self,
        query=None,
        cql=None,
        title=None,
        space=None,
        label=None,
        limit=10,
        start=0,
        explain=False,
        all_results=False,
        max_results=500,
        sort="relevance",
    ):
        statement = self.build_cql(
            query=query, cql=cql, title=title, space=space, label=label, sort=sort
        )
        if all_results:
            collected = []
            seen = set()
            cursor = 0
            total = None
            has_more = False
            explain_payload = None
            page_size = max(1, min(limit or 100, 100))
            while len(collected) < max_results:
                batch = self._search_once(
                    statement,
                    min(page_size, max_results - len(collected)),
                    cursor,
                    explain,
                )
                total = batch["total"]
                has_more = bool(batch["has_more"])
                if batch.get("explain"):
                    explain_payload = batch["explain"]
                for item in batch["results"]:
                    key = item.get("id")
                    if key and key in seen:
                        continue
                    seen.add(key)
                    collected.append(item)
                if not has_more or not batch["results"]:
                    break
                cursor = batch["next_start"]
            else:
                has_more = True
            truncated = has_more and (total is None or len(collected) < total)
            payload = {
                "count": len(collected),
                "total": total,
                "start": 0,
                "limit": len(collected),
                "has_more": truncated,
                "next_start": len(collected),
                "truncated": truncated,
                "cql": statement,
                "results": collected,
            }
            if explain_payload is not None:
                payload["explain"] = explain_payload
            return payload

        batch = self._search_once(statement, limit, start, explain)
        batch["cql"] = statement
        return batch

    def _search_once(self, cql, limit, start, explain):
        limit = max(1, min(int(limit or 10), 500))
        params = {"cql": cql, "limit": limit, "start": start}
        started = time.time()
        try:
            data = self.request("/rest/api/search", params)
            endpoint = "legacy"
            results = [self._normalize_legacy(item) for item in data.get("results") or []]
            total = data.get("totalSize")
            size = data.get("size", len(results))
        except (ApiError, NotFoundError) as exc:
            if exc.status not in (400, 404, 405):
                raise
            # /rest/api/content/search needs explicit expands; without them the
            # space and version fields the normaliser reads are absent.
            fallback_params = dict(params, expand="space,version")
            data = self.request("/rest/api/content/search", fallback_params)
            endpoint = "content"
            results = [
                self._normalize_content(item, excerpt=True)
                for item in data.get("results") or []
            ]
            total = data.get("totalSize")
            size = data.get("size", len(results))
        count = len(results)
        has_more = bool(total is not None and start + size < total)
        payload = {
            "count": count,
            "total": total,
            "start": start,
            "limit": limit,
            "has_more": has_more,
            "next_start": start + size,
            "truncated": False,
            "results": results,
        }
        if explain:
            payload["explain"] = {
                "cql": cql,
                "endpoint": f"/rest/api/{ 'search' if endpoint == 'legacy' else 'content/search'}",
                "requested_limit": limit,
                "returned": count,
                "server_size": size,
                "total": total,
                "elapsed_ms": int((time.time() - started) * 1000),
                "server_capped": count < limit and has_more,
            }
        return payload

    def _normalize_legacy(self, item):
        content = item.get("content") or {}
        url = item.get("url") or (content.get("_links") or {}).get("webui")
        return {
            "id": content.get("id"),
            "title": content.get("title") or HIGHLIGHT_RE.sub("", item.get("title") or ""),
            "type": content.get("type"),
            "status": content.get("status"),
            "space": self._space_key(item),
            "space_name": (item.get("resultGlobalContainer") or {}).get("title"),
            "url": self._absolute(url) if url else None,
            "last_modified": item.get("lastModified"),
            "excerpt": clean_excerpt(item.get("excerpt")),
        }

    def _normalize_content(self, item, excerpt=False):
        space = item.get("space") or {}
        body_excerpt = None
        if excerpt:
            raw = item.get("excerpt")
            if isinstance(raw, dict):
                body_excerpt = clean_excerpt(" ".join(str(v) for v in raw.values()))
            elif isinstance(raw, str):
                body_excerpt = clean_excerpt(raw)
        return {
            "id": item.get("id"),
            "title": item.get("title"),
            "type": item.get("type"),
            "status": item.get("status"),
            "space": space.get("key"),
            "space_name": space.get("name"),
            "url": self._absolute((item.get("_links") or {}).get("webui") or ""),
            "last_modified": (item.get("version") or {}).get("when"),
            "excerpt": body_excerpt,
        }

    @staticmethod
    def _space_key(item):
        container = item.get("resultGlobalContainer") or {}
        display = container.get("displayUrl") or ""
        match = re.match(r"/(?:display|spaces)/([^/?#]+)", display)
        if match:
            return urllib.parse.unquote(match.group(1))
        content = item.get("content") or {}
        expandable = (content.get("_expandable") or {}).get("space")
        if expandable:
            return expandable.rstrip("/").rsplit("/", 1)[-1]
        return None

    # -- page references ---------------------------------------------------

    @staticmethod
    def parse_page_ref(ref):
        ref = str(ref or "").strip()
        if ref.isdigit():
            return ref
        parsed = urllib.parse.urlparse(ref)
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("pageId") and query["pageId"][0].isdigit():
            return query["pageId"][0]
        match = re.search(r"/pages/(\d+)", parsed.path)
        if match:
            return match.group(1)
        match = re.search(r"[?&]pageId=(\d+)", ref)
        if match:
            return match.group(1)
        return None

    def resolve_page_id(self, title, space=None):
        """Resolve a title to a page id. Returns (id, match, candidates)."""
        params = {"type": "page", "title": title, "expand": "space,version"}
        if space:
            params["spaceKey"] = space
        try:
            data = self.request("/rest/api/content", params)
        except ConfluenceError:
            data = {}
        results = data.get("results") or []
        exact = [
            item
            for item in results
            if (item.get("title") or "").casefold() == title.casefold()
            and (not space or (item.get("space") or {}).get("key") == space)
        ]
        if exact:
            return self._pick_page(exact, "exact")

        statement = self.build_cql(title=title, space=space)
        found = self.search(cql=statement, limit=10)
        candidates = [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "space": item.get("space"),
                "url": item.get("url"),
            }
            for item in found.get("results") or []
        ]
        exact_fuzzy = [
            item
            for item in candidates
            if (item.get("title") or "").casefold() == title.casefold()
        ]
        if exact_fuzzy:
            return exact_fuzzy[0]["id"], "exact", candidates
        if candidates:
            return candidates[0]["id"], "fuzzy", candidates
        raise NotFoundError(
            f"no page titled {title!r}" + (f" in space {space}" if space else ""),
            hint="Use `search -q ...` to inspect candidates.",
        )

    @staticmethod
    def _pick_page(items, match):
        candidates = [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "space": (item.get("space") or {}).get("key"),
                "url": None,
            }
            for item in items[:5]
        ]
        return items[0].get("id"), match, candidates

    def page_meta(self, page_id):
        data = self.request(
            f"/rest/api/content/{page_id}",
            {"expand": "space,version,ancestors"},
        )
        return {
            "id": data.get("id"),
            "title": data.get("title"),
            "space": (data.get("space") or {}).get("key"),
            "version": (data.get("version") or {}).get("number"),
            "url": self._absolute((data.get("_links") or {}).get("webui") or ""),
            "parent_id": ((data.get("ancestors") or [{}])[-1]).get("id"),
        }

    # -- pages -------------------------------------------------------------

    def get_page(
        self,
        page_id,
        body_format="md",
        section=None,
        max_chars=None,
        with_attachments=False,
    ):
        source_format = "export_view" if body_format in ("md", "text") else body_format
        expand = (
            f"body.{source_format},version,space,ancestors,"
            "metadata.labels,children.attachment"
        )
        data = self.request(f"/rest/api/content/{page_id}", {"expand": expand})
        body = ((data.get("body") or {}).get(source_format) or {}).get("value") or ""
        images = []

        if body_format in ("md", "text"):
            plain = re.sub(r"(?is)<[^>]+>", "", body).strip()
            if not plain:
                for fallback in ("view", "storage"):
                    if fallback == source_format:
                        continue
                    try:
                        candidate = self.request(
                            f"/rest/api/content/{page_id}",
                            {
                                "expand": (
                                    f"body.{fallback},version,space,ancestors,"
                                    "metadata.labels,children.attachment"
                                )
                            },
                        )
                    except ConfluenceError:
                        break
                    value = ((candidate.get("body") or {}).get(fallback) or {}).get("value") or ""
                    if value.strip():
                        data = candidate
                        body = value
                        source_format = fallback
                        break

            if source_format == "storage":
                markdown = storage_to_markdown(body)
            else:
                markdown, images = html_to_markdown(body, self.base_url)

            if section:
                extracted, headings = extract_section(markdown, section)
                if extracted is None:
                    raise NotFoundError(
                        f"section not found: {section}",
                        hint="Available headings: " + ", ".join(headings[:20]),
                    )
                markdown = extracted

            output_body = markdown if body_format == "md" else markdown_to_text(markdown)
        else:
            output_body = body

        output_body, truncated = truncate_text(output_body, max_chars)
        version = data.get("version") or {}
        space = data.get("space") or {}
        labels = [
            item.get("name")
            for item in ((data.get("metadata") or {}).get("labels") or {}).get("results") or []
        ]
        attachment_count = (
            ((data.get("children") or {}).get("attachment") or {}).get("size") or 0
        )

        result = {
            "id": data.get("id"),
            "title": data.get("title"),
            "type": data.get("type"),
            "status": data.get("status"),
            "space": space.get("key"),
            "space_name": space.get("name"),
            "version": version.get("number"),
            "updated": version.get("when"),
            "updated_by": (version.get("by") or {}).get("displayName"),
            "url": self._absolute((data.get("_links") or {}).get("webui") or ""),
            "ancestors": [
                {"id": item.get("id"), "title": item.get("title")}
                for item in data.get("ancestors") or []
            ],
            "labels": labels,
            "body_format": body_format,
            "source_format": source_format,
            "body": output_body,
            "truncated": truncated,
            "images": images,
            "attachment_count": attachment_count,
        }
        if with_attachments:
            try:
                listing = self.list_attachments(page_id, all_results=True, max_results=1000)
                result["attachments"] = listing["results"]
            except ConfluenceError as exc:
                result["attachments_error"] = exc.message
        return result

    def read_pages(self, page_ids, max_chars=None, concurrency=4):
        results = [None] * len(page_ids)

        def fetch(index, page_id):
            try:
                results[index] = self.get_page(page_id, body_format="md", max_chars=max_chars)
            except ConfluenceError as exc:
                results[index] = {"id": page_id, "error": exc.to_dict()["error"]}

        workers = max(1, min(concurrency, len(page_ids) or 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(fetch, index, page_id)
                for index, page_id in enumerate(page_ids)
            ]
            for future in futures:
                future.result()

        errors = [item for item in results if isinstance(item, dict) and "error" in item]
        return {
            "count": len(results),
            "error_count": len(errors),
            "results": results,
        }

    # -- tree --------------------------------------------------------------

    def _paginate(self, path, query, max_results, page_size=100):
        items = []
        start = 0
        total = None
        truncated = False
        while len(items) < max_results:
            want = min(page_size, max_results - len(items))
            data = self.request(path, {**query, "limit": want, "start": start})
            batch = data.get("results") or []
            size = data.get("size", len(batch))
            total = data.get("totalSize", total)
            items.extend(batch)
            if not batch or size == 0:
                break
            if total is not None:
                if start + size >= total:
                    break
            elif size < want:
                break
            if len(items) >= max_results:
                truncated = bool(total is None or start + size < total)
                break
            start += size
        return {"items": items, "total": total, "truncated": truncated}

    def get_children(self, page_id, max_results=1000, page_size=100):
        return self._paginate(
            f"/rest/api/content/{page_id}/child/page",
            {"expand": "version,space"},
            max_results,
            page_size,
        )

    def resolve_space_root(self, space_key):
        data = self.request(
            f"/rest/api/space/{urllib.parse.quote(space_key)}",
            {"expand": "homepage"},
        )
        homepage = data.get("homepage") or {}
        if not homepage.get("id"):
            raise NotFoundError(f"space has no homepage: {space_key}")
        return homepage.get("id"), homepage.get("title")

    def tree(self, root_id=None, space=None, max_depth=None, max_pages=500):
        root_title = None
        if root_id is None:
            if not space:
                raise ConfigError("tree requires --id or --space")
            root_id, root_title = self.resolve_space_root(space)

        root_meta = self.page_meta(root_id)
        nodes = []
        truncated = False
        depth_limited = False
        errors = []
        seen = {root_id}
        queue = deque([(root_id, 0, None)])
        root_node = {
            "id": root_id,
            "title": root_meta.get("title") or root_title,
            "space": root_meta.get("space"),
            "version": root_meta.get("version"),
            "url": root_meta.get("url"),
            "depth": 0,
            "parent_id": None,
        }
        nodes.append(root_node)

        while queue and len(nodes) < max_pages:
            parent_id, depth, _ = queue.popleft()
            if max_depth is not None and depth >= max_depth:
                # Only report a depth cut when there really is something below.
                try:
                    probe = self.get_children(parent_id, max_results=1)
                except ConfluenceError as exc:
                    errors.append({"id": parent_id, "error": exc.to_dict()["error"]})
                    continue
                if probe["items"]:
                    depth_limited = True
                continue
            try:
                children = self.get_children(
                    parent_id,
                    max_results=max_pages - len(nodes),
                )
            except ConfluenceError as exc:
                errors.append({"id": parent_id, "error": exc.to_dict()["error"]})
                continue
            if children["truncated"]:
                truncated = True
            for child in children["items"]:
                child_id = child.get("id")
                if not child_id or child_id in seen:
                    continue
                seen.add(child_id)
                nodes.append(
                    {
                        "id": child_id,
                        "title": child.get("title"),
                        "space": (child.get("space") or {}).get("key"),
                        "version": (child.get("version") or {}).get("number"),
                        "url": self._absolute((child.get("_links") or {}).get("webui") or ""),
                        "depth": depth + 1,
                        "parent_id": parent_id,
                    }
                )
                queue.append((child_id, depth + 1, parent_id))
                if len(nodes) >= max_pages:
                    truncated = True
                    break

        return {
            "root": {"id": root_id, "title": root_node["title"], "space": root_node["space"]},
            "count": len(nodes),
            "truncated": truncated,
            "depth_limited": depth_limited,
            "errors": errors,
            "nodes": nodes,
        }

    # -- attachments -------------------------------------------------------

    def list_attachments(self, page_id, limit=None, all_results=False, max_results=1000):
        path = f"/rest/api/content/{page_id}/child/attachment"
        if all_results:
            page = self._paginate(path, {"expand": "version"}, max_results)
            items = page["items"]
            total = page["total"]
            truncated = page["truncated"]
        else:
            want = max(1, min(int(limit or 100), 500))
            data = self.request(path, {"limit": want, "expand": "version"})
            items = data.get("results") or []
            total = data.get("totalSize")
            truncated = _single_page_has_more(data, items, want)

        results = []
        for item in items:
            links = item.get("_links") or {}
            version = item.get("version") or {}
            results.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "media_type": (item.get("metadata") or {}).get("mediaType"),
                    "file_size": (item.get("extensions") or {}).get("fileSize"),
                    "download": self._absolute(links.get("download") or ""),
                    "webui": self._absolute(links.get("webui") or ""),
                    "version": version.get("number"),
                    "updated": version.get("when"),
                }
            )
        return {
            "page_id": str(page_id),
            "count": len(results),
            "total": total,
            "has_more": truncated,
            "results": results,
        }

    def download(self, url, target, overwrite=False):
        scheme, netloc = _origin(url)
        if scheme not in ("http", "https") or not netloc:
            raise ConfigError(
                f"refusing to download non-HTTP URL: {url}",
                hint="Only http:// and https:// download URLs are supported.",
            )
        target = pathlib.Path(target)
        if target.exists() and not overwrite:
            return {"path": str(target), "bytes": target.stat().st_size, "status": "skipped"}
        target.parent.mkdir(parents=True, exist_ok=True)

        # Credentials are only sent to the Confluence origin: an attachment
        # URL pointing at an external host must not receive the PAT.
        authenticated = (scheme, netloc) == _origin(self.base_url)
        if authenticated:
            modes = self._modes()
            opener = urllib.request.build_opener(
                SameOriginRedirectHandler(_origin(self.base_url))
            )
        else:
            modes = [None]
            opener = urllib.request.build_opener()

        last_error = None
        for mode_index, mode in enumerate(modes):
            attempt = 0
            while True:
                headers = {"Accept": "*/*"}
                if mode:
                    headers["Authorization"] = self._auth_headers(mode)["Authorization"]
                request = urllib.request.Request(url, headers=headers)
                try:
                    with opener.open(request, timeout=self.timeout) as response:
                        handle, temp_name = tempfile.mkstemp(
                            prefix=".download-", dir=str(target.parent)
                        )
                        written = 0
                        try:
                            with os.fdopen(handle, "wb") as out:
                                while True:
                                    chunk = response.read(65536)
                                    if not chunk:
                                        break
                                    out.write(chunk)
                                    written += len(chunk)
                            os.replace(temp_name, target)
                        finally:
                            if os.path.exists(temp_name):
                                os.unlink(temp_name)
                        if mode:
                            self.last_auth_mode = mode
                        return {"path": str(target), "bytes": written, "status": "written"}
                except urllib.error.HTTPError as exc:
                    if "refusing cross-origin redirect" in str(exc.reason):
                        exc.close()
                        raise NetworkError(
                            f"download redirected away from the Confluence origin: {url}",
                            url=url,
                            hint="Check the attachment URL; credentials are never sent elsewhere.",
                        )
                    if exc.code == 401 and mode and mode_index + 1 < len(modes):
                        last_error = self._map_http_error(exc, url, mode)
                        break
                    if exc.code in RETRY_STATUSES and attempt < self.retries:
                        last_error = self._map_http_error(exc, url, mode or "anonymous")
                        delay = self._retry_delay(exc, attempt)
                        exc.close()
                        time.sleep(delay)
                        attempt += 1
                        continue
                    raise self._map_http_error(exc, url, mode or "anonymous")
                except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                    last_error = NetworkError(f"download failed: {exc}", url=url)
                    if attempt < self.retries:
                        time.sleep(min(2**attempt, 8))
                        attempt += 1
                        continue
                    raise last_error
        raise last_error or NetworkError(f"download failed: {url}")

    def download_attachments(self, page_id, out_dir, overwrite=False, all_results=True):
        listing = self.list_attachments(page_id, all_results=all_results)
        out_path = pathlib.Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        saved = []
        skipped = []
        failed = []
        for item in listing["results"]:
            url = item.get("download")
            if not url:
                failed.append({"id": item.get("id"), "title": item.get("title"), "error": "no download URL"})
                continue
            filename = safe_filename(item.get("title"), fallback=f"attachment-{item.get('id')}")
            target = out_path / filename
            try:
                outcome = self.download(url, target, overwrite=overwrite)
            except ConfluenceError as exc:
                failed.append(
                    {"id": item.get("id"), "title": item.get("title"), "error": exc.message}
                )
                continue
            entry = {
                "id": item.get("id"),
                "title": item.get("title"),
                "path": outcome["path"],
                "bytes": outcome["bytes"],
            }
            if outcome["status"] == "skipped":
                skipped.append(entry)
            else:
                saved.append(entry)
        return {
            "page_id": str(page_id),
            "out": str(out_path),
            "saved": saved,
            "skipped": skipped,
            "failed": failed,
        }

    # -- spaces ------------------------------------------------------------

    def spaces(self, limit=25, all_results=False, max_results=500):
        if all_results:
            page = self._paginate(
                "/rest/api/space", {"expand": "homepage"}, max_results
            )
            items = page["items"]
            total = page["total"]
            truncated = page["truncated"]
        else:
            want = max(1, min(int(limit or 25), 500))
            data = self.request("/rest/api/space", {"limit": want, "expand": "homepage"})
            items = data.get("results") or []
            total = data.get("totalSize")
            truncated = _single_page_has_more(data, items, want)
        results = [
            {
                "key": item.get("key"),
                "name": item.get("name"),
                "type": item.get("type"),
                "status": item.get("status"),
                "homepage_id": (item.get("homepage") or {}).get("id"),
                "url": self._absolute(
                    (item.get("_links") or {}).get("webui")
                    or f"/display/{item.get('key')}"
                ),
            }
            for item in items
        ]
        return {
            "count": len(results),
            "total": total,
            "has_more": truncated,
            "results": results,
        }


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


def build_front_matter(meta):
    def quote(value):
        if value is None:
            return '""'
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'

    lines = ["---"]
    for key in ("id", "title", "space", "url"):
        lines.append(f"{key}: {quote(meta.get(key))}")
    if meta.get("version") is not None:
        lines.append(f"version: {meta.get('version')}")
    if meta.get("updated"):
        lines.append(f"updated: {quote(meta.get('updated'))}")
    if meta.get("parent_id"):
        lines.append(f"parent_id: {quote(meta.get('parent_id'))}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def export_path(out_dir, meta, extension):
    space = safe_filename(meta.get("space") or "_", fallback="_")
    title = meta.get("title") or ""
    page_id = safe_filename(meta.get("id") or "page", fallback="page")
    filename = f"{page_id}-{slugify(title)}{extension}"
    return pathlib.Path(out_dir) / space / filename


def collect_export_targets(client, args):
    targets = []
    seen = set()

    def add(meta):
        page_id = str(meta.get("id") or "")
        if not page_id or page_id in seen:
            return
        seen.add(page_id)
        targets.append(meta)

    # Target precedence: explicit CQL/query first, then --ids, then tree.
    if args.cql or args.query:
        found = client.search(
            query=args.query,
            cql=args.cql,
            space=args.space,
            label=args.label,
            limit=100,
            all_results=True,
            max_results=args.max_pages,
        )
        for item in found["results"]:
            add(item)
    elif args.ids:
        explicit = [value for value in re.split(r"[\s,]+", args.ids) if value]
        if explicit:
            def fetch(page_id):
                try:
                    return client.page_meta(page_id)
                except ConfluenceError as exc:
                    return {"id": page_id, "error": exc.to_dict()["error"]}

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, min(args.concurrency, len(explicit)))
            ) as pool:
                for meta in pool.map(fetch, explicit):
                    add(meta)
    elif args.id or args.space:
        tree = client.tree(
            root_id=args.id,
            space=args.space,
            max_depth=args.max_depth,
            max_pages=args.max_pages,
        )
        for node in tree["nodes"]:
            add(node)

    if not targets:
        raise ConfigError(
            "export requires --id, --ids, --space, --cql, or a query",
            hint="Run `tree` or `search` first to find page ids.",
        )
    return targets


def write_export_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".export-", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(content)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def run_export(client, args):
    out_dir = pathlib.Path(args.out).expanduser()
    extension = ".txt" if args.format == "text" else ".md"
    targets = collect_export_targets(client, args)

    if args.dry_run:
        planned = []
        for meta in targets:
            planned.append(
                {
                    "id": meta.get("id"),
                    "title": meta.get("title"),
                    "space": meta.get("space"),
                    "path": str(export_path(out_dir, meta, extension)),
                    "status": "planned",
                }
            )
        return {
            "out": str(out_dir),
            "dry_run": True,
            "total": len(planned),
            "planned": planned,
        }

    client_results = []

    def fetch_and_write(meta):
        if meta.get("error"):
            return {
                "id": meta.get("id"),
                "title": meta.get("title"),
                "path": None,
                "status": "failed",
                "error": meta["error"],
            }
        target = export_path(out_dir, meta, extension)
        if target.exists() and not args.overwrite:
            return {
                "id": meta.get("id"),
                "title": meta.get("title"),
                "space": meta.get("space"),
                "path": str(target),
                "status": "skipped",
            }
        try:
            page = client.get_page(
                meta.get("id"), body_format=args.format, max_chars=None
            )
        except ConfluenceError as exc:
            return {
                "id": meta.get("id"),
                "title": meta.get("title"),
                "path": str(target),
                "status": "failed",
                "error": exc.to_dict()["error"],
            }
        content = build_front_matter(
            {
                "id": page.get("id"),
                "title": page.get("title"),
                "space": page.get("space"),
                "url": page.get("url"),
                "version": page.get("version"),
                "updated": page.get("updated"),
                "parent_id": meta.get("parent_id"),
            }
        ) + page.get("body", "")
        write_export_file(target, content)
        entry = {
            "id": page.get("id"),
            "title": page.get("title"),
            "space": page.get("space"),
            "path": str(target),
            "bytes": len(content.encode("utf-8")),
            "status": "written",
        }
        if args.download_attachments:
            try:
                downloaded = client.download_attachments(
                    page.get("id"),
                    out_dir
                    / "attachments"
                    / safe_filename(page.get("id") or "page", fallback="page"),
                    overwrite=args.overwrite,
                )
                entry["attachments_saved"] = len(downloaded["saved"])
                if downloaded["failed"]:
                    entry["attachments_failed"] = downloaded["failed"]
            except ConfluenceError as exc:
                entry["attachments_error"] = exc.message
        return entry

    workers = max(1, min(args.concurrency, len(targets)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for entry in pool.map(fetch_and_write, targets):
            client_results.append(entry)

    written = [entry for entry in client_results if entry["status"] == "written"]
    skipped = [entry for entry in client_results if entry["status"] == "skipped"]
    failed = [entry for entry in client_results if entry["status"] == "failed"]
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "out": str(out_dir),
        "extension": extension,
        "total": len(client_results),
        "written": len(written),
        "skipped": len(skipped),
        "failed": len(failed),
        "files": client_results,
    }
    write_export_file(out_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return {
        "out": str(out_dir),
        "dry_run": False,
        "total": manifest["total"],
        "written": manifest["written"],
        "skipped": manifest["skipped"],
        "failed": failed,
        "manifest": str(out_dir / "manifest.json"),
    }


# ---------------------------------------------------------------------------
# Config commands
# ---------------------------------------------------------------------------


CONFIG_TEMPLATE = """# Confluence configuration for confluence-manager
# Generated by `confluence_api.py config init`.
# This file is read automatically; keep it private (chmod 600).

# Required: base URL of the Confluence instance, e.g. https://confluence.example.com
CONFLUENCE_BASE_URL=

# Recommended: personal access token, used with bearer auth.
# Generate it in Confluence: Profile > Settings > Personal Access Tokens.
CONFLUENCE_PAT=

# Optional: auto (default), bearer, or basic.
CONFLUENCE_AUTH_MODE=auto

# Basic auth only: your login username and password. A PAT is not a password.
# CONFLUENCE_USERNAME=
# CONFLUENCE_PASSWORD=

# Optional: request timeout in seconds and retry count.
# CONFLUENCE_TIMEOUT=30
# CONFLUENCE_RETRIES=2
"""


def config_target(args):
    if getattr(args, "local", False):
        return pathlib.Path.cwd() / ".env"
    return user_config_path()


def write_private_file(path, content):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".config-", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(content)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    os.chmod(path, 0o600)


def update_env_file(path, key, value):
    path = pathlib.Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pattern = re.compile(rf"^\s*(?:export\s+)?#?\s*{re.escape(key)}\s*=")
    replaced = False
    output = []
    for line in lines:
        if pattern.match(line):
            output.append(f"{key}={value}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"{key}={value}")
    write_private_file(path, "\n".join(output).rstrip() + "\n")


def run_config(args):
    command = args.config_command
    if command == "path":
        paths = [pathlib.Path.cwd() / ".env", user_config_path()]
        return {
            "paths": [
                {"path": str(path), "exists": path.exists(), "role": role}
                for path, role in (
                    (paths[0], "project (cwd/.env)"),
                    (paths[1], "user (default)"),
                )
            ]
        }
    if command == "init":
        target = config_target(args)
        if target.exists() and not args.force:
            raise ConfigError(
                f"config file already exists: {target}",
                hint="Use --force to overwrite, or edit the file directly.",
            )
        write_private_file(target, CONFIG_TEMPLATE)
        return {"path": str(target), "created": True, "mode": "0600"}
    if command == "set":
        if not args.assignment or "=" not in args.assignment:
            raise ConfigError("config set expects KEY=VALUE")
        key, value = args.assignment.split("=", 1)
        key = key.strip()
        if key not in CONFIG_KEYS:
            raise ConfigError(
                f"unsupported config key: {key}",
                hint="Allowed keys: " + ", ".join(sorted(CONFIG_KEYS)),
            )
        target = config_target(args)
        update_env_file(target, key, value)
        return {"path": str(target), "key": key, "updated": True, "mode": "0600"}
    if command == "show":
        try:
            settings = resolve_settings(args)
        except ConfigError as exc:
            return {
                "configured": False,
                "error": exc.to_dict()["error"],
                "paths": [
                    {"path": str(pathlib.Path.cwd() / ".env"), "exists": (pathlib.Path.cwd() / ".env").exists()},
                    {"path": str(user_config_path()), "exists": user_config_path().exists()},
                ],
            }
        payload = settings.sanitized()
        payload["configured"] = True
        return payload
    raise ConfigError(f"unknown config command: {command}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

GLOBAL_DEFAULTS = {
    "base_url": None,
    "pat": None,
    "password": None,
    "username": None,
    "auth_mode": None,
    "env_file": None,
    "timeout": None,
    "retries": None,
    "compact": False,
    "pretty": False,
    "debug": False,
}

COMMAND_ALIASES = {
    "get-page": "get",
    "search-pages": "search",
    "list-attachments": "attachments",
    "download-attachments": "download-attachments",
}


def _global_parser():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--base-url", default=argparse.SUPPRESS)
    parser.add_argument("--pat", default=argparse.SUPPRESS, help="Prefer CONFLUENCE_PAT over this flag.")
    parser.add_argument("--password", default=argparse.SUPPRESS)
    parser.add_argument("--username", default=argparse.SUPPRESS)
    parser.add_argument("--auth-mode", default=argparse.SUPPRESS, choices=["auto", "bearer", "basic"])
    parser.add_argument("--env-file", default=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--retries", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--compact", action="store_true", default=argparse.SUPPRESS, help="Accepted for compatibility; output is compact by default.")
    parser.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS, help="Pretty-print JSON output.")
    parser.add_argument("--debug", action="store_true", default=argparse.SUPPRESS, help="Include a traceback for unexpected errors.")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def build_parser():
    global_parser = _global_parser()
    parser = argparse.ArgumentParser(
        parents=[global_parser],
        description="Read, search, and export self-hosted Confluence content.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def sub(name, help_text, aliases=None):
        return subparsers.add_parser(
            name,
            help=help_text,
            aliases=aliases or [],
            parents=[global_parser],
        )

    sub("check", "Verify credentials and connectivity.")

    search = sub("search", "Search pages with CQL or natural-language text.", ["search-pages"])
    search.add_argument("query_pos", nargs="?", metavar="QUERY")
    search.add_argument("-q", "--query")
    search.add_argument("--cql")
    search.add_argument("--title")
    search.add_argument("--space")
    search.add_argument("--label")
    search.add_argument("--limit", type=int, default=None)
    search.add_argument("--start", type=int, default=0)
    search.add_argument("--all", action="store_true", dest="all_results")
    search.add_argument("--max-results", type=int, default=500)
    search.add_argument("--sort", choices=["relevance", "recent"], default="relevance")
    search.add_argument("--explain", action="store_true")

    get = sub("get", "Fetch one page as Markdown, text, or raw body.", ["get-page"])
    get.add_argument("ref", nargs="?", help="Page id or Confluence URL.")
    get.add_argument("--id")
    get.add_argument("--title")
    get.add_argument("--space")
    get.add_argument("--format", default="md", choices=["md", "text", "storage", "view", "export_view"])
    get.add_argument("--section", help="Only the section under this heading.")
    get.add_argument("--max-chars", type=int)
    get.add_argument("--out", help="Write the body to this file instead of stdout.")
    get.add_argument("--with-attachments", action="store_true")
    get.add_argument("--download-images", metavar="DIR")
    get.add_argument("--overwrite-images", action="store_true")

    read = sub("read", "Fetch several pages in one call.")
    read.add_argument("ids", nargs="*", help="Page ids or URLs.")
    read.add_argument("--ids", dest="ids_option", help="Comma-separated page ids.")
    read.add_argument("--cql")
    read.add_argument("--query", "-q")
    read.add_argument("--limit", type=int, default=5)
    read.add_argument("--max-chars", type=int)
    read.add_argument("--concurrency", type=int, default=4)

    tree = sub("tree", "List a page tree breadth-first.")
    tree.add_argument("--id")
    tree.add_argument("--space")
    tree.add_argument("--max-depth", type=int)
    tree.add_argument("--max-pages", type=int, default=500)

    export = sub("export", "Export pages to Markdown files with a manifest.")
    export.add_argument("--id")
    export.add_argument("--ids")
    export.add_argument("--space")
    export.add_argument("--cql")
    export.add_argument("--query", "-q")
    export.add_argument("--label")
    export.add_argument("--out", required=True)
    export.add_argument("--format", default="md", choices=["md", "text"])
    export.add_argument("--dry-run", action="store_true")
    export.add_argument("--overwrite", action="store_true")
    export.add_argument("--download-attachments", action="store_true")
    export.add_argument("--max-depth", type=int)
    export.add_argument("--max-pages", type=int, default=500)
    export.add_argument("--concurrency", type=int, default=4)

    attachments = sub("attachments", "List attachments for a page.", ["list-attachments"])
    attachments.add_argument("ref", nargs="?")
    attachments.add_argument("--id")
    attachments.add_argument("--limit", type=int, default=100)
    attachments.add_argument("--all", action="store_true", dest="all_results")
    attachments.add_argument("--max-results", type=int, default=1000)

    download = sub("download-attachments", "Download all attachments for a page.")
    download.add_argument("ref", nargs="?")
    download.add_argument("--id")
    download.add_argument("--out", required=True)
    download.add_argument("--overwrite", action="store_true")

    spaces = sub("spaces", "List Confluence spaces.")
    spaces.add_argument("--limit", type=int, default=25)
    spaces.add_argument("--all", action="store_true", dest="all_results")
    spaces.add_argument("--max-results", type=int, default=500)

    config = sub("config", "Inspect or edit configuration.")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    for name in ("show", "path", "init"):
        command = config_sub.add_parser(name, parents=[global_parser])
        if name in ("show", "init"):
            command.add_argument("--local", action="store_true")
            command.add_argument("--force", action="store_true")
    set_parser = config_sub.add_parser("set", parents=[global_parser])
    set_parser.add_argument("assignment", help="KEY=VALUE")
    set_parser.add_argument("--local", action="store_true")

    return parser


def normalize_args(args):
    for key, default in GLOBAL_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, default)
    if args.pretty:
        args.compact = False
    if getattr(args, "command", None) in COMMAND_ALIASES:
        args.command = COMMAND_ALIASES[args.command]
    if getattr(args, "timeout", None) is not None and args.timeout <= 0:
        raise ConfigError("timeout must be positive")
    if getattr(args, "retries", None) is not None and args.retries < 0:
        raise ConfigError("retries must be non-negative")
    return args


def page_ref_from_args(args):
    return getattr(args, "ref", None) or getattr(args, "id", None)


def dispatch(args, client, settings=None):
    command = args.command

    if command == "check":
        return client.check(settings)

    if command == "search":
        query = args.query or args.query_pos
        if not (query or args.cql or args.title or args.space or args.label):
            raise ConfigError(
                "search needs a query, --cql, --title, --space, or --label",
                hint="Example: search -q \"kubesphere 离线安装\" --space SC",
            )
        return client.search(
            query=query,
            cql=args.cql,
            title=args.title,
            space=args.space,
            label=args.label,
            limit=args.limit,
            start=args.start,
            explain=args.explain,
            all_results=args.all_results,
            max_results=args.max_results,
            sort=args.sort,
        )

    if command == "get":
        ref = page_ref_from_args(args)
        match = None
        candidates = None
        if ref:
            page_id = ConfluenceClient.parse_page_ref(ref)
            if not page_id:
                parsed = urllib.parse.urlparse(ref)
                display = re.match(r"/display/([^/]+)/([^/?#]+)", parsed.path)
                if not display:
                    raise ConfigError(
                        f"cannot parse page reference: {ref}",
                        hint="Pass a numeric page id, a URL containing /pages/<id>, or ?pageId=<id>.",
                    )
                space = urllib.parse.unquote(display.group(1))
                title = urllib.parse.unquote_plus(display.group(2))
                page_id, match, candidates = client.resolve_page_id(title, space=space)
        elif args.title:
            page_id, match, candidates = client.resolve_page_id(args.title, space=args.space)
        else:
            raise ConfigError("get requires a page id/URL or --title")

        if args.format not in ("md", "text"):
            if args.section:
                raise ConfigError(
                    "--section is only available for Markdown/text output; drop --format "
                    f"{args.format} or switch to --format md",
                )
            if args.download_images:
                raise ConfigError(
                    "--download-images requires Markdown/text output; drop --format "
                    f"{args.format} or switch to --format md",
                )

        result = client.get_page(
            page_id,
            body_format=args.format,
            section=args.section,
            max_chars=args.max_chars,
            with_attachments=args.with_attachments,
        )
        if match:
            result["match"] = match
        if candidates and len(candidates) > 1:
            result["ambiguous"] = True
            result["candidates"] = candidates
        if args.download_images:
            downloaded = []
            failed = []
            out_path = pathlib.Path(args.download_images)
            for image in result.get("images") or []:
                url = image.get("url")
                if not url or url.startswith("data:"):
                    continue
                filename = safe_filename(
                    urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]),
                    fallback=f"image-{len(downloaded) + 1}",
                )
                try:
                    outcome = client.download(url, out_path / filename, overwrite=args.overwrite_images)
                    downloaded.append({"url": url, "path": outcome["path"], "status": outcome["status"]})
                except ConfluenceError as exc:
                    failed.append({"url": url, "error": exc.message})
            result["downloaded_images"] = downloaded
            if failed:
                result["download_image_errors"] = failed
        if args.out:
            path = pathlib.Path(args.out).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(result.get("body") or "", encoding="utf-8")
            result["path"] = str(path)
            result["bytes"] = len((result.get("body") or "").encode("utf-8"))
            result["body"] = None
        return result

    if command == "read":
        explicit = []
        if getattr(args, "ids_option", None):
            explicit.extend(value for value in re.split(r"[\s,]+", args.ids_option) if value)
        for ref in args.ids or []:
            page_id = ConfluenceClient.parse_page_ref(ref) or ref
            explicit.append(page_id)
        if args.cql or args.query:
            found = client.search(
                query=args.query,
                cql=args.cql,
                limit=args.limit,
                all_results=True,
                max_results=args.limit,
            )
            explicit.extend(item["id"] for item in found["results"] if item.get("id"))
        page_ids = []
        seen = set()
        for page_id in explicit:
            if page_id not in seen:
                seen.add(page_id)
                page_ids.append(page_id)
        if not page_ids:
            raise ConfigError("read requires page ids, --ids, --cql, or --query")
        return client.read_pages(
            page_ids,
            max_chars=args.max_chars,
            concurrency=args.concurrency,
        )

    if command == "tree":
        return client.tree(
            root_id=args.id,
            space=args.space,
            max_depth=args.max_depth,
            max_pages=args.max_pages,
        )

    if command == "export":
        return run_export(client, args)

    if command == "attachments":
        ref = page_ref_from_args(args)
        if not ref:
            raise ConfigError("attachments requires a page id or URL")
        page_id = ConfluenceClient.parse_page_ref(ref) or ref
        return client.list_attachments(
            page_id,
            limit=args.limit,
            all_results=args.all_results,
            max_results=args.max_results,
        )

    if command == "download-attachments":
        ref = page_ref_from_args(args)
        if not ref:
            raise ConfigError("download-attachments requires a page id or URL")
        page_id = ConfluenceClient.parse_page_ref(ref) or ref
        return client.download_attachments(
            page_id,
            args.out,
            overwrite=args.overwrite,
        )

    if command == "spaces":
        return client.spaces(
            limit=args.limit,
            all_results=args.all_results,
            max_results=args.max_results,
        )

    raise ConfigError(f"unsupported command: {command}")


def write_json(payload, pretty=False):
    if pretty:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write(text + "\n")


def write_error(exc):
    sys.stderr.write(json.dumps(exc.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")


def main(argv=None):
    args = normalize_args(build_parser().parse_args(argv))
    if args.command == "config":
        write_json(run_config(args), pretty=args.pretty)
        return EXIT_OK
    settings = resolve_settings(args)
    client = ConfluenceClient(
        base_url=settings.base_url,
        pat=settings.pat,
        password=settings.password,
        auth_mode=settings.auth_mode,
        username=settings.username,
        timeout=settings.timeout,
        retries=settings.retries,
    )
    result = dispatch(args, client, settings=settings)
    write_json(result, pretty=args.pretty)
    return EXIT_OK


def run(argv=None):
    """Run the CLI and return an exit code (used by tests and __main__)."""
    debug = False
    try:
        raw_args = sys.argv[1:] if argv is None else list(argv)
        debug = "--debug" in raw_args
        return main(raw_args)
    except ConfluenceError as exc:
        write_error(exc)
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return EXIT_OK
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        if debug:
            import traceback

            traceback.print_exc()
        write_error(
            ConfluenceError(
                f"unexpected error: {exc}",
                hint="Re-run with --debug for a traceback.",
            )
        )
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(run())
