"""Run the automated part of the HD2-DG-LAB release acceptance matrix.

The tool deliberately keeps hardware, live-game, and human-safety checks manual.  A
passing exit code means only that every automated release gate passed; it never
claims that a DG-LAB device or a current game build was exercised.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import locale
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
LUA_VERSION_RE = re.compile(r"^local\s+VERSION\s*=\s*'([^']+)'", re.MULTILINE)
PY_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)


@dataclass(frozen=True)
class Result:
    check_id: str
    title: str
    status: str
    evidence: str


MANUAL_ROWS = (
    ("MAN-01", "Fluent 桌面在 Windows 10/11 + WebView2 启动，关闭后进程退出"),
    ("MAN-02", "F11 后台暂停/恢复与 F12 全局急停在失焦窗口下生效"),
    ("MAN-03", "启动检查向导只读，不会自动连接、检测或武装"),
    ("MAN-04", "Mock 下伤害合并、A/B 独立倍率、限幅和急停符合预期"),
    ("MAN-05", "游戏桥按 safe → net → menu → recon → live 分级验证"),
    ("MAN-06", "受支持游戏构建中血量、六肢、阵亡和复活与游戏一致（保持未武装）"),
    ("MAN-07", "真机仅由具备条件的验收者低强度检查；确认 App/设备端急停"),
    ("MAN-08", "上一稳定版桌面与对应桥可按文档完成回滚"),
)


def _run(command: list[str], cwd: Path = ROOT) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command, cwd=cwd, text=True, encoding=locale.getpreferredencoding(False), errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
    except OSError as exc:
        return False, f"无法执行 {command[0]}：{exc}"
    tail = completed.stdout.strip().splitlines()[-1:] or ["无输出"]
    return completed.returncode == 0, tail[0]


def _result(check_id: str, title: str, ok: bool, evidence: str) -> Result:
    return Result(check_id, title, "PASS" if ok else "FAIL", evidence)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_versions(root: Path = ROOT) -> Result:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    py = PY_VERSION_RE.search((root / "hd2coyote" / "__init__.py").read_text(encoding="utf-8"))
    lua = LUA_VERSION_RE.search((root / "lua" / "hd2_coyote_bridge.lua").read_text(encoding="utf-8"))
    ok = bool(VERSION_RE.fullmatch(version) and py and lua and py.group(1) == lua.group(1) == version)
    return _result("AUT-01", "版本号三处一致且符合 SemVer", ok,
                   f"VERSION={version}, Python={py.group(1) if py else 'missing'}, Lua={lua.group(1) if lua else 'missing'}")


def check_tests(python: str = sys.executable) -> Result:
    command = [python, "-m", "unittest", "discover", "-s", "tests", "-v"]
    try:
        completed = subprocess.run(
            command, cwd=ROOT, text=True, encoding=locale.getpreferredencoding(False),
            errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
    except OSError as exc:
        return _result("AUT-02", "完整 unittest 回归测试", False, f"无法执行测试：{exc}")
    count = re.search(r"Ran\s+(\d+)\s+tests?", completed.stdout)
    summary = re.search(r"(?m)^OK(?: \(skipped=(\d+)\))?[ \t]*$", completed.stdout)
    ok = completed.returncode == 0 and bool(summary)
    evidence = (f"Ran {count.group(1)} tests; OK"
                + (f" (skipped={summary.group(1)})" if summary and summary.group(1) else "")
                if ok and count else
                completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "无输出")
    return _result("AUT-02", "完整 unittest 回归测试", ok, evidence)


def check_frontend() -> Result:
    ok, evidence = _run(["node", "--check", "hd2coyote/frontend/app.js"])
    return _result("AUT-03", "前端 JavaScript 语法检查", ok, evidence)


def check_privacy(root: Path = ROOT) -> Result:
    ok, output = _run(["git", "ls-files"], cwd=root)
    if not ok:
        return _result("AUT-04", "Git 跟踪文件隐私清单", False, output)
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=root, text=True, encoding=locale.getpreferredencoding(False), errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    ).stdout.splitlines()
    forbidden = []
    for raw in tracked:
        name = raw.replace("\\", "/")
        lower = name.lower()
        if (lower in {"config.json", "hd2coyote.log", "bridge_config.lua", "bridge_config.lua.bak"}
                or lower.startswith(("ref/", "dist/", "build/"))
                or lower.endswith((".log", ".dmp", ".dump"))):
            forbidden.append(name)
    return _result("AUT-04", "Git 跟踪文件隐私清单", not forbidden,
                   "未跟踪运行配置、日志、ref、构建产物或转储" if not forbidden else "禁止项：" + ", ".join(forbidden))


def check_docs(root: Path = ROOT) -> Result:
    required = ["README.md", "SAFETY.md", "THIRD_PARTY.md", "CHANGELOG.md",
                "docs/DESKTOP.md", "docs/HOOK.md", "docs/UPGRADE.md", "docs/RELEASE_ACCEPTANCE.md"]
    missing = [name for name in required if not (root / name).is_file()]
    return _result("AUT-05", "发布、安全、更新与验收文档齐全", not missing,
                   "8 份必需文档存在" if not missing else "缺少：" + ", ".join(missing))


def _parse_sums(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    # Windows PowerShell 5 writes UTF-8 text with a BOM by default.
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        digest, sep, name = line.partition("  ")
        if not sep or not re.fullmatch(r"[0-9a-fA-F]{64}", digest) or not name:
            raise ValueError(f"非法校验行：{line!r}")
        values[name] = digest.lower()
    return values


def desktop_exe_name(version: str) -> str:
    """Return the versioned desktop artifact name required by Releases."""
    return f"HD2-DG-LAB-{version}.exe"


def check_artifacts(dist: Path, version: str) -> list[Result]:
    exe = dist / desktop_exe_name(version)
    bridge = dist / f"HD2-Coyote-Bridge-{version}.zip"
    exe_header = b""
    if exe.is_file():
        with exe.open("rb") as handle:
            exe_header = handle.read(2)
    exe_ok = exe.is_file() and exe.stat().st_size > 1024 * 1024 and exe_header == b"MZ"
    results = [_result("AUT-06", "Windows 桌面 EXE 存在且为非空 PE", exe_ok,
                       f"{exe.name}, {exe.stat().st_size if exe.exists() else 0} bytes")]

    zip_ok = False
    zip_evidence = f"缺少 {bridge.name}"
    if bridge.is_file():
        try:
            with zipfile.ZipFile(bridge) as archive:
                names = set(archive.namelist())
                manifest = json.loads(archive.read("manifest.json"))
                expected = "data/9ba626afa44a3aa3.patch_0"
                zip_ok = expected in names and manifest.get("Options", [{}])[0].get("Include") == ["data"]
                zip_evidence = f"manifest + {expected} + data Include 已验证"
        except (OSError, KeyError, IndexError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
            zip_evidence = f"桥 ZIP 无效：{exc}"
    results.append(_result("AUT-07", "桥 ZIP 布局与 Arsenal manifest", zip_ok, zip_evidence))

    required_docs = ["DESKTOP.md", "HOOK.md", "UPGRADE.md", "RELEASE_ACCEPTANCE.md", "RELEASE_NOTES.md"]
    missing_docs = [name for name in required_docs if not (dist / name).is_file()]
    results.append(_result("AUT-08", "发布包附带操作、回滚与验收文档", not missing_docs,
                           "5 份发布文档存在" if not missing_docs else "缺少：" + ", ".join(missing_docs)))

    sums_path = dist / "SHA256SUMS.txt"
    sums_ok = False
    sums_evidence = "缺少 SHA256SUMS.txt"
    if sums_path.is_file():
        try:
            sums = _parse_sums(sums_path)
            # The checksum file cannot hash itself.  Every other published file,
            # including the generated acceptance report, must be covered.
            release_files = [p for p in dist.iterdir() if p.is_file() and p.name != "SHA256SUMS.txt"]
            missing = [p.name for p in release_files if p.name not in sums]
            wrong = [p.name for p in release_files if p.name in sums and sha256(p) != sums[p.name]]
            extra = [name for name in sums if not (dist / name).is_file()]
            sums_ok = not missing and not wrong and not extra
            sums_evidence = "所有发布资产哈希匹配" if sums_ok else f"missing={missing}, wrong={wrong}, extra={extra}"
        except (OSError, ValueError) as exc:
            sums_evidence = f"校验文件无效：{exc}"
    results.append(_result("AUT-09", "SHA256SUMS 覆盖发布资产且全部匹配", sums_ok, sums_evidence))
    return results


def git_identity(root: Path = ROOT) -> tuple[str, str]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True,
            encoding="ascii", errors="replace", stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, check=False,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, text=True,
            encoding=locale.getpreferredencoding(False), errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        ).stdout.strip()
    except OSError:
        return "unknown", "unknown"
    return commit or "unknown", "dirty" if dirty else "clean"


def render_report(version: str, results: Iterable[Result], *, commit: str = "unknown",
                  tree_state: str = "unknown") -> str:
    rows = list(results)
    passed = all(row.status == "PASS" for row in rows)
    lines = [
        f"# HD2-DG-LAB {version} 发布验收报告", "",
        f"源码提交：`{commit}`；工作树：`{tree_state}`。", "",
        f"自动门禁结论：**{'PASS' if passed else 'FAIL'}**。",
        "", "> 自动门禁通过不代表实机、真机或人体安全验收已经完成。人工项必须由验收者记录环境和证据。", "",
        "## 自动门禁", "", "| ID | 项目 | 状态 | 证据 |", "| --- | --- | --- | --- |",
    ]
    for row in rows:
        evidence = row.evidence.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {row.check_id} | {row.title} | **{row.status}** | {evidence} |")
    lines += ["", "## 人工验收", "", "| ID | 项目 | 状态 | 验收环境/证据 |", "| --- | --- | --- | --- |"]
    for check_id, title in MANUAL_ROWS:
        lines.append(f"| {check_id} | {title} | PENDING | 由发布验收者填写 |")
    lines += ["", "矩阵定义、阻断规则和证据要求见 `docs/RELEASE_ACCEPTANCE.md`。", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    # Windows PowerShell/cmd often uses a legacy code page.  Never let a diagnostic
    # character crash the acceptance gate itself.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="执行发布验收矩阵中的自动门禁")
    parser.add_argument("--dist", type=Path, help="可选：验证已构建的发布目录")
    parser.add_argument("--report", type=Path, help="可选：写出 Markdown 验收报告")
    parser.add_argument("--skip-tests", action="store_true", help="仅供开发迭代；正式发布不得使用")
    args = parser.parse_args(argv)

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    results = [check_versions(ROOT)]
    if not args.skip_tests:
        results.append(check_tests(sys.executable))
    results.extend([check_frontend(), check_privacy(ROOT), check_docs(ROOT)])
    if args.dist:
        dist = args.dist if args.dist.is_absolute() else ROOT / args.dist
        results.extend(check_artifacts(dist, version))

    for row in results:
        print(f"[{row.status}] {row.check_id} {row.title} — {row.evidence}")
    if args.report:
        report = args.report if args.report.is_absolute() else ROOT / args.report
        report.parent.mkdir(parents=True, exist_ok=True)
        commit, tree_state = git_identity(ROOT)
        report.write_text(render_report(version, results, commit=commit, tree_state=tree_state), encoding="utf-8")
        print(f"验收报告：{report}")
    return 0 if all(row.status == "PASS" for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
