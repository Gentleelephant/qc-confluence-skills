#!/usr/bin/env python3
import argparse
import base64
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_EXPAND = "body.storage,version,space,ancestors"


def load_dotenv(path):
    values = {}
    dotenv_path = pathlib.Path(path)
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
        if value and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


class ConfluenceClient:
    def __init__(self, base_url, pat=None, auth_mode="auto", username=None, timeout=30):
        self.base_url = base_url.rstrip("/")
        self.pat = pat
        self.auth_mode = auth_mode
        self.username = username
        self.timeout = timeout

    def _auth_headers(self, mode):
        headers = {"Accept": "application/json"}
        if not self.pat:
            return headers
        if mode == "bearer":
            headers["Authorization"] = f"Bearer {self.pat}"
        elif mode == "basic":
            if not self.username:
                raise ValueError("CONFLUENCE_USERNAME is required for basic auth")
            token = base64.b64encode(f"{self.username}:{self.pat}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        else:
            raise ValueError(f"unsupported auth mode: {mode}")
        return headers

    def _request(self, path, query=None, raw=False):
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        modes = [None]
        if self.pat:
            if self.auth_mode == "auto":
                modes = ["bearer"]
                if self.username:
                    modes.append("basic")
            else:
                modes = [self.auth_mode]

        last_error = None
        for mode in modes:
            headers = self._auth_headers(mode) if mode else {"Accept": "application/json"}
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = resp.read()
                    if raw:
                        return data
                    content_type = resp.headers.get("Content-Type", "")
                    if "application/json" in content_type:
                        return json.loads(data.decode("utf-8"))
                    return {
                        "status": resp.status,
                        "content_type": content_type,
                        "body": data.decode("utf-8", errors="replace"),
                    }
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_error = {
                    "status": exc.code,
                    "reason": exc.reason,
                    "body": body,
                    "auth_mode": mode or "none",
                    "url": url,
                }
                if exc.code in (401, 403) and self.auth_mode == "auto" and mode != modes[-1]:
                    continue
                raise RuntimeError(json.dumps(last_error, ensure_ascii=False))
            except urllib.error.URLError as exc:
                raise RuntimeError(f"request failed: {exc}") from exc
        raise RuntimeError(json.dumps(last_error, ensure_ascii=False))

    def get_page(self, page_id, body_format="storage", expand=None):
        expand_value = expand or DEFAULT_EXPAND
        if f"body.{body_format}" not in expand_value:
            expand_value = f"body.{body_format}," + expand_value.replace("body.storage,", "").replace("body.view,", "")
        data = self._request(f"/rest/api/content/{page_id}", query={"expand": expand_value})
        body = data.get("body", {}).get(body_format, {}).get("value")
        return {
            "id": data.get("id"),
            "title": data.get("title"),
            "type": data.get("type"),
            "status": data.get("status"),
            "space": data.get("space", {}).get("key"),
            "version": data.get("version", {}).get("number"),
            "webui": self._webui(data.get("_links", {})),
            "ancestors": [
                {"id": item.get("id"), "title": item.get("title")}
                for item in data.get("ancestors", [])
            ],
            "body_format": body_format,
            "body": body,
            "raw": data,
        }

    def search_pages(self, title=None, cql=None, limit=10, body_format=None):
        if cql:
            query = {"cql": cql, "limit": limit}
            if body_format:
                query["expand"] = f"body.{body_format},version,space"
            data = self._request("/rest/api/content/search", query=query)
        else:
            query = {"type": "page", "limit": limit}
            if title:
                query["title"] = title
            if body_format:
                query["expand"] = f"body.{body_format},version,space"
            data = self._request("/rest/api/content", query=query)

        results = []
        for item in data.get("results", []):
            body = None
            if body_format:
                body = item.get("body", {}).get(body_format, {}).get("value")
            results.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "type": item.get("type"),
                    "status": item.get("status"),
                    "space": item.get("space", {}).get("key"),
                    "version": item.get("version", {}).get("number"),
                    "webui": self._webui(item.get("_links", {})),
                    "body_format": body_format,
                    "body": body,
                }
            )
        return {
            "count": len(results),
            "limit": limit,
            "results": results,
            "raw": data,
        }

    def list_attachments(self, page_id, limit=100):
        data = self._request(
            f"/rest/api/content/{page_id}/child/attachment",
            query={"limit": limit, "expand": "version"},
        )
        results = []
        for item in data.get("results", []):
            links = item.get("_links", {})
            results.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "mediaType": item.get("metadata", {}).get("mediaType"),
                    "fileSize": item.get("extensions", {}).get("fileSize"),
                    "download_path": self._download_path(links),
                    "download": self._download_url(links),
                    "webui": self._webui(links),
                    "version": item.get("version", {}).get("number"),
                }
            )
        return {
            "page_id": page_id,
            "count": len(results),
            "results": results,
            "raw": data,
        }

    def download_attachments(self, page_id, out_dir, limit=100):
        listing = self.list_attachments(page_id, limit=limit)
        out_path = pathlib.Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        saved = []
        for item in listing["results"]:
            if not item["download"]:
                continue
            data = self._request(item["download_path"], raw=True)
            filename = item["title"]
            target = out_path / filename
            target.write_bytes(data)
            saved.append(
                {
                    "id": item["id"],
                    "title": filename,
                    "path": str(target),
                    "bytes": len(data),
                }
            )
        return {
            "page_id": page_id,
            "saved": saved,
        }

    def _download_url(self, links):
        if "download" in links:
            return f"{self.base_url}{links['download']}"
        return None

    def _download_path(self, links):
        if "download" in links:
            return links["download"]
        return None

    def _webui(self, links):
        webui = links.get("webui")
        if not webui:
            return None
        return f"{self.base_url}{webui}"


