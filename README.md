# SuperToken Skills

这是 SuperToken 官方 Agent Skills 仓库。`supertoken-gpt-image-2` 通过 SuperToken 的 GPT Image 2 接口生成、编辑和保存图片，支持同步请求与异步任务，可用于 Codex 和 Claude Code。

## 可用 Skills

| Skill | 说明 |
| --- | --- |
| `supertoken-gpt-image-2` | 默认使用 `gpt-image-2-count`，支持图片生成、编辑、任务查询和结果保存 |

## 环境要求

- Python 3.10 或更高版本
- 可以访问 `https://api.supertoken.cc`
- 用于模型调用的 SuperToken 模型 API Token
- 查询异步任务时需要 SuperToken 资源 API Key
- 使用安装和升级命令时，需要 Node.js 22.20.0 或更高版本

下文命令在 macOS 和 Linux 使用 `python3`；Windows 使用 `py -3`。

## 安装

安装到 Codex：

```bash
npx skills add supertoken2026/skills --skill supertoken-gpt-image-2 --agent codex --global
```

需要同时在 Codex 和 Claude Code 中使用时，请用一条命令安装到两端：

```bash
npx skills add supertoken2026/skills --skill supertoken-gpt-image-2 --agent codex claude-code --global
```

联合安装后，两端引用同一份 Skill，一次升级即可同步更新。

## 升级

```bash
# 更新单个 Skill
npx skills update supertoken-gpt-image-2

# 仅更新全局安装
npx skills update -g supertoken-gpt-image-2

# 仅更新当前项目中的安装
npx skills update -p supertoken-gpt-image-2
```

通过 `npx skills add` 安装的 Skill 可以使用上述命令升级。手动复制目录的安装方式不提供自动升级。

## 配置凭据

`SUPERTOKEN_API_KEY` 保存模型 API Token（`sk-...`），用于模型列表、同步生成、同步编辑和异步创建。`SUPERTOKEN_RESOURCE_API_KEY` 保存资源 API Key（`ak_...`），只用于异步任务查询和等待。两种 Key 不能混用。

推荐运行安全配置脚本。macOS 和 Linux：

```bash
python3 skills/supertoken-gpt-image-2/scripts/setup.py
```

Windows PowerShell：

```powershell
py -3 skills/supertoken-gpt-image-2/scripts/setup.py
```

脚本在 macOS 使用 Keychain，在 Windows 使用 DPAPI，在安装了 `secret-tool` 的 Linux 环境使用 Secret Service。找不到系统安全存储时，不会自动保存明文 Key。脚本会隐藏提示模型 Token；只做同步工作时无需资源 Key。需要查询任务时，按下文将资源 Key 隐藏输入到环境变量，不要把 Key 写进命令参数。

运行时先读取对应环境变量，再读取系统安全存储。只有系统安全存储不可用且明确传入 `--allow-plaintext-key-store` 时，才会把 Key 写入本地明文凭据文件；POSIX 系统会将该文件权限设为 `0600`。

如果只想在当前 Bash 或 zsh 中使用环境变量：

```bash
printf "SuperToken Model API Token: " >&2
IFS= read -r -s SUPERTOKEN_API_KEY
printf "\n" >&2
export SUPERTOKEN_API_KEY
printf "SuperToken Resource API Key: " >&2
IFS= read -r -s SUPERTOKEN_RESOURCE_API_KEY
printf "\n" >&2
export SUPERTOKEN_RESOURCE_API_KEY
```

本版不读取 Webhook Key，也不运行 Webhook 接收服务。

## 模型选择

默认模型是 `gpt-image-2-count`。需要 `n > 1` 或依赖官方 Images API 的完整参数时，使用 `gpt-image-2`。`adobe-gpt-image-2-count` 只有在用户明确指定，或模型查询确认账号可用时才使用。

查询可用模型：

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py models
```

默认只显示 ID 中包含 `gpt-image-2` 的模型；传 `--all` 可查看完整列表。

## 同步生成

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py generate \
  --prompt "一只坐在阳光里的小猫" \
  --output ./supertoken-kitten.png
```

生成四张图片时使用 `gpt-image-2`：

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py generate \
  --prompt "白色背景上的四款智能手表产品图" \
  --model gpt-image-2 \
  --n 4 \
  --output ./watch.png
```

旧版入口仍然可用，并会转到新版同步端点：

```bash
python3 skills/supertoken-gpt-image-2/scripts/generate_image.py \
  --prompt "一只坐在阳光里的小猫" \
  --output ./supertoken-kitten.png
```

## 同步编辑

使用本地参考图和本地 Mask：

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py edit \
  --prompt "将 Mask 区域改成日落海边" \
  --image ./source.png \
  --mask ./mask.png \
  --output ./edited.png
```

使用 URL 参考图：

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py edit \
  --prompt "改成黑白铅笔素描" \
  --image https://example.com/source.png \
  --output ./sketch.png
