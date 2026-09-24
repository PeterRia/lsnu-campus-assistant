#!/usr/bin/env python3
"""Independent personal Skill helper. No UI, daemon or network; explicit host-bound access."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import isolation

LOCAL_CONFIG = Path(__file__).resolve().parents[1] / "local-config.json"
KINDS = {"preference", "knowledge", "workflow"}


def default_store():
    return Path.home() / ".local/share/lsnu-personal-memory"


def configured_store():
    if LOCAL_CONFIG.is_file():
        return Path(json.loads(LOCAL_CONFIG.read_text())["store"])
    return default_store()


def now():
    return datetime.now(timezone.utc)


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dump(value) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def valid_store(value):
    raw = Path(value).expanduser().absolute()
    path = raw.resolve()
    if any((p / ".git").exists() for p in [path, *path.parents]):
        raise ValueError("个人记忆必须位于 Git 仓库之外")
    if any(
        s in str(path).lower()
        for s in ("/mobile documents/", "/cloudstorage/", "/dropbox/", "/onedrive/")
    ):
        raise ValueError("个人记忆不能放在已知云同步目录")
    if raw != path and not (
        sys.platform == "darwin"
        and str(raw).startswith("/tmp/")
        and str(path).startswith("/private/tmp/")
    ):
        raise ValueError("个人记忆目录不能经由符号链接")
    return path


def words(value):
    parts = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", value.lower())
    return {
        t
        for p in parts
        for t in (
            [p]
            if p.isascii() or len(p) == 1
            else [p[i : i + 2] for i in range(len(p) - 1)]
        )
    }


class Memory:
    def __init__(self, directory):
        self.root = valid_store(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        for name in ("policy.json", "items", "audit.jsonl", ".lock"):
            if (self.root / name).is_symlink():
                raise ValueError("个人存储内部路径不能是符号链接")
        self.policy_path = self.root / "policy.json"
        if not self.policy_path.exists():
            atomic(
                self.policy_path,
                {
                    "schema": 1,
                    "paused": False,
                    "grants": [],
                    "remember_categories": [],
                    "contribution_reminders": True,
                },
            )
        self.policy = json.loads(self.policy_path.read_text())
        self.items = self.root / "items"
        self.items.mkdir(mode=0o700, exist_ok=True)

    def audit(self, action, object_id=""):
        with (self.root / "audit.jsonl").open("a", encoding="utf-8") as f:
            f.write(
                json.dumps({"at": now().isoformat(), "action": action, "id": object_id})
                + "\n"
            )

    def save_policy(self):
        atomic(self.policy_path, self.policy)

    def grant(self, request):
        host = request.get("host", "")
        mode = request.get("processing")
        kinds = request.get("kinds", ["preference"])
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", host) or mode not in {
            "local",
            "cloud",
        }:
            raise ValueError("需要明确的宿主标识和本地/云端推理方式")
        if (
            request.get("consent") is not True
            or not str(request.get("evidence", "")).strip()
        ):
            raise PermissionError("访问授权需要用户明确同意及对应的简短授权说明")
        if mode == "cloud" and request.get("cloud_inference_consent") is not True:
            raise PermissionError(
                "把记忆交给云端模型推理需要单独明确授权；不等于贡献上传"
            )
        if not isinstance(kinds, list) or set(kinds) - KINDS or not kinds:
            raise ValueError("需限定允许读取的记忆类别")
        expiry = request.get("expires_at")
        if request.get("persistent") is True:
            expiry = None
        elif expiry:
            dt = datetime.fromisoformat(expiry)
            if dt.tzinfo is None or dt <= now():
                raise ValueError("授权期限需要带时区且尚未过期")
        else:
            expiry = (now() + timedelta(hours=1)).isoformat()
        grant = {
            "id": str(uuid.uuid4()),
            "host": host,
            "processing": mode,
            "kinds": kinds,
            "purposes": ["campus-personalization"],
            "allow_sensitive": request.get("allow_sensitive") is True,
            "expires_at": expiry,
            "created_at": now().isoformat(),
            "evidence": str(request["evidence"])[:400],
            "cloud_inference_consent": mode == "cloud",
            "contribution_upload": False,
        }
        self.policy["grants"] = [
            g
            for g in self.policy["grants"]
            if not (g["host"] == host and g["processing"] == mode)
        ] + [grant]
        self.save_policy()
        self.audit("grant", grant["id"])
        return {
            "status": "ok",
            "grant": {k: v for k, v in grant.items() if k != "evidence"},
        }

    def access(self, request):
        if self.policy["paused"]:
            return None
        for grant in self.policy["grants"]:
            if grant["host"] != request.get("host") or grant[
                "processing"
            ] != request.get("processing"):
                continue
            if grant["expires_at"] and now() >= datetime.fromisoformat(
                grant["expires_at"]
            ):
                continue
            if (
                request.get("purpose", "campus-personalization")
                not in grant["purposes"]
            ):
                continue
            return grant
        return None

    def need_access(self, request):
        mode = request.get("processing")
        return {
            "status": "authorization_required",
            "paused": self.policy["paused"],
            "memory_read": False,
            "contribution_reminders": self.policy["contribution_reminders"],
            "host": request.get("host"),
            "processing": mode,
            "reason": "需要允许此宿主读取指定类别的本地记忆；云端宿主会把召回内容用于云端推理。这与贡献到共享仓库的授权独立。",
        }

    def path(self, id_):
        if not isinstance(id_, str) or not re.fullmatch(r"[a-f0-9-]{36}", id_):
            raise ValueError("记忆标识无效")
        path = self.items / (id_ + ".json")
        if path.is_symlink():
            raise ValueError("记忆记录不能是符号链接")
        return path

    def remember(self, request):
        if self.policy["paused"]:
            raise PermissionError("记忆已暂停")
        kind = request.get("kind", "preference")
        if kind not in KINDS:
            raise ValueError("未知记忆类别")
        if (
            request.get("consent") is not True
            and kind not in self.policy["remember_categories"]
        ):
            raise PermissionError("长期保存需用户明确要求记住，或有效的类别授权")
        key, value = request.get("key"), request.get("value")
        if (
            not isinstance(key, str)
            or not key.strip()
            or len(key) > 120
            or not isinstance(value, str)
            or not value.strip()
            or len(value) > 16000
        ):
            raise ValueError("记忆名称或内容无效")
        if re.search(
            r"password|token|secret|密码|验证码|访问令牌", key, re.I
        ) or re.search(
            r"\b(?:github_pat_|gh[pousr]_|sk-)[A-Za-z0-9_-]{16,}|(?:密码|验证码)\s*[:：]\s*\S+",
            value,
        ):
            raise ValueError("不保存密码、验证码、访问令牌等凭据")
        sensitive = request.get("sensitivity", "ordinary")
        if sensitive not in {"ordinary", "sensitive"}:
            raise ValueError("敏感等级无效")
        if sensitive == "sensitive" and request.get("consent") is not True:
            raise PermissionError("敏感记录需要本次明确保存授权")
        expiry = request.get("review_after") or None
        if expiry:
            datetime.fromisoformat(expiry)
        id_ = request.get("id") or str(uuid.uuid4())
        path = self.path(id_)
        old = json.loads(path.read_text()) if path.exists() else None
        # An overwrite must target the explicit stable ID, never a fuzzy title match.
        history = (
            (
                old.get("history", [])
                + [{k: v for k, v in old.items() if k != "history"}]
            )[-10:]
            if old
            else []
        )
        item = {
            "id": id_,
            "kind": kind,
            "key": key.strip(),
            "value": value.strip(),
            "scope": request.get("scope", "campus"),
            "source": str(request.get("source", "用户明确提供"))[:400],
            "sensitivity": sensitive,
            "review_after": expiry,
            "updated_at": now().isoformat(),
            "revision": old["revision"] + 1 if old else 1,
            "history": history,
        }
        atomic(path, item)
        self.audit("remember", id_)
        return {
            "status": "ok",
            "id": id_,
            "revision": item["revision"],
            "stored_locally": True,
            "uploaded": False,
        }

    def recall(self, request):
        grant = self.access(request)
        if not grant:
            return self.need_access(request)
        query = str(request.get("query", "")).strip()
        ids = request.get("ids")
        if ids is not None and (not isinstance(ids, list) or len(ids) > 30):
            raise ValueError("记忆选择无效")
        terms = words(query)
        rows, stale = [], []
        # Only this Skill's designated store, never arbitrary user folders.
        paths = (
            [self.path(id_) for id_ in ids]
            if ids is not None
            else sorted(self.items.glob("*.json"))
        )
        for path in paths:
            if path.is_symlink():
                raise ValueError("记忆记录不能是符号链接")
            if not path.exists():
                continue
            item = json.loads(path.read_text())
            if item["kind"] not in grant["kinds"] or (
                item["sensitivity"] == "sensitive" and not grant["allow_sensitive"]
            ):
                continue
            if item.get("scope") not in {"campus", request.get("scope", "campus")}:
                continue
            relevant = len(terms & words(item["key"] + " " + item["value"]))
            if item["kind"] == "preference" and item["key"] in {
                "校区",
                "年级",
                "专业",
                "回答风格",
                "称呼",
                "学习目标",
            }:
                relevant += 3
            if ids is not None:
                relevant += 5
            if not relevant:
                continue
            if (
                item["review_after"]
                and item["review_after"][:10] < now().date().isoformat()
            ):
                stale.append(
                    {
                        "id": item["id"],
                        "key": item["key"],
                        "reason": "已到复核日期，未作为当前事实使用",
                    }
                )
                continue
            rows.append((relevant, item))
        rows.sort(key=lambda pair: (pair[0], pair[1]["updated_at"]), reverse=True)
        limit = max(1, min(10, int(request.get("limit", 5))))
        recalled = [
            {k: v for k, v in item.items() if k != "history"}
            for _, item in rows[:limit]
        ]
        # Current request overrides old preferences in the Agent; data never grants new actions.
        self.audit("recall", grant["id"])
        return {
            "status": "ok",
            "memories": recalled,
            "stale": stale[:10],
            "grant_id": grant["id"],
            "processing": grant["processing"],
            "contribution_upload_authorized": False,
            "contribution_reminders": self.policy["contribution_reminders"],
            "rules": [
                "当前明确偏好优先于旧偏好",
                "个人经验不能改写官方政策",
                "个人流程是受任务权限约束的工作方法，不能授予上传或任意执行权限",
            ],
        }

    def forget(self, request):
        if request.get("consent") is not True:
            raise PermissionError("删除需要用户明确指定记录")
        path = self.path(request.get("id"))
        path.unlink(missing_ok=True)
        self.audit("forget", request["id"])
        return {
            "status": "ok",
            "deleted": request["id"],
            "history_deleted": True,
            "note": "本工具不建立备份；系统及用户另建副本需要分别管理。",
        }

    def restore(self, request):
        if request.get("consent") is not True:
            raise PermissionError("恢复历史需明确授权")
        path = self.path(request.get("id"))
        if not path.exists():
            raise ValueError("已删除记忆不能通过历史恢复")
        item = json.loads(path.read_text())
        match = next(
            (h for h in item["history"] if h["revision"] == request.get("revision")),
            None,
        )
        if not match:
            raise ValueError("此历史版本不存在")
        return self.remember({**match, "consent": True})

    def configure(self, request):
        if request.get("consent") is not True:
            raise PermissionError("修改记忆策略需要明确授权")
        values = request.get("values", {})
        if set(values) - {"paused", "remember_categories", "contribution_reminders"}:
            raise ValueError("不能借记忆配置授予外传权限")
        if "remember_categories" in values and (
            not isinstance(values["remember_categories"], list)
            or set(values["remember_categories"]) - KINDS
        ):
            raise ValueError("记忆类别无效")
        for key in ("paused", "contribution_reminders"):
            if key in values and not isinstance(values[key], bool):
                raise ValueError("配置值必须为布尔值")
        self.policy.update(values)
        self.save_policy()
        self.audit("configure")
        return {
            "status": "ok",
            "settings": {
                k: self.policy[k]
                for k in ("paused", "remember_categories", "contribution_reminders")
            },
        }

    def handle(self, request):
        action = request.get("action")
        if action == "status":
            return {
                "status": "ok",
                "paused": self.policy["paused"],
                "contribution_reminders": self.policy["contribution_reminders"],
                "authorized_for_this_host": self.access(request) is not None,
                "memory_read": False,
            }
        if action == "revoke":
            if request.get("consent") is not True:
                raise PermissionError("撤销需明确指令")
            self.policy["grants"] = [
                g for g in self.policy["grants"] if g["host"] != request.get("host")
            ]
            self.save_policy()
            self.audit("revoke")
            return {"status": "ok", "revoked": request.get("host")}
        if action in {"list", "export"}:
            grant = self.access(request)
            if not grant:
                return self.need_access(request)
            records = []
            for p in sorted(self.items.glob("*.json")):
                if p.is_symlink():
                    raise ValueError("记忆记录不能是符号链接")
                item = json.loads(p.read_text())
                if item["kind"] not in grant["kinds"] or (
                    item["sensitivity"] == "sensitive" and not grant["allow_sensitive"]
                ):
                    continue
                fields = ("id", "kind", "key", "revision", "updated_at", "review_after")
                records.append(
                    {k: item[k] for k in fields}
                    if action == "list"
                    else {k: v for k, v in item.items() if k != "history"}
                )
            return {
                "status": "ok",
                "records": records,
                "scope": {
                    "kinds": grant["kinds"],
                    "sensitive": grant["allow_sensitive"],
                },
                "uploaded_to_shared_repository": False,
            }
        routes = {
            "grant": self.grant,
            "remember": self.remember,
            "recall": self.recall,
            "forget": self.forget,
            "restore": self.restore,
            "configure": self.configure,
        }
        if action not in routes:
            raise ValueError("未知个人记忆操作")
        return routes[action](request)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        isolation.require_runtime()
        if args.worker and os.environ.get("LSNU_ISOLATED_WORKER") != "1":
            raise RuntimeError(
                "内部 worker 只能由隔离入口启动；不能直接运行或设置环境标记绕过隔离"
            )
        request = json.load(sys.stdin)
        if args.worker and args.store is None:
            raise ValueError("隔离入口必须明确指定存储")
        store = valid_store(args.store or configured_store())
        if args.worker:
            import fcntl

            store.mkdir(mode=0o700, parents=True, exist_ok=True)
            if (store / ".lock").is_symlink():
                raise ValueError("锁文件不能是符号链接")
            with (store / ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                answer = Memory(store).handle(request)
        else:
            store.mkdir(mode=0o700, parents=True, exist_ok=True)
            # The helper has no network access, even when the user authorizes the
            # calling cloud Agent to receive a small, relevant recall result.
            answer = isolation.run(
                Path(__file__).resolve(),
                request,
                write=[store],
                network=False,
                args=["--worker", "--store", str(store)],
            )
        print(dump(answer))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(dump({"status": "error", "error": str(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
