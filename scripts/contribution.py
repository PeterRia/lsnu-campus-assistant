"""GitHub contribution transport: only an approved payload, never a private directory."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.parse
import urllib.request

REPO = "PeterRia/lsnu-compus-skill"


def api(method, route, token, body=None):
    if not route.startswith(("/user", f"/repos/{REPO}/issues")):
        raise ValueError("提交目的地不在允许范围内")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        "https://api.github.com" + route,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "lsnu-campus-contribution/1",
        },
    )

    # Redirects are disabled so authentication cannot escape api.github.com.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args):
            raise ValueError("贡献 API 不允许重定向")

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=15) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("GitHub 返回过大")
        return json.loads(raw)


def send(request, transport=api):
    token = request.get("token")
    if not isinstance(token, str) or not token:
        raise ValueError("需要已登录 GitHub；凭据只在本次本地进程中使用")
    identity = transport("GET", "/user", token)["login"]
    if request.get("action") == "identity":
        return {
            "status": "ok",
            "identity": identity,
            "repository": REPO,
            "visibility": "public",
        }
    payload = request["payload"]
    digest = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    if (
        request.get("digest") != digest
        or payload.get("repository") != REPO
        or payload.get("visibility") != "public"
        or payload.get("identity") != identity
    ):
        raise PermissionError("内容、目的地或当前 GitHub 身份与授权预览不一致")
    if not re.fullmatch(r"[a-f0-9-]{36}", request.get("id", "")):
        raise ValueError("贡献标识无效")
    marker = f"<!-- lsnu-contribution:{request['id']}:{digest} -->"
    route = f"/repos/{REPO}/issues"
    # After an ambiguous failure, reconcile only. Never blindly issue a second POST.
    if request.get("check_only"):
        items = transport(
            "GET",
            route + "?state=all&per_page=100&creator=" + urllib.parse.quote(identity),
            token,
        )
        matches = [
            v
            for v in items
            if marker in (v.get("body") or "") and v.get("title") == payload["title"]
        ]
        if not matches:
            return {
                "status": "ok",
                "submission_status": "manual_check_required",
                "note": "未确认前次结果；没有再次提交。请在 GitHub 核对。",
            }
        receipt = matches[0]
    else:
        receipt = transport(
            "POST",
            route,
            token,
            {"title": payload["title"], "body": payload["body"] + "\n\n" + marker},
        )
    url = receipt.get("html_url", "")
    if not url.startswith(f"https://github.com/{REPO}/issues/"):
        raise ValueError("缺少可核对的 GitHub 回执")
    return {
        "status": "ok",
        "submission_status": "submitted_for_review",
        "url": url,
        "number": receipt["number"],
        "merged_or_published": False,
    }


if __name__ == "__main__":
    try:
        answer = send(json.load(sys.stdin))
    except Exception:
        answer = {
            "status": "error",
            "error": "提交或身份核验未完成；请核对 GitHub 状态，勿重复创建。",
        }
    print(json.dumps(answer, ensure_ascii=False))