```

一次 `edit` 只能使用本地文件、URL 或 Base64 中的一种，不能混用。本地 Mask 只能配合本地图片；异步 URL 编辑可使用 URL Mask。Base64 只支持同步编辑，建议通过 `--image-base64-file ./image.txt` 读取，避免把长内容写进命令历史。

## 异步任务

只创建任务时使用 `--async`，不要传 `--output`。只有模型 Token 也能创建任务，但配置 `SUPERTOKEN_RESOURCE_API_KEY` 之前无法查询或等待；提交前应确认用户接受这一限制。显式幂等键可用于提交结果不确定时的确认或手动重试：

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py generate \
  --prompt "夜间城市天际线" \
  --async \
  --idempotency-key skyline-20260727
```

创建任务、等待完成并保存全部结果：

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py generate \
  --prompt "夜间城市天际线" \
  --async \
  --wait \
  --output ./skyline.png
```

`--async --wait` 同时需要 `SUPERTOKEN_API_KEY` 和 `SUPERTOKEN_RESOURCE_API_KEY`。异步 Base64 编辑不受支持；请改用同步编辑，或在本 Skill 之外预上传后传 URL。

查询一次已有任务，不下载结果：

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py task task_example123
```

恢复等待已有任务并保存结果：

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py wait task_example123 \
  --output ./skyline.png
```

任务状态为 `queued` 或 `in_progress` 时继续等待，`succeeded` 时保存 `result.images[]` 中的全部图片，`failed` 时停止并输出脱敏错误。默认最长等待 900 秒。

## 结果、重试与旧版基址

成功 JSON 会包含模型、存在时的任务 ID，以及 `outputs[]` 中每张图片的绝对路径、字节数和格式。多图结果按 `name-1.ext`、`name-2.ext` 命名。文件先写入 `.part`，校验完成后再替换目标文件。

同步生成、同步编辑和异步创建的 POST 不自动重试。手动重试同一个异步请求时，复用原来的 `Idempotency-Key`；不同请求使用新键。任务查询 GET 只对连接错误、`429`、`502` 和 `503` 做有限重试。

默认基址是 `https://api.supertoken.cc`。如需明确调用旧版同步接口，可在 `generate` 或 `edit` 中单次传入：

```bash
--base-url https://api.supertoken.cc/image-wrapper/v1
```

旧版 `image-wrapper/v1` 基址只支持同步生成和编辑，不支持 `models` 或异步命令。端点、字段与上传限制见 [GPT Image 2 API 参考](skills/supertoken-gpt-image-2/references/gpt-image-2-api.md)。

## 支持

- 文档：<https://docs.supertoken.cc/>
- 官网：<https://supertoken.cc/>
- 官方 QQ 群：`1091860777`
- QQ 客服：`376064105`
- 微信客服：`piplszy`
- 微信客服：`minus502`

## English

This is the official SuperToken Agent Skills repository. `supertoken-gpt-image-2` generates, edits, and saves images through the SuperToken GPT Image 2 API. It supports synchronous requests and asynchronous tasks in Codex and Claude Code.

### Requirements

- Python 3.10 or newer
- Network access to `https://api.supertoken.cc`
- A SuperToken model API Token for model calls
- A SuperToken resource API Key for async task queries
- Node.js 22.20.0 or newer for installation and updates through `npx skills`

The commands below use `python3` on macOS and Linux. Use `py -3` on Windows.

### Install

Install for Codex:

```bash
npx skills add supertoken2026/skills --skill supertoken-gpt-image-2 --agent codex --global
```

Install for both Codex and Claude Code in one command:

```bash
npx skills add supertoken2026/skills --skill supertoken-gpt-image-2 --agent codex claude-code --global
```

Both agents then use the same Skill copy, so one update refreshes both.

### Update

```bash
# Update one Skill
npx skills update supertoken-gpt-image-2

# Update only the global installation
npx skills update -g supertoken-gpt-image-2

# Update only the project installation
npx skills update -p supertoken-gpt-image-2
```

These update commands require an installation created by `npx skills add`. A manually copied directory cannot be updated automatically.

### Configure credentials

`SUPERTOKEN_API_KEY` holds the model API Token (`sk-...`) used for model listing, sync generation, sync editing, and async creation. `SUPERTOKEN_RESOURCE_API_KEY` holds the resource API Key (`ak_...`) used only for async task queries and waits. Do not swap the two.

Run the secure setup script on macOS and Linux:

```bash
python3 skills/supertoken-gpt-image-2/scripts/setup.py
```

On Windows PowerShell:

```powershell
py -3 skills/supertoken-gpt-image-2/scripts/setup.py
```

The script uses Keychain on macOS, DPAPI on Windows, and Secret Service on Linux when `secret-tool` is installed. It never falls back silently to plaintext. The script prompts securely for the model Token. Sync-only use does not need a resource Key; for task queries, enter it into the environment with the hidden-input example below instead of placing it in a command argument.

