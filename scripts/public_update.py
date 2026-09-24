"""Trusted-origin, pull-only updates. Run in a sandbox with no private-directory grant."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SKILL = "lsnu-campus-assistant"
REPO = "PeterRia/lsnu-campus-assistant"
MANIFEST_URL = f"https://raw.githubusercontent.com/{REPO}/main/release.json"
PERMISSIONS = ["public-network", "public-cache-write"]
MAX_ARCHIVE = 12 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024


def stamp():
    return datetime.now(timezone.utc).isoformat()


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("版本必须为三段数字")
    return tuple(int(x) for x in value.split("."))


def safe_relative(value):
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise ValueError("非法发布路径")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(p in {".", ".."} for p in path.parts)
    ):
        raise ValueError("发布路径越界")
    if path.as_posix() != value or any(ord(c) < 32 for c in value):
        raise ValueError("发布路径不规范")
    return path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def check_url(url, *, manifest=False):
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or p.username or p.password or p.port not in (None, 443):
        raise ValueError("更新源必须使用可信 HTTPS 地址")
    if manifest:
        if url != MANIFEST_URL:
            raise ValueError("更新清单不属于固定的可信仓库")
    elif not (
        (
            p.hostname == "github.com"
            and p.path.startswith(f"/{REPO}/releases/download/")
        )
        or p.hostname == "release-assets.githubusercontent.com"
    ):
        raise ValueError("拒绝向未授权更新主机跳转")


class TrustedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, limit, *, manifest=False, etag=None):
    check_url(url, manifest=manifest)
    headers = {
        "User-Agent": "lsnu-campus-assistant-updater/1",
        "Accept": "application/json" if manifest else "application/octet-stream",
    }
    if etag:
        headers["If-None-Match"] = etag
    # No inherited proxy credentials; no cookie jar, query text or user profile.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), TrustedRedirect()
    )
    try:
        with opener.open(
            urllib.request.Request(url, headers=headers), timeout=12
        ) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError("更新内容超过大小限制")
            return data, response.headers.get("ETag")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, etag
        raise


def verify_manifest(manifest):
    if manifest.get("schema_version") != 1 or manifest.get("skill_id") != SKILL:
        raise ValueError("更新清单格式或技能标识不匹配")
    version(manifest.get("version"))
    if manifest.get("permissions") != PERMISSIONS:
        raise PermissionError("更新声明了不同权限，需审阅后另行授权")
    minimum = manifest.get("python_min", "3.10")
    if not re.fullmatch(r"\d+\.\d+", minimum):
        raise ValueError("Python 兼容性声明无效")
    if sys.version_info[:2] < tuple(map(int, minimum.split("."))):
        raise ValueError("更新需要更高版本 Python")
    expected = f"https://github.com/{REPO}/releases/download/v{manifest['version']}/{SKILL}-{manifest['version']}.zip"
    if manifest.get("archive_url") != expected:
        raise ValueError("发布包不属于对应版本的可信仓库")
    if not re.fullmatch(r"[a-f0-9]{64}", manifest.get("archive_sha256", "")):
        raise ValueError("发布包校验值无效")


def unpack_verified(data, manifest, target):
    verify_manifest(manifest)
    if hashlib.sha256(data).hexdigest() != manifest["archive_sha256"]:
        raise ValueError("发布包 SHA-256 不一致")
    target = Path(target).resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > 1500 or sum(i.file_size for i in infos) > MAX_EXPANDED:
            raise ValueError("发布包展开大小异常")
        names = [i.filename for i in infos]
        if len(names) != len(set(names)):
            raise ValueError("发布包包含重复文件")
        prefix = SKILL + "/"
        for item in infos:
            if not item.filename.startswith(prefix) or item.is_dir():
                raise ValueError("发布包目录结构不正确")
            safe_relative(item.filename[len(prefix) :])
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                raise ValueError("发布包不能包含链接或特殊文件")
        hashes = json.loads(archive.read(prefix + "PACKAGE-SHA256.json"))
        allowed = json.loads(archive.read(prefix + "PUBLIC-FILES.json"))
        if not isinstance(allowed, list) or len(set(allowed)) != len(allowed):
            raise ValueError("公共文件清单无效")
        for rel in allowed:
            safe_relative(rel)
        if set(hashes) != set(allowed) or set(names) != {
            prefix + p for p in [*allowed, "PACKAGE-SHA256.json"]
        }:
            raise ValueError("发布包与逐文件允许清单不一致")
        if archive.read(prefix + "VERSION").decode().strip() != manifest["version"]:
            raise ValueError("发布包内部版本不一致")
        for rel, expected in hashes.items():
            content = archive.read(prefix + rel)
            if hashlib.sha256(content).hexdigest() != expected:
                raise ValueError("包内文件校验失败")
            if rel.endswith(".py"):
                try:
                    ast.parse(content, filename=rel)
                except SyntaxError as exc:
                    raise ValueError("更新包含无法解析的 Python 文件") from exc
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
            dest.chmod(0o644)
        (target / "PACKAGE-SHA256.json").write_text(
            json.dumps(hashes), encoding="utf-8"
        )
    return target


def verified_root(path):
    path = Path(path).resolve()
    hashes = json.loads((path / "PACKAGE-SHA256.json").read_text())
    for rel, expected in hashes.items():
        candidate = path / str(safe_relative(rel))
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(path):
            raise ValueError("已缓存的公共文件路径越界")
        if hashlib.sha256(candidate.read_bytes()).hexdigest() != expected:
            raise ValueError("已缓存公共版本发生变化")
    return path


def current_root(cache, installed):
    pointer = Path(cache) / "current.json"
    if pointer.exists():
        info = json.loads(pointer.read_text())
        current = Path(cache) / "versions" / str(safe_relative(info["directory"]))
        if not current.resolve().is_relative_to((Path(cache) / "versions").resolve()):
            raise ValueError("公共版本指针越界")
        return verified_root(current)
    return Path(installed).resolve()


def _update(payload, fetch=download):
    cache, installed = (
        Path(payload["cache"]).resolve(),
        Path(payload["installed"]).resolve(),
    )
    cache.mkdir(parents=True, exist_ok=True)
    # Recover from interruption by ignoring any non-activated staging directories.
    root = current_root(cache, installed)
    local_version = (root / "VERSION").read_text().strip()
    result = {
        "status": "ok",
        "checked_at": stamp(),
        "source": MANIFEST_URL,
        "public_root": str(root),
        "version": local_version,
    }
    cached_path = cache / "remote.json"
    saved = json.loads(cached_path.read_text()) if cached_path.exists() else {}
    try:
        raw, etag = fetch(
            MANIFEST_URL, 128 * 1024, manifest=True, etag=saved.get("etag")
        )
        manifest = saved.get("manifest") if raw is None else json.loads(raw)
        if not isinstance(manifest, dict):
            raise ValueError("上游返回了无效版本清单")
        verify_manifest(manifest)
        result["remote_version"] = manifest["version"]
        if version(manifest["version"]) <= version(local_version):
            result["update_status"] = (
                "current" if manifest["version"] == local_version else "local_newer"
            )
        else:
            data, _ = fetch(manifest["archive_url"], MAX_ARCHIVE)
            with tempfile.TemporaryDirectory(prefix="stage-", dir=cache) as stage:
                candidate = unpack_verified(data, manifest, Path(stage) / SKILL)
                name = manifest["version"] + "-" + manifest["archive_sha256"][:12]
                dest = cache / "versions" / name
                dest.parent.mkdir(exist_ok=True)
                if dest.exists():
                    verified_root(dest)
                else:
                    os.replace(candidate, dest)
                previous = (
                    json.loads((cache / "current.json").read_text())
                    if (cache / "current.json").exists()
                    else None
                )
                atomic_json(
                    cache / "current.json",
                    {"directory": name, "previous": previous, "activated_at": stamp()},
                )
            result.update(
                update_status="updated",
                public_root=str(dest),
                version=manifest["version"],
            )
        atomic_json(cached_path, {"etag": etag, "manifest": manifest})
    except PermissionError:
        result["update_status"] = "permission_review_required"
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        result["update_status"] = "unavailable_using_local"
    atomic_json(cache / "last-check.json", result)
    return result


def update(payload, fetch=download):
    # Supported isolation backends are POSIX. Serialize multiple host invocations.
    import fcntl

    cache = Path(payload["cache"]).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / ".update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _update(payload, fetch)


def rollback(payload):
    import fcntl

    cache = Path(payload["cache"]).resolve()
    with (cache / ".update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pointer = cache / "current.json"
        value = json.loads(pointer.read_text())
        previous = value.get("previous")
        if not previous:
            raise ValueError("没有可回滚的已验证缓存版本；原安装包未被覆盖")
        verified_root(cache / "versions" / str(safe_relative(previous["directory"])))
        atomic_json(pointer, previous)
    return {"status": "ok", "update_status": "rolled_back"}


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        answer = (
            rollback(request)
            if request.get("action") == "rollback"
            else update(request)
        )
    except Exception:
        # Never emit fetched content or response headers into the host conversation.
        answer = {"status": "error", "error": "公共更新检查失败，原安装文件未被覆盖"}
    print(json.dumps(answer, ensure_ascii=False))
