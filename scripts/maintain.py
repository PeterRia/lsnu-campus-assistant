#!/usr/bin/env python3
"""Refresh reviewed card checksums, validate package links, or build a clean skill ZIP."""

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

from campus import ROOT, card_text, digest, doctor, load_catalog

TOP_FILES = {
    "SKILL.md",
    "README.md",
    "LICENSE",
    "NOTICE.md",
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    ".gitignore",
}
FOLDERS = {
    "agents",
    "scripts",
    "references",
    "kb",
    "docs",
    "examples",
    "tests",
    ".github",
}


def files():
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if not path.is_file() or path.is_symlink() or "__pycache__" in rel.parts:
            continue
        if (
            (len(rel.parts) == 1 and rel.name in TOP_FILES) or rel.parts[0] in FOLDERS
        ) and path.suffix not in {".pyc", ".log"}:
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
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for path in files():
            data = path.read_bytes()
            rel = path.relative_to(ROOT).as_posix()
            hashes[rel] = hashlib.sha256(data).hexdigest()
            info = zipfile.ZipInfo(
                "lsnu-campus-assistant/" + rel, date_time=(2026, 9, 23, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
        info = zipfile.ZipInfo(
            "lsnu-campus-assistant/PACKAGE-SHA256.json",
            date_time=(2026, 9, 23, 0, 0, 0),
        )
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        z.writestr(
            info,
            json.dumps(hashes, ensure_ascii=False, indent=2) + "\n",
        )
    return {
        "status": "ok",
        "zip": str(dest),
        "files": len(hashes),
        "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("refresh-hashes")
    s.add_parser("check")
    z = s.add_parser("package")
    z.add_argument("--output", required=True)
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
