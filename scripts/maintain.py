#!/usr/bin/env python3
"""Refresh reviewed card checksums, validate package links, or build a clean skill ZIP."""

import argparse
import hashlib
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path

from campus import ROOT, card_text, digest, doctor, load_catalog


def files():
    from public_update import safe_relative

    allowed = json.loads((ROOT / "PUBLIC-FILES.json").read_text(encoding="utf-8"))
    if not isinstance(allowed, list) or len(allowed) != len(set(allowed)):
        raise ValueError("公共文件允许清单无效")
    for name in sorted(allowed):
        rel = safe_relative(name)
        path = ROOT / str(rel)
        if any((ROOT / str(parent)).is_symlink() for parent in [rel, *rel.parents]):
            raise ValueError("公共文件路径不能经过符号链接")
        if not path.resolve().is_relative_to(ROOT.resolve()) or not path.is_file():
            raise ValueError("允许清单中的文件缺失或越界: " + name)
        yield path


def check_links():
    errors = []
    for path in files():
        if path.suffix != ".md":
            continue
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            if not (path.parent / target.split("#")[0]).exists():
                errors.append(f"{path.relative_to(ROOT)}: missing {target}")
    return errors


def package(output):
    dest = Path(output).expanduser().resolve()
    if dest.exists():
        raise ValueError("发布包已存在；明确移走旧包或使用新文件名")
    errors = doctor()["errors"] + check_links()
    if errors:
        raise ValueError("; ".join(errors))
    dest.parent.mkdir(parents=True, exist_ok=True)
    hashes = {}
    with tempfile.NamedTemporaryFile(
        dir=dest.parent, suffix=".zip", delete=False
    ) as tmp:
        pending = Path(tmp.name)
    with zipfile.ZipFile(pending, "w", zipfile.ZIP_DEFLATED) as z:
        for path in files():
            data = path.read_bytes()
            rel = path.relative_to(ROOT).as_posix()
            hashes[rel] = hashlib.sha256(data).hexdigest()
            info = zipfile.ZipInfo(
                "lsnu-compus-skill/" + rel, date_time=(2026, 9, 23, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
        info = zipfile.ZipInfo(
            "lsnu-compus-skill/PACKAGE-SHA256.json",
            date_time=(2026, 9, 23, 0, 0, 0),
        )
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        z.writestr(
            info,
            json.dumps(hashes, ensure_ascii=False, indent=2) + "\n",
        )
    pending.replace(dest)
    return {
        "status": "ok",
        "zip": str(dest),
        "files": len(hashes),
        "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
    }


def release(output, manifest_path):
    from public_update import PERMISSIONS, REPO, version

    value = (ROOT / "VERSION").read_text().strip()
    version(value)
    result = package(Path(output) / f"lsnu-compus-skill-{value}.zip")
    manifest = {
        "schema_version": 1,
        "skill_id": "lsnu-compus-skill",
        "version": value,
        "python_min": "3.10",
        "permissions": PERMISSIONS,
        "archive_url": f"https://github.com/{REPO}/releases/download/v{value}/lsnu-compus-skill-{value}.zip",
        "archive_sha256": result["sha256"],
    }
    Path(manifest_path).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (Path(output) / "SHA256SUMS.txt").write_text(
        result["sha256"] + "  " + Path(result["zip"]).name + "\n"
    )
    return {**result, "manifest": str(manifest_path), "version": value}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("refresh-hashes")
    s.add_parser("check")
    z = s.add_parser("package")
    z.add_argument("--output", required=True)
    r = s.add_parser("release")
    r.add_argument("--output", required=True)
    r.add_argument("--manifest", required=True)
    a = p.parse_args()
    try:
        if a.cmd == "refresh-hashes":
            c = load_catalog()
            for card in c["cards"]:
                card["card_sha256"] = digest(card_text(card))
            (ROOT / "kb/catalog.json").write_text(
                json.dumps(c, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            result = {
                "status": "ok",
                "note": "只刷新文件校验，不更改核查日或宣布政策有效。",
            }
        elif a.cmd == "check":
            result = doctor()
            result["errors"] += check_links()
            result["status"] = "error" if result["errors"] else "ok"
        elif a.cmd == "release":
            result = release(a.output, a.manifest)
        else:
            result = package(a.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] == "error":
            sys.exit(2)
    except (OSError, ValueError, KeyError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
