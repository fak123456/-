# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the standalone Amazon crawler CLI.

Produces a single-file exe with a console window so users can see progress
and error messages while it runs. Entry point is ``crawler/run.py``.

Build:  python build_crawler_exe.py
"""
from pathlib import Path

ROOT = Path(SPECPATH)

datas = []

binaries = []

excludes = [
    "torch",
    "torchvision",
    "torchaudio",
    "pytorch_lightning",
    "tensorflow",
    "tensorboard",
    "tensorflow_estimator",
    "jax",
    "jaxlib",
    "IPython",
    "jupyter",
    "jupyterlab",
    "notebook",
    "ipykernel",
    "ipywidgets",
    "qtconsole",
    "pytest",
    "scipy",
    "sklearn",
    "skimage",
    "bokeh",
    "plotly",
    "holoviews",
    "datashader",
    "colorcet",
    "panel",
    "pyviz_comms",
    "xarray",
    "netCDF4",
    "tables",
    "h5py",
    "statsmodels",
    "patsy",
    "numba",
    "sqlalchemy",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "qtpy",
    "django",
    "flask",
    "cv2",
    "botocore",
    "boto3",
    "zmq",
    "distributed",
    "dask",
    "sphinx",
    "nbconvert",
    "intake",
    "imageio_ffmpeg",
    "notebook_shim",
    "jupyter_server",
    # 主程序专用，爬虫用不到
    "gradio",
    "gradio_client",
    "uvicorn",
    "fastapi",
    "starlette",
    "websockets",
    "huggingface_hub",
    "matplotlib",
    "pandas",
    "numpy",
    "PIL",
]

hiddenimports = [
    "openpyxl",
    "openpyxl.cell._writer",
    "lxml",
    "lxml.etree",
    "lxml.html",
    "requests",
    "urllib3",
    "charset_normalizer",
    "idna",
    "certifi",
]

a = Analysis(
    [str(ROOT / "crawler" / "run.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="amazon_crawler",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
