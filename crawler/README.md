# 亚马逊商品爬虫（独立模块）

把一份 Excel 表里列 A 的亚马逊商品链接批量爬下来，每个商品输出一个 `{ASIN}.zip`（内含完整标题 + 高清图），并生成一份 `商品列表.xlsx` 直接喂给现有的电商生图程序。

**纯独立模块**：不修改也不依赖父项目（`src/` / `gui/`）的任何代码，只复用同一份虚拟环境。

## 输入

一份 `.xlsx` 文件，**列 A 放商品链接**（每行一条），第一行可以是表头（自动检测：第一格不像 URL 时当表头跳过）。

模板：[`input_template.xlsx`](input_template.xlsx)（运行 `python -m crawler.run --make-template` 也可重新生成）。

支持的链接形式：

- `https://www.amazon.de/dp/B0GK22CBCR`
- `https://www.amazon.de/dp/B0GK22CBCR/ref=...`
- `https://www.amazon.de/gp/product/B0GK22CBCR`
- 以上同样适用于 `amazon.com / amazon.co.uk / amazon.fr` 等其他站点（地区相关只影响标题语言）

## 输出

```
<out_dir>/
  B0GK22CBCR.zip         每个商品一个 zip
  B0XXXXXXXX.zip
  ...
  商品列表.xlsx           两列：ZIP/文件夹路径 + 商品标题（仅成功的行）
  crawl_log.txt           每抓一行立即追加一条记录（含失败原因）
```

每个 `{ASIN}.zip` 内部结构：

```
{ASIN}/
  商品标题.txt              UTF-8，完整商品标题
  refs/
    01_main.jpg             高清主图（landingImage）
    02_alt_01.jpg           gallery 第 1 张
    03_alt_02.jpg           gallery 第 2 张
    ...                     最多 --max-images 张
```

这个形状刚好就是现有 `gui/zip_util.py` 的 `extract_product_zip` 期待的格式，所以 `商品列表.xlsx` 可以**直接拖进**现有生图程序的「批量」Tab。

## 运行

确保父项目的 `.venv-build` 已经按 `requirements.txt` 装好（`requests / lxml / openpyxl` 都在），然后在项目根目录：

```powershell
.\.venv-build\Scripts\python.exe -m crawler.run --input crawler\input_template.xlsx --out crawler\output
```

完整 CLI 参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--input PATH` | 必填 | 输入 xlsx 路径，列 A=URL |
| `--out DIR` | `crawler/output` | 输出目录 |
| `--max-images N` | `8` | 每个商品最多保存几张图 |
| `--limit N` | 无 | 只跑前 N 行（调试用） |
| `--skip-existing` | 关 | 已存在的 `{ASIN}.zip` 跳过（断点续抓） |
| `--delay-min FLOAT` | `2.0` | 页面之间最小间隔秒 |
| `--delay-max FLOAT` | `5.0` | 页面之间最大间隔秒 |
| `--make-template` | — | 在 `--input` 指向的位置生成空白模板，然后退出 |

## 反爬应对

亚马逊会随机用 CAPTCHA 拦请求。本爬虫的策略：

- 全程一个 `requests.Session()`，cookie 自动累积
- 模拟 Chrome 124 浏览器请求头，`Accept-Language` 优先 `de-DE`
- 每页之间随机等 2-5 秒
- 遇到 CAPTCHA / 5xx / 网络错误时退避 2/5/10s，最多 3 次
- 还失败就标记 `status=captcha`/`fetch_error` 写到 `crawl_log.txt`，**继续**抓下一个商品（不死循环）

如果命中率不理想：

1. 加大间隔：`--delay-min 5 --delay-max 12`
2. 隔几小时再用 `--skip-existing` 续跑（已成功的不会重抓）
3. 走代理：设置环境变量 `HTTPS_PROXY=http://...` 后再跑（`requests` 自动识别）

## 法律提醒

亚马逊 ToS 禁止抓取页面内容。**仅供个人选品 / 调研使用**，不要拿来商用或对外提供数据服务。商用请走 Amazon Product Advertising API（PA-API）。
