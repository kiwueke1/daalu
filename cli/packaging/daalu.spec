# PyInstaller spec — builds the single-file `daalu` binary.
#
#   pip install ./cli pyinstaller
#   pyinstaller cli/packaging/daalu.spec
#
# Produces dist/daalu (or dist/daalu.exe on Windows). The CLI's only third-party
# dependency is httpx, so the binary stays small.

block_cipher = None

a = Analysis(
    ["../daalu_cli/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=["daalu_cli", "daalu_cli.auth", "daalu_cli.agent", "daalu_cli.client", "daalu_cli.config"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "setuptools"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="daalu",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
