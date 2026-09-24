#!/usr/bin/env python3
"""Conversation-driven contribution drafts and exact-payload approvals; no personal-store access."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolation
from contribution import REPO
from memory import atomic, valid_store

DEFAULT_DRAFTS = Path.home() / ".local/share/lsnu-campus-contributions"


def now():
    return datetime.now(timezone.utc)


def digest(payload):
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def network(request):
    executable = shutil.which("gh")
    if not executable:
        raise ValueError("没有 GitHub CLI，保留本地草稿；未上传")
    result = subprocess.run(
        [executable, "auth", "token"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise ValueError("GitHub 尚未登录，保留本地草稿；未上传")
    return isolation.run(
        Path(__file__).with_name("contribution.py"),
        {**request, "token": result.stdout.strip()},
        network=True,
    )


class Contributions:
    def __init__(self, directory):
        self.root = valid_store(directory)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def path(self, id_):
        try:
            id_ = str(uuid.UUID(id_))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("草稿标识无效") from exc
        path = self.root / (id_ + ".json")
        if path.is_symlink():
            raise ValueError("草稿不能是符号链接")
        return path

    def preview(self, id_):
        value = json.loads(self.path(id_).read_text())
        return {
            **value,
            "status": "ok",
            "submission_body": value["payload"]["body"]
            + "\n\n<!-- lsnu-contribution:"
            + value["id"]
            + ":"
            + value["digest"]
            + " -->",
        }

    def draft(self, request):
        if request.get("shareable_confirmed") is not True:
            raise PermissionError("需先核对有分享权、完成最小化与脱敏")
        title, body, identity = (
            request.get("title"),
            request.get("body"),
            request.get("identity"),
        )
        if (
            any(
                not isinstance(v, str) or not v.strip() for v in (title, body, identity)
            )
            or len(title) > 150
            or len(body) > 20000
        ):
            raise ValueError("需要完整标题、正文和已核对的 GitHub 身份")
        payload = {
            "title": title,
            "body": body,
            "repository": REPO,
            "visibility": "public",
            "identity": identity,
            "object": "GitHub issue, pending maintainer review",
        }
        hash_ = digest(payload)
        for path in self.root.glob("*.json"):
            if path.is_symlink():
                raise ValueError("草稿不能是符号链接")
            previous = json.loads(path.read_text())
            if previous.get("digest") == hash_:
                return self.preview(previous["id"])
        id_ = str(uuid.uuid4())
        value = {
            "id": id_,
            "payload": payload,
            "digest": hash_,
            "state": "draft",
            "approved_until": None,
            "receipt": None,
        }
        atomic(self.path(id_), value)
        return self.preview(id_)

    def decide(self, request):
        value = self.preview(request.get("id"))
        value.pop("submission_body")
        value.pop("status")
        if value["state"] in {"submitted", "sending", "uncertain"}:
            raise ValueError("已开始提交的草稿只能核对回执，不能改写授权")
        if request.get("decision") == "decline":
            value.update(state="declined", approved_until=None)
        elif request.get("decision") == "approve":
            if (
                request.get("consent") is not True
                or not str(request.get("evidence", "")).strip()
                or request.get("digest") != value["digest"]
            ):
                raise PermissionError("上传需要用户针对本次完整预览和目的地的明确批准")
            value.update(
                state="approved",
                approved_until=(now() + timedelta(minutes=30)).isoformat(),
                approval_evidence=str(request["evidence"])[:400],
            )
        else:
            raise ValueError("未知授权决定")
        atomic(self.path(value["id"]), value)
        return {
            "status": "ok",
            "id": value["id"],
            "state": value["state"],
            "digest": value["digest"],
            "approved_until": value["approved_until"],
            "uploaded": False,
        }

    def submit(self, request, transport=network):
        value = self.preview(request.get("id"))
        value.pop("submission_body")
        value.pop("status")
        if (
            digest(value["payload"]) != value["digest"]
            or request.get("digest") != value["digest"]
        ):
            raise PermissionError("内容或目的地已变化，需要新的预览与授权")
        if value["state"] == "submitted":
            return value["receipt"]
        if (
            value["state"] not in {"approved", "sending", "uncertain"}
            or not value["approved_until"]
            or now() >= datetime.fromisoformat(value["approved_until"])
        ):
            raise PermissionError("没有有效的本次上传授权")
        check_only = value["state"] in {"sending", "uncertain"}
        value["state"] = "sending"
        atomic(self.path(value["id"]), value)
        try:
            result = transport(
                {
                    "id": value["id"],
                    "payload": value["payload"],
                    "digest": value["digest"],
                    "check_only": check_only,
                }
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            result = {
                "status": "ok",
                "submission_status": "uncertain",
                "note": "尚未取得可靠回执；再次调用只核对，不重复发布。",
            }
        value["state"] = (
            "submitted"
            if result.get("submission_status") == "submitted_for_review"
            else "uncertain"
        )
        value["receipt"] = result
        atomic(self.path(value["id"]), value)
        return result

    def handle(self, request):
        action = request.get("action")
        if action == "identity":
            return network({"action": "identity"})
        if action == "preview":
            return self.preview(request.get("id"))
        routes = {"draft": self.draft, "decide": self.decide, "submit": self.submit}
        if action not in routes:
            raise ValueError("未知贡献操作")
        return routes[action](request)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drafts", type=Path, default=DEFAULT_DRAFTS)
    args = parser.parse_args()
    try:
        import fcntl

        root = valid_store(args.drafts)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (root / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = Contributions(root).handle(json.load(sys.stdin))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
