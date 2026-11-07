from pathlib import Path
root = Path(SPECPATH).parent
a = Analysis([str(root / 'better/desktop/launcher.py')], pathex=[str(root)], binaries=[],
    datas=[(str(root / 'better/web'), 'better/web'), (str(root / 'config/888'), 'config/888'),
           (str(root / 'better/desktop/maintenance.ps1'), 'better/desktop')],
    hiddenimports=['uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
      'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets.auto',
      'uvicorn.lifespan.on', 'sqlalchemy.dialects.sqlite', 'webview.platforms.winforms',
      'webview.platforms.edgechromium'], excludes=['pytest', 'PyQt5', 'PyQt6', 'PySide6', 'cefpython3'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Better', console=False, upx=False,
    icon=str(root / 'better/web/brand/bettor.ico'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Better')
