# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for a one-file exe.

- Uses collect_data_files(..., include_py_files=True) so Gradio can read its .py sources
  from the _MEI* extract dir (see gradio component_meta / blocks_events).
- Do not exclude pandas or matplotlib: Gradio imports them at startup / in the event queue.
- Heavy stacks (torch, jupyter, …) stay in excludes to keep analysis smaller on conda envs.

Build:  python build_exe.py
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "prompts"), "prompts"),
    (str(ROOT / "config.yaml"), "."),
]
datas += copy_metadata("gradio")
datas += copy_metadata("gradio_client")
datas += collect_data_files("gradio", include_py_files=True)
datas += collect_data_files("gradio_client", include_py_files=True)

binaries = collect_dynamic_libs("gradio")

# Heavy optional stacks often present in scientific Python installs — not needed for this GUI.
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
]

hiddenimports = [
    "gradio",
    "gradio_client",
    "gradio_client.client",
    "uvicorn",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "fastapi",
    "starlette",
    "starlette.routing",
    "websockets",
    "httpx",
    "orjson",
    "anyio",
    "anyio._backends",
    "anyio._backends._asyncio",
    "aiofiles",
    "semantic_version",
    "ffmpy",
    "markdown_it",
    "mdit_py_plugins",
    "tomlkit",
    "typer",
    "rich",
    "loguru",
    "yaml",
    "PIL",
    "huggingface_hub",
    # Excel / CSV batch I/O — pandas 通过字符串引擎名惰性导入，PyInstaller 默认抓不到
    "openpyxl",
    "openpyxl.cell._writer",
    "xlrd",
    "xlrd.book",
    "lxml",
    "lxml.etree",
    "lxml.html",
    "html5lib",
    "html5lib.treebuilders",
    "html5lib.treebuilders.etree",
    "pandas.io.excel._openpyxl",
    "pandas.io.excel._xlrd",
    "pandas.io.html",
    # 浏览文件夹按钮使用，stdlib 但需要把 tcl/tk 运行时一起带上
    "tkinter",
    "tkinter.filedialog",
]

a = Analysis(
    [str(ROOT / "gui" / "app.py")],
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
    name="AmazonImgGUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
