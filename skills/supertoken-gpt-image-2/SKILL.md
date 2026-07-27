---
name: supertoken-gpt-image-2
description: Use when generating, editing, saving, querying, or waiting for GPT-Image-2 images and asynchronous tasks through SuperToken, including model access and credentials.
---

# SuperToken GPT Image 2

通过 SuperToken 生成或编辑图片，并保存接口返回的每一张图片。默认使用新版同步接口；用户明确需要后台任务、任务 ID 或较长处理时间时，再使用异步任务。

macOS 和 Linux 使用 `python3`，Windows 使用 `py -3`。先定位此 `SKILL.md` 所在目录，再从该目录运行示例命令。需要端点、字段或限制的准确映射时，读取 [API 参考](references/gpt-image-2-api.md)。

## 选择调用方式

| 需求 | 方式 | 命令 | 凭据 |
| --- | --- | --- | --- |
| 直接生成并保存图片 | 同步生成 | `generate` | `SUPERTOKEN_API_KEY` |
| 用参考图或 Mask 编辑并直接保存 | 同步编辑 | `edit` | `SUPERTOKEN_API_KEY` |
| 后台提交并立即取得任务 ID | 异步创建 | `generate|edit --async` | `SUPERTOKEN_API_KEY` |
| 查询任务或等待并保存结果 | 异步等待 | `task`、`wait` 或 `--async --wait` | 查询需要 `SUPERTOKEN_RESOURCE_API_KEY`；创建并等待需要两种 Key |

## 凭据与模型

- `SUPERTOKEN_API_KEY` 保存模型 API Token（`sk-...`），用于 `models`、同步请求和异步创建。
- `SUPERTOKEN_RESOURCE_API_KEY` 保存资源 API Key（`ak_...`），只用于 `task`、`wait` 和异步轮询。两种 Key 不能混用。
- 本版不读取 Webhook Key，也不运行 Webhook 接收服务。
- 默认模型是 `gpt-image-2-count`。当 `n > 1` 或请求依赖官方 Images API 的完整参数时，使用 `gpt-image-2`。
- 只有用户明确指定，或 `models` 已确认账号可用时，才使用 `adobe-gpt-image-2-count`。不要假定账号拥有该模型。

安全配置模型 Token：

```bash
python3 scripts/setup.py
```

资源 Key 从 `SUPERTOKEN_RESOURCE_API_KEY` 读取。设置时使用 README 中兼容 Bash 和 zsh 的隐藏输入方式，不要把 Key 写进命令参数。

不要把真实 Key 写进文档、提交、Issue 或命令示例。

## 常用命令

查询当前账号可用的 GPT Image 2 模型：

```bash
python3 scripts/supertoken_image.py models
```

同步生成一张图片：

```bash
python3 scripts/supertoken_image.py generate \
  --prompt "一只坐在阳光里的小猫" \
  --output ./supertoken-kitten.png
```

生成多张图片时改用 `gpt-image-2`：

```bash
python3 scripts/supertoken_image.py generate \
  --prompt "白色背景上的四款智能手表产品图" \
  --model gpt-image-2 \
  --n 4 \
  --output ./watch.png
```

使用本地图片和本地 Mask 同步编辑：

```bash
python3 scripts/supertoken_image.py edit \
  --prompt "将 Mask 区域改成日落海边" \
  --image ./source.png \
  --mask ./mask.png \
  --output ./edited.png
```

使用 URL 参考图同步编辑：

```bash
python3 scripts/supertoken_image.py edit \
  --prompt "改成黑白铅笔素描" \
  --image https://example.com/source.png \
  --output ./sketch.png
```

一次 `edit` 只能使用本地文件、URL 或 Base64 中的一种，不能混用。本地 Mask 只能配合本地图片；异步 URL 编辑可使用 URL Mask。Base64 只支持同步编辑，可通过 `--image-base64-file` 读取，避免把长内容写进命令历史。同步编辑会发送 `--n`；`gpt-image-2-count` 只允许 `--n 1`。

只创建异步任务时不要传 `--output`。如果只有模型 Token、尚未配置资源 Key，提交前必须告诉用户：任务可以创建，但配置 `SUPERTOKEN_RESOURCE_API_KEY` 之前无法查询或等待。显式提供幂等键，便于提交结果不确定时安全确认或重试：

```bash
python3 scripts/supertoken_image.py generate \
  --prompt "夜间城市天际线" \
  --async \
  --idempotency-key skyline-20260727
```

创建任务、等待完成并保存结果：

```bash
python3 scripts/supertoken_image.py generate \
  --prompt "夜间城市天际线" \
  --async \
  --wait \
  --output ./skyline.png
```

查询一次已有任务：

```bash
python3 scripts/supertoken_image.py task task_example123
```

恢复等待并保存已有任务的结果：

```bash
python3 scripts/supertoken_image.py wait task_example123 \
  --output ./skyline.png
```

## 结果与失败处理

- 同步请求必须返回与 `--n` 相同数量的图片；恢复已有异步任务时接受 1 到 10 张。单次保存的解码结果总量不能超过 256 MiB。
- 多图结果按 `name-1.ext`、`name-2.ext` 依次命名；扩展名根据内容确定为 `.png`、`.jpeg` 或 `.webp`。文件通过同目录唯一临时文件原子保存，中途失败时清理本次输出。
- 同步生成、同步编辑和异步创建的 POST 不自动重试。手动重试同一个异步请求时，复用原来的 `Idempotency-Key`；不同请求使用新键。
- `task` 只输出任务 ID、状态、可选进度和脱敏失败摘要。`wait` 会轮询 `queued` 和 `in_progress`，在 `succeeded` 时保存图片，在 `failed` 时停止；任务查询、轮询休眠和结果下载共用一个截止时间。
- 自定义 API 基址必须是干净的绝对 HTTPS 地址。认证请求不跟随重定向，诊断输出会删除 URL 中的用户信息、查询参数和片段。
- 异步 Base64 编辑和 Webhook 接收不在本版范围内。

