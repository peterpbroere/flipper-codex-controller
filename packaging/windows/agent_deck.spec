# One shared directory with console and windowed entry points.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent.parent
hiddenimports = collect_submodules("bleak") + collect_submodules("winrt")
datas = [(str(root / "scripts" / "Focus-Agent.ps1"), "scripts")]

a = Analysis(
    [str(root / "packaging" / "windows" / "entrypoint.py")],
    pathex=[str(root / "bridge")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

console = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="AgentDeck",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
companion = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="AgentDeckCompanion",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
COLLECT(
    console, companion, a.binaries, a.datas, strip=False, upx=False,
    upx_exclude=[], name="AgentDeck",
)
