# 电商生图批量工具（Amazon 泛欧）

按 **类型 × 数量** 为每个商品文件夹批量生成主图 / 场景图 / 多格图 / 尺寸图 / 细节图等，支持 **Nano Banana（Gemini 2.5 Flash Image）** 测试与 **Gemini 3.1 Flash Image Preview** 生产，输出 PNG 与 `meta.json`。

## 目录约定

每个商品一个文件夹，需包含：

- `商品标题.txt` — 标题原文（任意欧洲语言 UTF-8，不翻译，直接写入英文 prompt）
- 参考图：优先放在 `refs/`；若在根目录，首次运行会自动移入 `refs/`
- 生成结果：`output/main_01.png`、`scene_01.png` … 与 `output/meta.json`

可选覆盖：

- `商品X/counts.yaml` — 仅覆盖本商品的 `image_counts`
- `商品X/briefs.yaml` — 自定义每条图的 **brief**（画面侧重一句话），或启用 `brief_source: llm`（占位/未来接 LLM）

## 安装

```bash
pip install -r requirements.txt
```

复制 `.env.example` 为 `.env`，按需填写 API Key。

## 配置

### 项目根 `config.yaml`

- `image_counts`：各类型张数（**只写需要覆盖的键**即可，其余用内置默认 main=1+scene=2+multi=1+size=1+detail=1+angle=1+material=1，共 8 张）
- `generation.size`：推荐 `native`（不缩放，保留模型原生像素；NB1 约 1024²，NB3 图像模型可配 `2K` 等）
- `generation.max_refs_per_call`：每次请求最多附带几张参考图
- `generation.gemini_native_image_size`：在 `size: native` 且使用 **Gemini 3.x 图像模型** 时传给 API 的档位（如 `2K`）

**总数** = 各类型数量之和，须在 **1～20** 之间，否则程序报错退出。

### 合并优先级

**`--counts` CLI** > **`商品X/counts.yaml`** > **根目录 `config.yaml`** > **内置默认**

### `.env` 主要变量

