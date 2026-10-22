# Explicit resource allowlist: never collect the repository root or local config.
from pathlib import Path
import sys
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH)
version = (root / "VERSION").read_text(encoding="utf-8").strip()
exe_name = f"HD2-DG-LAB-{version}"
datas = [(str(root / "hd2coyote" / "frontend"), "hd2coyote/frontend")]
for name in ("LICENSE", "THIRD_PARTY.md", "SAFETY.md", "VERSION"):
    datas.append((str(root / name), "."))
for name in ("DESKTOP.md", "UPGRADE.md", "RELEASE_ACCEPTANCE.md", "EAC.md"):
    datas.append((str(root / "docs" / name), "docs"))
# Wheel metadata includes license texts (including bundled native libraries).
for package in ("pywebview", "websockets", "qrcode", "numpy", "pillow", "mss", "pyinstaller"):
    datas.extend(copy_metadata(package, recursive=True))
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if not python_license.is_file():
    raise FileNotFoundError("Python runtime LICENSE.txt is required for distribution")
datas.append((str(python_license), "licenses/python"))

a = Analysis(
    [str(root / "desktop_entry.py")], pathex=[str(root)],
    binaries=[], datas=datas,
    hiddenimports=["webview.platforms.edgechromium", "qrcode.image.svg", "numpy", "PIL.ImageGrab", "mss"],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["dxcam", "pytest", "tkinter", "hd2coyote.ui", "hd2coyote.__main__"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name=exe_name, debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False,
    disable_windowed_traceback=False,
)