At runtime, each environment variable takes precedence over its operating-system credential-store entry. Plaintext storage is used only when the secure store is unavailable and `--allow-plaintext-key-store` is explicit. On POSIX systems, that local file uses mode `0600`.

For environment variables limited to the current Bash or zsh session:

```bash
printf "SuperToken Model API Token: " >&2
IFS= read -r -s SUPERTOKEN_API_KEY
printf "\n" >&2
export SUPERTOKEN_API_KEY
printf "SuperToken Resource API Key: " >&2
IFS= read -r -s SUPERTOKEN_RESOURCE_API_KEY
printf "\n" >&2
export SUPERTOKEN_RESOURCE_API_KEY
```

This version does not read a Webhook Key or run a Webhook receiver.

### Choose a model

The default model is `gpt-image-2-count`. Use `gpt-image-2` when `n > 1` or a request depends on the full official Images API parameters. Select `adobe-gpt-image-2-count` only when the user asks for it or a model query confirms account access.

List available models:

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py models
```

The command returns only IDs containing `gpt-image-2` by default. Pass `--all` for the complete list.

### Generate synchronously

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py generate \
  --prompt "A tiny kitten sitting in sunlight" \
  --output ./supertoken-kitten.png
```

Use `gpt-image-2` for four images:

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py generate \
  --prompt "Four smartwatch product images on a white background" \
  --model gpt-image-2 \
  --n 4 \
  --output ./watch.png
```

The legacy entry point remains available and delegates to the modern sync endpoint:

```bash
python3 skills/supertoken-gpt-image-2/scripts/generate_image.py \
  --prompt "A tiny kitten sitting in sunlight" \
  --output ./supertoken-kitten.png
```

### Edit synchronously

Use a local reference and local Mask:

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py edit \
  --prompt "Replace the Mask area with a beach at sunset" \
  --image ./source.png \
  --mask ./mask.png \
  --output ./edited.png
```

Use a URL reference:

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py edit \
  --prompt "Convert this to a black-and-white pencil sketch" \
  --image https://example.com/source.png \
  --output ./sketch.png
```

One `edit` invocation must use exactly one input family: local files, URLs, or Base64. Do not mix them. A local Mask requires local images; an async URL edit may use a URL Mask. Base64 is synchronous-only. Prefer `--image-base64-file ./image.txt` so long data does not enter shell history.

### Run asynchronous tasks

Use `--async` without `--output` to create only a task. A model Token alone can create it, but it cannot be queried or waited on until `SUPERTOKEN_RESOURCE_API_KEY` is configured; confirm that limitation before submission. An explicit idempotency key lets you confirm or manually retry an uncertain submission:

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py generate \
  --prompt "A city skyline at night" \
  --async \
  --idempotency-key skyline-20260727
```

Create, wait, and save every result:

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py generate \
  --prompt "A city skyline at night" \
  --async \
  --wait \
  --output ./skyline.png
```

`--async --wait` needs both `SUPERTOKEN_API_KEY` and `SUPERTOKEN_RESOURCE_API_KEY`. Async Base64 editing is unsupported. Use a sync edit or upload outside this Skill and pass a URL.

Query an existing task once without downloading:

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py task task_example123
```

Resume waiting for an existing task and save its results:

```bash
python3 skills/supertoken-gpt-image-2/scripts/supertoken_image.py wait task_example123 \
  --output ./skyline.png
```

Polling continues for `queued` and `in_progress`, saves every `result.images[]` item on `succeeded`, and stops with a redacted error on `failed`. The default wait limit is 900 seconds.

### Results, retries, and the legacy base

Successful JSON includes the model, the task ID when present, and each image's absolute path, byte count, and format in `outputs[]`. Multiple results become `name-1.ext`, `name-2.ext`, and so on. Files are written through `.part` and promoted only after validation.

Sync generation, sync editing, and async creation POST requests are never retried automatically. Reuse the original `Idempotency-Key` when manually retrying the same async request. Use a new key for a different request. Task-query GET retries are bounded to connection errors, `429`, `502`, and `503`.

The default base is `https://api.supertoken.cc`. To call the legacy sync API explicitly, add this one-command override to `generate` or `edit`:

```bash
--base-url https://api.supertoken.cc/image-wrapper/v1
```

The legacy `image-wrapper/v1` base supports only sync generation and editing, not `models` or async commands. See the [GPT Image 2 API reference](skills/supertoken-gpt-image-2/references/gpt-image-2-api.md) for endpoints, field mappings, and upload limits.

### Support

- Documentation: <https://docs.supertoken.cc/>
- Website: <https://supertoken.cc/>
- Official QQ group: `1091860777`
- QQ support: `376064105`
- WeChat support: `piplszy`
- WeChat support: `minus502`