def build_parser():
    parser = argparse.ArgumentParser(description="Fetch content from self-hosted Confluence.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--base-url")
    parser.add_argument("--pat")
    parser.add_argument("--auth-mode")
    parser.add_argument("--username")
    parser.add_argument("--timeout", type=int, default=30)

    subparsers = parser.add_subparsers(dest="command", required=True)

    get_page = subparsers.add_parser("get-page")
    get_page.add_argument("--id", required=True)
    get_page.add_argument("--body-format", default="storage", choices=["storage", "view", "export_view"])
    get_page.add_argument("--expand", default=None)

    search = subparsers.add_parser("search-pages")
    search.add_argument("--title")
    search.add_argument("--cql")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--body-format", choices=["storage", "view", "export_view"])

    attachments = subparsers.add_parser("list-attachments")
    attachments.add_argument("--id", required=True)
    attachments.add_argument("--limit", type=int, default=100)

    download = subparsers.add_parser("download-attachments")
    download.add_argument("--id", required=True)
    download.add_argument("--out", required=True)
    download.add_argument("--limit", type=int, default=100)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    dotenv_values = load_dotenv(args.env_file)

    base_url = (
        args.base_url
        or os.environ.get("CONFLUENCE_BASE_URL")
        or dotenv_values.get("CONFLUENCE_BASE_URL")
    )
    pat = (
        args.pat
        or os.environ.get("CONFLUENCE_PAT")
        or dotenv_values.get("CONFLUENCE_PAT")
    )
    auth_mode = (
        args.auth_mode
        or os.environ.get("CONFLUENCE_AUTH_MODE")
        or dotenv_values.get("CONFLUENCE_AUTH_MODE")
        or "auto"
    )
    username = (
        args.username
        or os.environ.get("CONFLUENCE_USERNAME")
        or dotenv_values.get("CONFLUENCE_USERNAME")
    )

    if not base_url:
        parser.error("missing --base-url or CONFLUENCE_BASE_URL")

    client = ConfluenceClient(
        base_url=base_url,
        pat=pat,
        auth_mode=auth_mode,
        username=username,
        timeout=args.timeout,
    )

    if args.command == "get-page":
        result = client.get_page(args.id, body_format=args.body_format, expand=args.expand)
    elif args.command == "search-pages":
        if not args.title and not args.cql:
            parser.error("search-pages requires --title or --cql")
        result = client.search_pages(
            title=args.title,
            cql=args.cql,
            limit=args.limit,
            body_format=args.body_format,
        )
    elif args.command == "list-attachments":
        result = client.list_attachments(args.id, limit=args.limit)
    elif args.command == "download-attachments":
        result = client.download_attachments(args.id, out_dir=args.out, limit=args.limit)
    else:
        parser.error(f"unsupported command: {args.command}")

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
