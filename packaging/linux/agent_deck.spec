# Portable x86-64 Linux package with console and Tk companion entry points.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent.parent

a = Analysis(
    [str(root / "packaging" / "entrypoint.py")],
    pathex=[str(root / "bridge")],
    binaries=[],
    datas=[],
    hiddenimports=collect_submodules("bleak"),
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
