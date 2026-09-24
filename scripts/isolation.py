"""OS boundaries for public-network and private-offline workers; fail closed."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class IsolationUnavailable(RuntimeError):
    pass


def require_runtime():
    if sys.version_info < (3, 10):
        raise IsolationUnavailable(
            "需要 Python 3.10+；请使用宿主已有的兼容 Python 解释器再运行，未读取个人记忆"
        )


def backend():
    if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").exists():
        return "macos-seatbelt"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return "linux-bubblewrap"
    return None


def clean_environment():
    # In particular, do not inherit proxies, tokens, PYTHONPATH or startup hooks.
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "LANG": "en_US.UTF-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def command(argv, *, read=(), write=(), network=False):
    reads = {str(Path(p).resolve()) for p in read}
    writes = {str(Path(p).resolve()) for p in write}
    runtime = {sys.base_prefix, str(Path(sys.executable).resolve().parent.parent)}
    mode = backend()
    if mode == "macos-seatbelt":
        # No general /Users, /private, /Library or home-directory read grant.
        reads |= runtime | {
            "/System/Library",
            "/usr/lib",
            "/usr/share",
            "/usr/bin",
            "/Library/Apple/System/Library",
            "/opt/homebrew/lib",
            "/opt/homebrew/opt",
            "/opt/homebrew/etc/openssl@3",
            "/opt/homebrew/opt/sqlite/lib",
            "/opt/homebrew/opt/openssl@3/lib",
            "/opt/homebrew/opt/xz/lib",
            "/opt/homebrew/opt/mpdecimal/lib",
            "/opt/homebrew/opt/zstd/lib",
            "/opt/homebrew/etc/ca-certificates",
            "/private/etc/ssl",
            "/private/etc/resolv.conf",
            "/private/etc/hosts",
            "/private/var/run/resolv.conf",
        }
        rules = [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow file-read-metadata)",
            '(allow file-read* (literal "/"))',
            '(allow file-read* file-write* (literal "/dev/null"))',
            '(allow file-read* (literal "/dev/urandom") (literal "/dev/random"))',
        ]
        reads = {str(Path(path).resolve()) for path in reads}
        for path in sorted(reads | writes):
            rules.append(f"(allow file-read* (subpath {json.dumps(path)}))")
        for path in sorted(writes):
            rules.append(f"(allow file-write* (subpath {json.dumps(path)}))")
        if network:
            rules.append("(allow network*)")
        return ["/usr/bin/sandbox-exec", "-p", "\n".join(rules), *argv]
    if mode == "linux-bubblewrap":
        args = [
            shutil.which("bwrap"),
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
        ]
        if network:
            args += ["--share-net"]
        reads |= runtime | {
            "/usr",
            "/lib",
            "/lib64",
            "/etc/ssl",
            "/etc/resolv.conf",
            "/etc/hosts",
            "/etc/nsswitch.conf",
        }
        for path in sorted(reads - writes):
            if Path(path).exists():
                args += ["--ro-bind", path, path]
        for path in sorted(writes):
            args += ["--bind", path, path]
        return [*args, "--", *argv]
    raise IsolationUnavailable("当前系统没有可用的文件与网络隔离器；私有处理未启动")


def run(script, payload, *, read=(), write=(), network=False, timeout=50, args=()):
    require_runtime()
    script = Path(script).resolve()
    with tempfile.TemporaryDirectory(prefix="lsnu-worker-") as tmp:
        argv = command(
            [str(Path(sys.executable).resolve()), "-I", "-B", str(script), *args],
            read=[script.parent, *read],
            write=[tmp, *write],
            network=network,
        )
        env = clean_environment()
        env["TMPDIR"] = tmp
        env["LSNU_ISOLATED_WORKER"] = "1"
        result = subprocess.run(
            argv,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            env=env,
            cwd=tmp,
            timeout=timeout,
            check=False,
        )
    if result.returncode:
        # Worker stderr may contain paths or data. Never relay it to a cloud host.
        raise RuntimeError(f"隔离进程未成功完成（退出码 {result.returncode}）")
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("隔离进程没有返回有效结果") from exc
    if value.get("status") == "error":
        raise ValueError(value.get("error", "操作失败"))
    return value