| 变量 | 说明 |
|------|------|
| `IMAGE_PROVIDER` | `placeholder` / `gemini` / `openai` / `doubao` |
| `IMAGE_API_KEY` | Gemini 填 Google AI Studio 的 key（[获取地址](https://aistudio.google.com/apikey)） |
| `IMAGE_OUTPUT_SIZE` | 若设置则覆盖 `config.yaml` 的 `generation.size`（如 `native`、`2048x2048`） |
| `GEMINI_MODEL_ID` | 测试：`gemini-2.5-flash-image`；生产可改 `gemini-3.1-flash-image-preview` |
| `BRIEF_LLM_PROVIDER` | `placeholder`（默认，复制 `prompts/briefs.yaml`）或 `openai` / `gemini`（stub，需自行实现 API） |

## 运行示例

```bash
# 占位图跑通流程（不扣费）
python -m src.main --all --provider placeholder

# 单个商品
python -m src.main --product 商品1 --provider placeholder

# 仅命令行改张数（示例：2 张主图、0 张场景）
python -m src.main --product 商品1 --counts main=2,scene=0

# 指定另一份全局配置
python -m src.main --all --config path/to/alternate_config.yaml

# 只打印计划、不写文件、不调 API
python -m src.main --product 商品1 --dry-run
```

## 本地 Web 界面（GUI，给非技术用户）

1. `pip install -r requirements.txt`
2. 双击项目根目录下的 `启动.bat`，或在终端执行：`python -m gui.app`
3. 浏览器打开 `http://127.0.0.1:7860`，在 **「设置」** 中填写 API Key 并保存；在 **「生成」** 中选商品并生成。
4. 支持：批量路径、ZIP 解压、仅重跑指定图、一次性备注、历史记录、编辑 `prompts/` 模板。
5. **打包为 exe**（推荐干净 venv）：

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\pip install -r requirements.txt
.\.venv-build\Scripts\python build_exe.py
```

   产物 `dist/AmazonImgGUI.exe`（实测约 70 MB）会自动复制到 [`installer/启动.exe`](installer/启动.exe)。分发时附带 [`installer/README_GUI.md`](installer/README_GUI.md)、[`installer/使用说明.html`](installer/使用说明.html)、[`installer/README.txt`](installer/README.txt)、[`installer/测试报告.md`](installer/测试报告.md)。

   注意：`requirements.txt` 默认不装 `google-genai`（与 `gradio-client` 在 `websockets` 上互斥）。仅当用户选 `IMAGE_PROVIDER=gemini` 时再 `pip install --no-deps google-genai`。

## Gemini（Nano Banana）真图

1. `.env`：`IMAGE_PROVIDER=gemini`，`IMAGE_API_KEY=<你的 key>`，`GEMINI_MODEL_ID=gemini-2.5-flash-image`
2. 先少张数试跑，避免扣费过多：

```bash
python -m src.main --product 商品1 --provider gemini --counts main=1,scene=0,multi=0,size=0,detail=0
```

`openai` / `doubao` 的 **图像** 接口仍为 stub，需自行在对应 `*_provider.py` 中实现。

## Xais 中转站（xais.dchai.cn / sg2.dchai.cn）

走 OpenAI 兼容协议（`/v1/chat/completions`），支持多张 base64 参考图，单次同步返回 markdown 图片链接。

`.env` 示例：

```
IMAGE_PROVIDER=xais
IMAGE_API_KEY=<你的 XTOKEN>
XAIS_API_BASE=https://sg2.dchai.cn
XAIS_MODEL_ID=Nano_Banana_Pro_2K_0
XAIS_TIMEOUT=300
```

可选 `XAIS_API_BASE`：`https://sg2.dchai.cn`（主）/ `https://xais.dchai.cn` / `https://sg2c.dchai.cn` / `https://sg2g.dchai.cn`（备用，按速度选）。

可选 `XAIS_MODEL_ID`：

| 模型 ID | 分辨率 | 价格（积分/张） | 备注 |
|---|---|---|---|
| `Nano_Banana_Pro_2K_5` | 2048×2048 | 0.10 | 最便宜，但偶尔上游不稳 |
| `Nano_Banana_Pro_2K_0` | 2048×2048 | 0.15 | T0 速度，**推荐默认**（更稳） |
| `Nano_Banana_Pro_4K_0` | 4096×4096 | 0.18 | T0 速度，4K |
| `Nano_Banana_2_2K_0` | 2048×2048 | 0.15 | NB2 |
| `Nano_Banana_2_4K_0` | 4096×4096 | 0.18 | NB2 4K |

试跑 1 张主图（约 0.10 积分）：

```bash
python -m src.main --product 商品1 --provider xais --counts main=1,scene=0,multi=0,size=0,detail=0
```

跑通后跑全套 9 张：

```bash
python -m src.main --product 商品1 --provider xais
```

注意：

- 模型默认输出 **2K = 2048×2048**，比官方 Nano Banana 1 的 1024 更适合亚马逊（直接触发 Zoom）
- 长宽比通过把 `Aspect ratio: 1:1\n长宽比:1:1` 自动追加到 prompt 末尾控制；`config.yaml` 改 `generation.size` 为 `16:9 / 9:16 / 1600x900` 等比例时会自动换算
- 参考图作为 base64 data URI 一起发送，单次最多 6 张（由 `config.yaml` 的 `generation.max_refs_per_call` 控制）

## Brief 系统（避免同类型多张雷同）

全局默认在 [`prompts/briefs.yaml`](prompts/briefs.yaml)。每张图会把对应 **brief** 注入该类型的 `prompts/type_*.md` 中的 `{user_brief}`。

- **`brief_source: preset`**：在 `商品X/briefs.yaml` 里写 `briefs.scene` 等列表
- **`brief_source: llm`**：首次运行会调用 `BriefGenerator`（默认 placeholder = 复制全局 brief），并把结果写回 `briefs.yaml` 作为缓存；再次运行走 `llm_cached`

## 类型与模板文件

| 类型 | 模板 |
|------|------|
| main | `prompts/type_main.md` |
| scene | `prompts/type_scene.md` |
| multi | `prompts/type_multi.md` |
| size | `prompts/type_size.md` |
| detail | `prompts/type_detail.md` |
| angle | `prompts/type_angle.md` |
| material | `prompts/type_material.md` |

占位符：`{product_title}` `{type_index}` `{type_total}` `{type_name}` `{user_brief}`

## Amazon 图片规格（摘要）

- 长边至少 **1000 px** 可上架；**≥1600 px** 启用缩放查看（Zoom）
- 使用 `native` 时：NB1 输出约 1024²（满足 1000，但 Zoom 较弱）；NB2 配 `2K` 约 2048²（更利于 Zoom）

## 许可与风险

参考图与生成内容须符合平台及版权政策；API 调用产生费用，请自行控制批量与重试。