## English

Generate or edit images through SuperToken and save every image returned by the API. Use the modern synchronous endpoints by default. Choose an asynchronous task only when the user asks for background execution, a task ID, or longer processing.

Use `python3` on macOS and Linux or `py -3` on Windows. Resolve the directory containing this `SKILL.md` and run the examples from that directory. Read the [API reference](references/gpt-image-2-api.md) when exact endpoint, field, or limit mappings are needed.

### Choose a mode

| Need | Mode | Command | Credential |
| --- | --- | --- | --- |
| Generate and save images directly | Sync generation | `generate` | `SUPERTOKEN_API_KEY` |
| Edit references or a Mask and save directly | Sync edit | `edit` | `SUPERTOKEN_API_KEY` |
| Submit in the background and return a task ID | Async create | `generate|edit --async` | `SUPERTOKEN_API_KEY` |
| Query a task or wait and save its results | Async wait | `task`, `wait`, or `--async --wait` | Queries need `SUPERTOKEN_RESOURCE_API_KEY`; create-and-wait needs both keys |

### Credentials and models

- `SUPERTOKEN_API_KEY` holds the model API Token (`sk-...`) for `models`, synchronous requests, and asynchronous creation.
- `SUPERTOKEN_RESOURCE_API_KEY` holds the resource API Key (`ak_...`) only for `task`, `wait`, and polling. Do not swap the two keys.
- This version does not read a Webhook Key or run a Webhook receiver.
- The default model is `gpt-image-2-count`. Use `gpt-image-2` when `n > 1` or the request depends on the full official Images API parameters.
- Use `adobe-gpt-image-2-count` only when the user explicitly selects it or `models` confirms access. Do not assume entitlement.

Configure the model Token securely:

```bash
python3 scripts/setup.py
```

The resource Key is read from `SUPERTOKEN_RESOURCE_API_KEY`. Set it with the hidden-input Bash/zsh flow in the README, never as a command argument.

Never put a real key in documentation, commits, issues, or command examples.

### Commands

List the account's available GPT Image 2 models:

```bash
python3 scripts/supertoken_image.py models
```

Generate one image synchronously:

```bash
python3 scripts/supertoken_image.py generate \
  --prompt "A tiny kitten sitting in sunlight" \
  --output ./supertoken-kitten.png
```

Use `gpt-image-2` for multiple images:

```bash
python3 scripts/supertoken_image.py generate \
  --prompt "Four smartwatch product images on a white background" \
  --model gpt-image-2 \
  --n 4 \
  --output ./watch.png
```

Edit synchronously with a local image and local Mask:

```bash
python3 scripts/supertoken_image.py edit \
  --prompt "Replace the Mask area with a beach at sunset" \
  --image ./source.png \
  --mask ./mask.png \
  --output ./edited.png
```

Edit synchronously from a URL reference:

```bash
python3 scripts/supertoken_image.py edit \
  --prompt "Convert this to a black-and-white pencil sketch" \
  --image https://example.com/source.png \
  --output ./sketch.png
```

One `edit` invocation must use exactly one input family: local files, URLs, or Base64. Do not mix them. A local Mask requires local images; an asynchronous URL edit may use a URL Mask. Base64 is synchronous-only and can be read with `--image-base64-file` to keep long data out of shell history. Sync edits send `--n`; `gpt-image-2-count` accepts only `--n 1`.

For create-only asynchronous work, omit `--output`. If only the model Token is available, warn the user before submission that the task can be created but cannot be queried or waited on until `SUPERTOKEN_RESOURCE_API_KEY` is configured. Supply an explicit idempotency key when the same request may need to be confirmed or retried:

```bash
python3 scripts/supertoken_image.py generate \
  --prompt "A city skyline at night" \
  --async \
  --idempotency-key skyline-20260727
```

Create, wait, and save in one invocation:

```bash
python3 scripts/supertoken_image.py generate \
  --prompt "A city skyline at night" \
  --async \
  --wait \
  --output ./skyline.png
```

Query an existing task once:

```bash
python3 scripts/supertoken_image.py task task_example123
```

Resume waiting for an existing task and save its results:

```bash
python3 scripts/supertoken_image.py wait task_example123 \
  --output ./skyline.png
```

### Results and failures

- A synchronous response must contain exactly the requested `--n`; a resumed asynchronous task may contain 1-10 images. Aggregate decoded output is limited to 256 MiB per save.
- Multiple results are named `name-1.ext`, `name-2.ext`, and so on; content determines whether the suffix is `.png`, `.jpeg`, or `.webp`. Unique temporary files in the destination directory are promoted atomically, and a later failure removes outputs from that save.
- Generation, edit, and task-creation POST requests are never retried automatically. A manual retry of the same asynchronous request must reuse its original `Idempotency-Key`; a different request needs a new key.
- `task` prints only the task ID, status, optional progress, and a redacted failure summary. `wait` polls `queued` and `in_progress`, saves images on `succeeded`, and stops on `failed`; task queries, polling sleeps, and result downloads share one deadline.
- A custom API base must be a clean absolute HTTPS URL. Authenticated requests do not follow redirects, and diagnostic URLs omit userinfo, query, and fragment.
- Asynchronous Base64 editing and Webhook receiving are outside this version.
