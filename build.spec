# PyInstaller spec — builds "TM Desktop" as a windowed onedir app.
#
# Build with:  pyinstaller build.spec   (or run build.bat, which also zips the result)
#
# Notes:
# - We collect the full `playwright` package so its bundled Node driver ships with the
#   app. We do NOT bundle a browser: the app launches the operator's installed Chrome
#   (channel="chrome"), so nothing extra to download.
# - `webview` is collected so the native window backend and JS shim are included.
# - The `web/` UI folder is added as data and read via app/paths.py (sys._MEIPASS aware).

from PyInstaller.utils.hooks import collect_all

datas = [("web", "web")]
binaries = []
hiddenimports = ["zipcodes", "phonenumbers", "bs4", "yaml"]

# icloud_hme reaches for `srp` and `requests` at call time, and zipcodes/certifi ship data
# files PyInstaller won't find on its own. gspread/google.auth (the Google Sheet mirror)
# resolve their signing and transport backends dynamically.
for pkg in ("playwright", "webview", "bottle", "icloud_hme", "srp", "zipcodes", "certifi",
            "gspread", "google.auth", "google.oauth2"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TM Desktop",
    console=False,       # windowed app, no console
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="TM Desktop",   # -> dist/TM Desktop/
)
