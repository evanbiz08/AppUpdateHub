#!/usr/bin/env python3
"""Local-only release console for AppUpdateHub."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import webbrowser
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "apps.json"
INDEX_PATH = ROOT / "index.html"
MAX_JSON_BYTES = 128 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class ConsoleError(Exception):
    pass


def load_config() -> dict[str, Any]:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsoleError(f"无法读取 apps.json：{exc}") from exc

    if config.get("schemaVersion") != 1:
        raise ConsoleError("apps.json schemaVersion 必须为 1")
    repository = config.get("repository")
    apps = config.get("apps")
    if not isinstance(repository, dict) or not isinstance(apps, list) or not apps:
        raise ConsoleError("apps.json 缺少 repository 或 apps 配置")

    seen: set[str] = set()
    for app in apps:
        app_id = app.get("id")
        if not isinstance(app_id, str) or not SAFE_ID_RE.fullmatch(app_id) or app_id in seen:
            raise ConsoleError(f"无效或重复的应用 ID：{app_id}")
        seen.add(app_id)
        for key in (
            "name",
            "archivePrefix",
            "feedPath",
            "downloadsPath",
            "rawDownloadPrefix",
            "sparkleToolsPath",
            "keyAccount",
        ):
            if not isinstance(app.get(key), str) or not app[key].strip():
                raise ConsoleError(f"应用 {app_id} 缺少 {key}")
        resolve_repo_path(app["feedPath"])
        resolve_repo_path(app["downloadsPath"])
    return config


def resolve_repo_path(relative: str) -> Path:
    path = (ROOT / relative).resolve()
    try:
        path.relative_to(ROOT)
    except ValueError as exc:
        raise ConsoleError(f"路径必须位于仓库内：{relative}") from exc
    return path


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stdout.strip() or f"退出码 {result.returncode}"
        raise ConsoleError(f"命令执行失败：{' '.join(command[:3])}\n{detail}")
    return result


def git_status() -> dict[str, Any]:
    config = load_config()
    repository = config["repository"]
    branch = run(["git", "branch", "--show-current"]).stdout.strip()
    porcelain = run(["git", "status", "--porcelain=v1", "--untracked-files=all"]).stdout
    changes = [line for line in porcelain.splitlines() if line.strip()]
    upstream = run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
        check=False,
    ).stdout.strip()
    return {
        "branch": branch,
        "expectedBranch": repository["branch"],
        "remote": repository["remote"],
        "upstream": upstream,
        "clean": not changes,
        "changes": changes,
    }


def app_by_id(config: dict[str, Any], app_id: str) -> dict[str, Any]:
    for app in config["apps"]:
        if app["id"] == app_id:
            return app
    raise ConsoleError(f"未知应用：{app_id}")


def latest_release(app: dict[str, Any]) -> dict[str, str] | None:
    feed = resolve_repo_path(app["feedPath"])
    if not feed.exists():
        return None
    try:
        import xml.etree.ElementTree as ET

        root = ET.parse(feed).getroot()
        item = root.find("./channel/item")
        if item is None:
            return None
        sparkle_ns = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"
        return {
            "version": item.findtext(f"{sparkle_ns}shortVersionString", ""),
            "build": item.findtext(f"{sparkle_ns}version", ""),
            "publishedAt": item.findtext("pubDate", ""),
        }
    except (OSError, ET.ParseError):
        return None


def public_apps(config: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for app in config["apps"]:
        result.append(
            {
                "id": app["id"],
                "name": app["name"],
                "bundleIdentifier": app.get("bundleIdentifier", ""),
                "feedPath": app["feedPath"],
                "latest": latest_release(app),
            }
        )
    return result


def ensure_ready_for_publish(config: dict[str, Any]) -> None:
    status = git_status()
    if status["branch"] != status["expectedBranch"]:
        raise ConsoleError(
            f"当前分支为 {status['branch'] or 'detached HEAD'}，请切换到 {status['expectedBranch']}"
        )
    if not status["clean"]:
        raise ConsoleError("仓库存在未提交改动，请先提交或清理后再发布，避免夹带文件")
    remote = config["repository"]["remote"]
    branch = config["repository"]["branch"]
    run(["git", "pull", "--ff-only", remote, branch])


def inspect_archive(path: Path, app: dict[str, Any], requested_version: str) -> dict[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            for name in names:
                parts = Path(name).parts
                if name.startswith("/") or ".." in parts:
                    raise ConsoleError("ZIP 中包含不安全路径")
            plist_names = []
            for name in names:
                if name.startswith("__MACOSX/"):
                    continue
                parts = Path(name).parts
                app_indexes = [index for index, part in enumerate(parts) if part.endswith(".app")]
                # Ignore nested helper apps such as Sparkle's Updater.app.
                if len(app_indexes) == 1 and parts[app_indexes[0] + 1 :] == ("Contents", "Info.plist"):
                    plist_names.append(name)
            if len(plist_names) != 1:
                raise ConsoleError("ZIP 必须且只能包含一个 .app")
            info = archive.getinfo(plist_names[0])
            if info.file_size > 1024 * 1024:
                raise ConsoleError("Info.plist 文件异常")
            plist = plistlib.loads(archive.read(plist_names[0]))
    except (zipfile.BadZipFile, OSError, plistlib.InvalidFileException) as exc:
        raise ConsoleError(f"无法读取 ZIP：{exc}") from exc

    actual_version = str(plist.get("CFBundleShortVersionString", ""))
    build = str(plist.get("CFBundleVersion", ""))
    bundle_id = str(plist.get("CFBundleIdentifier", ""))
    if actual_version != requested_version:
        raise ConsoleError(f"页面版本 {requested_version} 与 ZIP 内版本 {actual_version or '缺失'} 不一致")
    expected_bundle = app.get("bundleIdentifier")
    if expected_bundle and bundle_id != expected_bundle:
        raise ConsoleError(f"ZIP Bundle ID 为 {bundle_id or '缺失'}，预期 {expected_bundle}")
    if not build:
        raise ConsoleError("ZIP 内缺少 CFBundleVersion")
    return {"version": actual_version, "build": build, "bundleIdentifier": bundle_id}


class ReleaseStore:
    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(32)
        self.uploads: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def prepare(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._purge_expired_uploads()
        config = load_config()
        ensure_ready_for_publish(config)
        app_id = str(payload.get("appId", ""))
        app = app_by_id(config, app_id)
        version = str(payload.get("version", "")).strip()
        notes = str(payload.get("notes", "")).strip()
        filename = Path(str(payload.get("fileName", ""))).name
        try:
            file_size = int(payload.get("fileSize", 0))
        except (TypeError, ValueError) as exc:
            raise ConsoleError("文件大小无效") from exc
        if not VERSION_RE.fullmatch(version):
            raise ConsoleError("版本号必须使用 1.2.3 格式")
        if not notes:
            raise ConsoleError("请填写更新内容")
        if not filename.lower().endswith(".zip"):
            raise ConsoleError("请选择 ZIP 更新包")
        if file_size <= 0 or file_size > MAX_ARCHIVE_BYTES:
            raise ConsoleError("ZIP 文件大小无效或超过 2GB")

        downloads = resolve_repo_path(app["downloadsPath"])
        archive_name = f"{app['archivePrefix']}-v{version}.zip"
        if (downloads / archive_name).exists():
            raise ConsoleError(f"版本 {version} 已存在，禁止覆盖已发布文件")

        upload_id = secrets.token_urlsafe(18)
        temp = tempfile.NamedTemporaryFile(prefix="app-update-hub-", suffix=".zip", delete=False)
        temp.close()
        self.uploads[upload_id] = {
            "createdAt": time.time(),
            "appId": app_id,
            "version": version,
            "notes": notes,
            "fileName": filename,
            "fileSize": file_size,
            "tempPath": temp.name,
            "uploaded": False,
        }
        return {"uploadId": upload_id}

    def upload(self, upload_id: str, stream: Any, content_length: int) -> dict[str, Any]:
        record = self.uploads.get(upload_id)
        if record is None:
            raise ConsoleError("上传任务不存在或已过期")
        if content_length != record["fileSize"]:
            raise ConsoleError("上传文件大小与选择的文件不一致")
        remaining = content_length
        with open(record["tempPath"], "wb") as output:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ConsoleError("ZIP 上传不完整")
                output.write(chunk)
                remaining -= len(chunk)

        config = load_config()
        app = app_by_id(config, record["appId"])
        try:
            details = inspect_archive(Path(record["tempPath"]), app, record["version"])
        except Exception:
            self._remove_upload(upload_id)
            raise
        record["uploaded"] = True
        record["archive"] = details
        return details

    def publish(self, upload_id: str) -> dict[str, Any]:
        record = self.uploads.get(upload_id)
        if record is None or not record.get("uploaded"):
            raise ConsoleError("请先完成 ZIP 上传和校验")
        with self.lock:
            config = load_config()
            ensure_ready_for_publish(config)
            app = app_by_id(config, record["appId"])
            result = self._publish_record(config, app, record)
        self._remove_upload(upload_id)
        return result

    def _publish_record(
        self, config: dict[str, Any], app: dict[str, Any], record: dict[str, Any]
    ) -> dict[str, Any]:
        downloads = resolve_repo_path(app["downloadsPath"])
        feed = resolve_repo_path(app["feedPath"])
        downloads.mkdir(parents=True, exist_ok=True)
        feed.parent.mkdir(parents=True, exist_ok=True)
        archive_name = f"{app['archivePrefix']}-v{record['version']}.zip"
        archive_path = downloads / archive_name
        notes_path = downloads / f"{app['archivePrefix']}-v{record['version']}.md"
        if archive_path.exists() or notes_path.exists():
            raise ConsoleError(f"版本 {record['version']} 已存在，禁止覆盖")

        sparkle_tools = (ROOT / app["sparkleToolsPath"]).resolve()
        generate_appcast = sparkle_tools / "generate_appcast"
        if not generate_appcast.is_file() or not os.access(generate_appcast, os.X_OK):
            raise ConsoleError(f"找不到 Sparkle generate_appcast：{generate_appcast}")

        try:
            shutil.copy2(record["tempPath"], archive_path)
            notes_path.write_text(record["notes"].rstrip() + "\n", encoding="utf-8")
            command = [
                str(generate_appcast),
                "--account",
                app["keyAccount"],
                "--download-url-prefix",
                app["rawDownloadPrefix"],
                "--embed-release-notes",
                "--maximum-versions",
                str(app.get("maximumVersions", 3)),
                "-o",
                str(feed),
                str(downloads),
            ]
            appcast_output = run(command).stdout.strip()
            relative_feed = str(feed.relative_to(ROOT))
            relative_downloads = str(downloads.relative_to(ROOT))
            run(["git", "add", "-A", "--", relative_feed, relative_downloads])
            staged = run(["git", "diff", "--cached", "--name-only"]).stdout.splitlines()
            allowed_roots = (relative_feed, relative_downloads + "/")
            unexpected = [
                path
                for path in staged
                if path != allowed_roots[0] and not path.startswith(allowed_roots[1])
            ]
            if unexpected:
                raise ConsoleError(f"检测到非发布文件进入暂存区：{', '.join(unexpected)}")
            if not staged:
                raise ConsoleError("没有生成可提交的发布变化")
            commit_message = f"release({app['id']}): v{record['version']}"
            commit_output = run(["git", "commit", "-m", commit_message]).stdout.strip()
        except Exception:
            self._rollback_uncommitted(app)
            raise

        remote = config["repository"]["remote"]
        branch = config["repository"]["branch"]
        push = run(["git", "push", remote, branch], check=False)
        if push.returncode != 0:
            raise ConsoleError(
                "发布已在本地提交，但 push 失败。请修复网络或权限后执行 git push。\n"
                + push.stdout.strip()
            )
        return {
            "app": app["name"],
            "version": record["version"],
            "build": record["archive"]["build"],
            "commit": commit_output.splitlines()[-1] if commit_output else commit_message,
            "push": push.stdout.strip(),
            "appcast": appcast_output,
        }

    def _rollback_uncommitted(self, app: dict[str, Any]) -> None:
        feed = app["feedPath"]
        downloads = app["downloadsPath"]
        status = run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", feed, downloads],
            check=False,
        ).stdout
        for entry in status.split("\0"):
            if not entry or entry[:2] != "??":
                continue
            relative = entry[3:]
            target = resolve_repo_path(relative)
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        run(["git", "restore", "--staged", "--", feed, downloads], check=False)
        run(["git", "restore", "--worktree", "--", feed, downloads], check=False)

    def _purge_expired_uploads(self) -> None:
        cutoff = time.time() - 2 * 60 * 60
        for upload_id, record in list(self.uploads.items()):
            if record["createdAt"] < cutoff:
                self._remove_upload(upload_id)

    def _remove_upload(self, upload_id: str) -> None:
        record = self.uploads.pop(upload_id, None)
        if record:
            Path(record["tempPath"]).unlink(missing_ok=True)


STORE = ReleaseStore()


class Handler(BaseHTTPRequestHandler):
    server_version = "AppUpdateHub/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._send_bytes(INDEX_PATH.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            try:
                config = load_config()
                self._send_json(
                    {
                        "ok": True,
                        "token": STORE.token,
                        "apps": public_apps(config),
                        "repository": git_status(),
                    }
                )
            except ConsoleError as exc:
                self._send_error(str(exc))
            return
        self._send_error("资源不存在", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self._authorize():
            return
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/releases":
                self._send_json({"ok": True, **STORE.prepare(self._read_json())})
                return
            match = re.fullmatch(r"/api/releases/([A-Za-z0-9_-]+)/publish", path)
            if match:
                self._send_json({"ok": True, **STORE.publish(match.group(1))})
                return
            self._send_error("接口不存在", HTTPStatus.NOT_FOUND)
        except ConsoleError as exc:
            self._send_error(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_error(f"内部错误：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self) -> None:
        if not self._authorize():
            return
        path = urllib.parse.urlsplit(self.path).path
        match = re.fullmatch(r"/api/releases/([A-Za-z0-9_-]+)/archive", path)
        if not match:
            self._send_error("接口不存在", HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_ARCHIVE_BYTES:
                raise ConsoleError("上传大小无效")
            details = STORE.upload(match.group(1), self.rfile, length)
            self._send_json({"ok": True, **details})
        except (ValueError, ConsoleError) as exc:
            self._send_error(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_error(f"内部错误：{exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def _authorize(self) -> bool:
        origin = self.headers.get("Origin")
        allowed_origins = {
            f"http://127.0.0.1:{self.server.server_port}",
            f"http://localhost:{self.server.server_port}",
        }
        if origin and origin not in allowed_origins:
            self._send_error("拒绝非本机页面请求", HTTPStatus.FORBIDDEN)
            return False
        if self.headers.get("X-AppUpdateHub-Token") != STORE.token:
            self._send_error("本机会话已失效，请刷新页面", HTTPStatus.FORBIDDEN)
            return False
        return True

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_JSON_BYTES:
            raise ConsoleError("请求内容大小无效")
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConsoleError("请求 JSON 无效") from exc
        if not isinstance(payload, dict):
            raise ConsoleError("请求 JSON 必须为对象")
        return payload

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _send_error(self, message: str, status: HTTPStatus = HTTPStatus.INTERNAL_SERVER_ERROR) -> None:
        self._send_json({"ok": False, "error": message}, status)

    def _send_bytes(
        self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)


def check_environment() -> None:
    config = load_config()
    if not INDEX_PATH.is_file():
        raise ConsoleError("缺少 index.html")
    run(["git", "rev-parse", "--show-toplevel"])
    for app in config["apps"]:
        tool = (ROOT / app["sparkleToolsPath"]).resolve() / "generate_appcast"
        if not tool.is_file():
            raise ConsoleError(f"{app['name']} 缺少 Sparkle 工具：{tool}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AppUpdateHub 本机发布控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost"):
        raise SystemExit("安全限制：服务只能监听 127.0.0.1 或 localhost")
    try:
        check_environment()
    except ConsoleError as exc:
        raise SystemExit(f"启动失败：{exc}") from exc
    if args.check:
        print("AppUpdateHub configuration: OK")
        return

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"AppUpdateHub 发布控制台：{url}")
    print("仅监听本机；按 Control-C 停止。")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
