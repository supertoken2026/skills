# GPT Image 2 API 参考

本文记录 `scripts/supertoken_image.py` 使用的端点、字段和客户端限制。命令选择与常用示例见上一级 [SKILL.md](../SKILL.md)。

## 目录

- 中文
  - API 基址与鉴权
  - 端点
  - 同步字段
  - 异步字段
  - 编辑输入与上传限制
  - 任务、重试与结果
  - 状态码与退出码
- English
  - API bases and authentication
  - Endpoints
  - Synchronous fields
  - Asynchronous fields
  - Edit inputs and upload limits
  - Tasks, retries, and results
  - Status and exit codes

## API 基址与鉴权

默认基址是 `https://api.supertoken.cc`。CLI 按以下规则拼接路径：

| 基址形式 | 规则 |
| --- | --- |
| 根地址，例如 `https://api.supertoken.cc` | 直接追加 `/v1/...` |
| 现代前缀，例如 `https://api.supertoken.cc/v1` | 去掉相对路径中重复的 `/v1` 后追加 |
| 旧版前缀 `https://api.supertoken.cc/image-wrapper/v1` | 只允许同步生成和同步编辑；直接追加 `/images/...` |

旧版前缀不是默认值。只有调用兼容服务或明确测试旧版同步接口时，才通过 `--base-url https://api.supertoken.cc/image-wrapper/v1` 单次覆盖。`models`、异步创建、`task` 和 `wait` 会拒绝旧版前缀。

自定义基址必须是可规范化的绝对 HTTPS 地址。CLI 会去除首尾 ASCII 空白、末尾斜线和默认端口 `:443`，并统一 scheme 与 host 的大小写；用户信息、查询参数、片段、控制字符、路径空白、重复分隔符、点段及编码后的斜线都不允许。认证请求不跟随 3xx，也不会把 `Authorization` 或 `Idempotency-Key` 转发到重定向目标。

| 环境变量 | Key 类型 | 用途 |
| --- | --- | --- |
| `SUPERTOKEN_API_KEY` | 模型 API Token（`sk-...`） | 模型列表、同步生成、同步编辑、异步创建 |
| `SUPERTOKEN_RESOURCE_API_KEY` | 资源 API Key（`ak_...`） | 异步任务查询与等待 |

两种 Key 分开读取和存储。CLI 会先去除 Key 首尾空白并拒绝控制字符，再检查环境变量、安全存储或显式参数中已知的 Key 类型混用；未知前缀仍可用于兼容的自定义服务。`wk-...` Webhook Key 不会被读取或保存；本版也不提供 Webhook 接收服务。

## 端点

| 能力 | 方法与路径 | 请求格式 | Key |
| --- | --- | --- | --- |
| 查询模型 | `GET /v1/models` | 无请求体 | `SUPERTOKEN_API_KEY` |
| 同步生成 | `POST /v1/images/generations` | JSON | `SUPERTOKEN_API_KEY` |
| 同步编辑 | `POST /v1/images/edits` | JSON 或 multipart | `SUPERTOKEN_API_KEY` |
| 创建异步任务 | `POST /v1/image/tasks` | JSON 或 multipart | `SUPERTOKEN_API_KEY` |
| 查询异步任务 | `GET /v1/image/tasks/{task_id}` | 无请求体 | `SUPERTOKEN_RESOURCE_API_KEY` |

`models` 默认只输出 ID 中包含 `gpt-image-2` 的模型；传 `--all` 才输出全部模型。默认模型是 `gpt-image-2-count`。当 `n > 1` 或需要通过同步接口传递完整官方参数时，选择 `gpt-image-2`。只有用户明确指定，或模型列表确认可用时，才选择 `adobe-gpt-image-2-count`。

## 同步字段

### 生成 JSON

| CLI 参数 | JSON 字段 | 说明 |
| --- | --- | --- |
| `--model` | `model` | 默认 `gpt-image-2-count` |
| `--prompt` | `prompt` | 必填 |
| `--n` | `n` | `1..10`；`gpt-image-2-count` 只允许 `1` |
| `--size` | `size` | 默认 `1024x1024` |
| `--quality` | `quality` | 默认 `low` |
| `--format` | `output_format` | 可选：`png`、`jpeg`、`webp` |
| `--background` | `background` | 可选：`transparent`、`opaque`、`auto` |
| `--param key=value` | 顶层 `key` | 值先按 JSON 解析，无法解析时按字符串处理 |
| `--json-params FILE` | 合并到顶层 | 文件内容必须是 JSON 对象 |

同步响应从 `data[]` 读取每一项的 `url` 或 `b64_json`。

### 编辑 JSON

URL 输入使用顶层字符串数组：

```json
{
  "model": "gpt-image-2-count",
  "prompt": "改成黑白铅笔素描",
  "n": 1,
  "size": "1024x1024",
  "quality": "low",
  "image": ["https://example.com/source.png"]
}
```

Base64 输入使用对象数组：

```json
{
  "model": "gpt-image-2-count",
  "prompt": "改成黑白铅笔素描",
  "n": 1,
  "size": "1024x1024",
  "quality": "low",
  "image": [{"b64_json": "..."}]
}
```

`--format` 映射到 `output_format`，`--background` 映射到 `background`。`--param` 和 `--json-params` 也只合并到同步请求的顶层。同步编辑会发送 `n`；`gpt-image-2-count` 只允许 `n=1`，需要多结果时应使用 `--model gpt-image-2 --n 4`。同步 URL 或 Base64 编辑不接受 Mask。

### 编辑 multipart

本地同步编辑使用 `multipart/form-data`：

- 文本字段：`model`、`prompt`、`n`、`size`、`quality`，以及按需出现的 `output_format`、`background` 和同步额外参数。
- 每个 `--image` 重复发送一个 `image` 文件字段。
- 本地 `--mask` 发送一个 `mask` 文件字段。

## 异步字段

所有异步创建请求发送 `Idempotency-Key`。未传 `--idempotency-key` 时，CLI 会生成一个新值；显式值必须由 1 到 128 个 ASCII 可见非空白字符（`0x21..0x7e`）组成。

### 生成 JSON

```json
{
  "model": "gpt-image-2-count",
  "operation": "generation",
  "input": {"prompt": "夜间城市天际线"},
  "output": {
    "count": 1,
    "size": "1024x1024",
    "quality": "low"
  }
}
```

可选输出字段是 `format`、`compression` 和 `background`。`--client-reference-id` 映射到顶层 `client_reference_id`，最多 191 个字符；`--metadata-json` 必须是 JSON 对象并映射到顶层 `metadata`。`compression` 范围为 `0..100`。

### URL 编辑 JSON

```json
{
  "model": "gpt-image-2-count",
  "operation": "edit",
  "input": {
    "prompt": "改成黑白铅笔素描",
    "images": [{"url": "https://example.com/source.png"}],
    "mask": {"url": "https://example.com/mask.png"}
  },
  "output": {
    "count": 1,
    "size": "1024x1024",
    "quality": "low"
  }
}
```

`input.mask` 可省略。异步 URL 编辑不接受本地 Mask。

### 本地编辑 multipart

本地异步编辑使用扁平的 multipart 字段：

- 文本字段：`model`、`operation=edit`、`prompt`、`n`、`size`、`quality`。
- 可选文本字段：`output_format`、`output_compression`、`background`、`client_reference_id`、`metadata`。
- 每个本地参考图重复使用 `image` 文件字段；本地 Mask 使用一个 `mask` 文件字段。

异步创建成功后，CLI 只输出本地确定的 `mode`、`operation`、`model`、`idempotency_key`，以及校验后的 `task_id`、`status` 和可选 `progress`。响应提供 `Location` 时只保留 scheme、host 和 path；有效的 `Retry-After` 以数字输出。只创建任务时不能传 `--output`。

## 编辑输入与上传限制

一次 `edit` 必须且只能选择一种参考图输入：

| 输入 | CLI 写法 | 传输 | Mask |
| --- | --- | --- | --- |
| 本地文件 | 重复 `--image ./file.png` | multipart | 本地文件；同步与异步均支持 |
| URL | 重复 `--image https://...` | JSON | 仅异步 URL 编辑支持 URL Mask |
| Data URL | `--image data:image/...;base64,...` | JSON | 不支持 |
| 纯 Base64 文件 | 重复 `--image-base64-file ./image.txt` | JSON | 不支持 |

本地、URL 和 Base64 不能在同一次编辑中混用。Base64 只支持同步编辑；异步场景需要先在本 Skill 之外预上传，再把 URL 作为输入。

客户端在请求前检查：

- 参考图片为 `1..10` 张。
- 单个本地文件或解码后的 Base64 不超过 `20 MiB`。
- multipart 中所有图片和 Mask 的总量不超过 `100 MiB`。
- 本地文件内容必须是 PNG、JPEG 或 WebP；不只检查扩展名。
- Mask 最多一张。本地 Mask 应与原图尺寸一致，尺寸由服务端最终校验。

输入 URL 可使用 HTTP 或 HTTPS。生成结果下载只接受 HTTPS，重定向后的最终地址也必须是 HTTPS。

## 任务、重试与结果

| 任务状态 | 行为 |
| --- | --- |
| `queued` | 继续等待 |
| `in_progress` | 继续等待 |
| `succeeded` | 从 `result.images[]` 保存全部图片 |
| `failed` | 输出脱敏后的结构化错误并停止；即使 `retryable=true` 也不自动新建任务 |

`task TASK_ID` 查询一次，不下载结果，只输出请求的任务 ID、状态、可选进度和脱敏后的三字段错误摘要。`wait TASK_ID --output PATH` 会轮询并保存结果。`generate|edit --async --wait --output PATH` 在同一命令中创建、轮询和保存，因此两种 Key 都必须可用。`wait` 默认最长等待 900 秒，可用正整数 `--wait-timeout` 覆盖；每次 GET、轮询休眠和结果下载都不得越过同一个单调时钟截止时间。

同步生成、同步编辑和异步创建的 POST 都不自动重试。提交连接中断或响应失败时，CLI 会显示脱敏后的本次 `Idempotency-Key`；普通恢复键保留原值，当前凭据或具有凭据外观的值会被隐藏。确认或手动重试同一个异步请求时必须复用原值；不同请求使用新值。

任务查询 GET 遇到连接错误、`429`、`502` 或 `503` 时，最多允许连续失败三次。一次成功查询会重置计数。轮询优先采用 `Retry-After`；缺失时使用当前间隔，初始为 2 秒，每次限制在 2 到 30 秒。

同步结果从 `data[]` 保存，异步结果从 `result.images[]` 保存。同步响应必须返回与请求 `n` 相同的数量；恢复任务接受 1 到 10 张。单张远程图片最多 64 MiB，一次保存的解码结果合计最多 256 MiB。API 成功响应上限为 384 MiB，错误响应上限为 1 MiB。图片先写入目标目录中的唯一临时文件，全部校验通过后再替换目标项；后续项目失败时会清理本次输出。单图保留请求文件名的主干，多图依次命名为 `name-1.ext`、`name-2.ext`。扩展名统一为识别出的 `.png`、`.jpeg` 或 `.webp`，成功 `outputs[].path` 使用不解引用最终组件的绝对路径。

旧版 `generate_image.py` 只接受 v0.1 参数，默认超时仍为 180 秒，不显示或接受现代异步选项。`--json-params` 文件缺失、不可读、编码错误或 JSON 无效时，在请求前以退出码 `2` 结束。

## 状态码与退出码

| 状态或响应 | CLI 行为 |
| --- | --- |
| `400` | 提示检查错误参数和请求结构 |
| `401` | 提示对应环境变量中的 Key 无效或失效 |
| `403` | 区分模型 Token、资源 Key 和模型权限 |
| `409` | 说明同一 `Idempotency-Key` 对应了不同请求 |
| `413` | 说明单文件或 multipart 总量超限 |
| `429` | 提示请求频率或额度受限；POST 不重试 |
| `502` / `503` | 输出脱敏后的服务端请求 ID；POST 不重试，任务查询 GET 按上节有限重试 |
| 其他 `5xx` | 作为临时服务错误报告并输出脱敏后的请求 ID；不扩大 GET 重试范围 |
| 非 JSON | 输出最多 1000 字符的脱敏诊断 |
| JSON 结构异常 | 输出固定的脱敏错误，不回显响应体 |
| 空图片或无法识别格式 | 删除 `.part`，不报告成功 |

退出码：`0` 表示成功，`1` 表示网络、API 或结果处理失败，`2` 表示参数、配置或凭据错误。诊断会脱敏显式 Key、已知 Key 形态、Base64 图片内容，以及 URL 中的用户信息、查询参数和片段。

## English

This reference records the endpoints, fields, and client-side limits used by `scripts/supertoken_image.py`. See the parent [SKILL.md](../SKILL.md) for mode selection and common commands.

### API bases and authentication

The default base is `https://api.supertoken.cc`.

| Base form | Resolution rule |
| --- | --- |
| Root, such as `https://api.supertoken.cc` | Append `/v1/...` directly |
| Modern prefix, such as `https://api.supertoken.cc/v1` | Remove the duplicate `/v1` from the relative route before appending |
| Legacy prefix `https://api.supertoken.cc/image-wrapper/v1` | Allow only sync generation and sync editing, then append `/images/...` |

The legacy prefix is not the default. Use `--base-url https://api.supertoken.cc/image-wrapper/v1` only as an explicit one-command override for a compatible service or legacy sync test. `models`, async creation, `task`, and `wait` reject it.

A custom base must be a canonicalizable absolute HTTPS URL. The CLI trims surrounding ASCII whitespace, a trailing slash, and default port `:443`, and lowercases the scheme and host. Userinfo, query, fragment, controls, path whitespace, repeated separators, dot segments, and encoded slashes are rejected. Authenticated requests do not follow 3xx responses or forward `Authorization` or `Idempotency-Key` to a redirect target.

| Environment variable | Key type | Used for |
| --- | --- | --- |
| `SUPERTOKEN_API_KEY` | Model API Token (`sk-...`) | Model listing, sync generation, sync editing, async creation |
| `SUPERTOKEN_RESOURCE_API_KEY` | Resource API Key (`ak_...`) | Async task queries and waits |

The two keys are read and stored separately. The CLI trims surrounding key whitespace and rejects control characters before checking known type swaps from environment variables, secure storage, or explicit arguments. Unknown prefixes remain compatible with custom services. The CLI does not read or store a `wk-...` Webhook Key, and this version does not run a Webhook receiver.

### Endpoints

| Capability | Method and path | Request | Key |
| --- | --- | --- | --- |
| List models | `GET /v1/models` | No body | `SUPERTOKEN_API_KEY` |
| Sync generation | `POST /v1/images/generations` | JSON | `SUPERTOKEN_API_KEY` |
| Sync edit | `POST /v1/images/edits` | JSON or multipart | `SUPERTOKEN_API_KEY` |
| Create async task | `POST /v1/image/tasks` | JSON or multipart | `SUPERTOKEN_API_KEY` |
| Query async task | `GET /v1/image/tasks/{task_id}` | No body | `SUPERTOKEN_RESOURCE_API_KEY` |

`models` returns only IDs containing `gpt-image-2` unless `--all` is supplied. The default is `gpt-image-2-count`. Use `gpt-image-2` when `n > 1` or a sync request needs the full official parameters. Select `adobe-gpt-image-2-count` only when the user asks for it or the model list confirms access.

### Synchronous fields

#### Generation JSON

| CLI option | JSON field | Rule |
| --- | --- | --- |
| `--model` | `model` | Defaults to `gpt-image-2-count` |
| `--prompt` | `prompt` | Required |
| `--n` | `n` | `1..10`; `gpt-image-2-count` accepts only `1` |
| `--size` | `size` | Defaults to `1024x1024` |
| `--quality` | `quality` | Defaults to `low` |
| `--format` | `output_format` | Optional: `png`, `jpeg`, or `webp` |
| `--background` | `background` | Optional: `transparent`, `opaque`, or `auto` |
| `--param key=value` | Top-level `key` | Parse the value as JSON, falling back to a string |
| `--json-params FILE` | Merge at top level | The file must contain a JSON object |

Sync responses are read from every `data[]` item containing `url` or `b64_json`.

#### Edit JSON

URL references use a top-level string array:

```json
{
  "model": "gpt-image-2-count",
  "prompt": "Convert this to a black-and-white pencil sketch",
  "n": 1,
  "size": "1024x1024",
  "quality": "low",
  "image": ["https://example.com/source.png"]
}
```

Base64 references use an object array:

```json
{
  "model": "gpt-image-2-count",
  "prompt": "Convert this to a black-and-white pencil sketch",
  "n": 1,
  "size": "1024x1024",
  "quality": "low",
  "image": [{"b64_json": "..."}]
}
```

`--format` maps to `output_format`, and `--background` maps to `background`. `--param` and `--json-params` also merge only into synchronous top-level requests. Sync edits send `n`; `gpt-image-2-count` accepts only `n=1`, so use `--model gpt-image-2 --n 4` for multiple results. Sync URL and Base64 edits do not accept a Mask.

#### Edit multipart

Local sync edits use `multipart/form-data`:

- Text fields: `model`, `prompt`, `n`, `size`, `quality`, plus optional `output_format`, `background`, and sync extra parameters.
- Each `--image` produces a repeated `image` file field.
- A local `--mask` produces one `mask` file field.

### Asynchronous fields

Every async creation sends `Idempotency-Key`. The CLI generates a new value when `--idempotency-key` is omitted. An explicit value must contain 1-128 ASCII HTTP VCHAR bytes (`0x21..0x7e`).

#### Generation JSON

```json
{
  "model": "gpt-image-2-count",
  "operation": "generation",
  "input": {"prompt": "A city skyline at night"},
  "output": {
    "count": 1,
    "size": "1024x1024",
    "quality": "low"
  }
}
```

Optional output fields are `format`, `compression`, and `background`. `--client-reference-id` maps to top-level `client_reference_id` with a 191-character limit. `--metadata-json` must be a JSON object and maps to top-level `metadata`. `compression` must be in `0..100`.

#### URL edit JSON

```json
{
  "model": "gpt-image-2-count",
  "operation": "edit",
  "input": {
    "prompt": "Convert this to a black-and-white pencil sketch",
    "images": [{"url": "https://example.com/source.png"}],
    "mask": {"url": "https://example.com/mask.png"}
  },
  "output": {
    "count": 1,
    "size": "1024x1024",
    "quality": "low"
  }
}
```

`input.mask` is optional. An async URL edit does not accept a local Mask.

#### Local edit multipart

Local async edits use flat multipart fields:

- Text: `model`, `operation=edit`, `prompt`, `n`, `size`, and `quality`.
- Optional text: `output_format`, `output_compression`, `background`, `client_reference_id`, and `metadata`.
- Each local reference repeats the `image` file field; a local Mask uses one `mask` file field.

Successful async creation prints only locally derived `mode`, `operation`, `model`, and `idempotency_key`, plus validated `task_id`, `status`, optional `progress`, a scheme/host/path-only `location`, and numeric `retry_after` when available. A create-only invocation must not include `--output`.

### Edit inputs and upload limits

One `edit` invocation must select exactly one reference family:

| Input | CLI form | Transport | Mask |
| --- | --- | --- | --- |
| Local file | Repeat `--image ./file.png` | multipart | Local file in sync or async mode |
| URL | Repeat `--image https://...` | JSON | URL Mask only for async URL edits |
| Data URL | `--image data:image/...;base64,...` | JSON | Unsupported |
| Raw Base64 file | Repeat `--image-base64-file ./image.txt` | JSON | Unsupported |

Local, URL, and Base64 references cannot be mixed in one edit. Base64 is synchronous-only. For async use, upload outside this Skill and pass the resulting URL.

The client checks these limits before dispatch:

- `1..10` reference images.
- At most `20 MiB` for one local file or decoded Base64 image.
- At most `100 MiB` across multipart images and the Mask.
- Local content must be PNG, JPEG, or WebP; the suffix alone is not trusted.
- At most one Mask. A local Mask should match the source dimensions; the service performs the final dimension check.

Input URLs may use HTTP or HTTPS. Result downloads require HTTPS, including the final URL after redirects.

### Tasks, retries, and results

| Task status | Behavior |
| --- | --- |
| `queued` | Continue waiting |
| `in_progress` | Continue waiting |
| `succeeded` | Save every image from `result.images[]` |
| `failed` | Print a redacted structured error and stop; do not create a replacement task even when `retryable=true` |

`task TASK_ID` queries once without downloading and prints only the requested task ID, status, optional progress, and a redacted three-field error summary. `wait TASK_ID --output PATH` polls and saves. `generate|edit --async --wait --output PATH` creates, polls, and saves in one invocation, so both keys must be available. The default wait limit is 900 seconds; a positive `--wait-timeout` may override it. Every GET, polling sleep, and result download is capped by one monotonic deadline.

Sync generation, sync editing, and async creation POST requests are never retried automatically. If submission loses the connection or returns an error, the CLI prints that attempt's redacted `Idempotency-Key`; ordinary recovery keys remain intact, while active or credential-shaped values are hidden. Reuse the original value to confirm or manually retry the same async request. Use a new key for a different request.

Task-query GET requests tolerate at most three consecutive connection errors, `429`, `502`, or `503` responses. A successful query resets the count. Polling prefers `Retry-After`; otherwise it keeps the current interval, starting at 2 seconds and clamping every interval to 2 through 30 seconds.

Sync results come from `data[]`; async results come from `result.images[]`. A synchronous response must contain exactly the requested `n`; resumed tasks accept 1-10 images. One remote image is limited to 64 MiB and aggregate decoded output to 256 MiB. Successful API bodies are limited to 384 MiB and error bodies to 1 MiB. Unique temporary files are written in the destination directory and promoted only after every item passes validation; a later failure removes outputs from that save. One result keeps the requested stem; multiple results become `name-1.ext`, `name-2.ext`, and so on. The suffix is canonicalized to detected `.png`, `.jpeg`, or `.webp`. Successful `outputs[].path` values are lexical absolute paths that do not dereference the final component.

The legacy `generate_image.py` accepts only the v0.1 options and retains its 180-second default timeout. Modern asynchronous options do not appear in its help and are rejected. A missing, unreadable, invalid-UTF-8, or invalid-JSON `--json-params` file exits with code `2` before a request.

### Status and exit codes

| Status or response | CLI action |
| --- | --- |
| `400` | Ask the user to check invalid parameters and request structure |
| `401` | Identify the corresponding environment variable as invalid or expired |
| `403` | Distinguish model Token, resource Key, and model access |
| `409` | Explain that one `Idempotency-Key` was used for different requests |
| `413` | Report the single-file or multipart total limit |
| `429` | Report rate or credit limits; never retry a POST |
| `502` / `503` | Print a sanitized server request ID; do not retry POST, and apply bounded retries only to task-query GET |
| Other `5xx` | Report a temporary service error with a sanitized request ID without broadening GET retries |
| Non-JSON | Print up to 1000 characters of redacted diagnostics |
| Structurally malformed JSON | Print a fixed redacted error without echoing the response body |
| Empty or unrecognized image | Remove `.part` and do not report success |

Exit code `0` means success, `1` means a network, API, or result-processing failure, and `2` means a parameter, configuration, or credential error. Diagnostics redact explicit secrets, known key shapes, Base64 image bodies, and URL userinfo, query, and fragment.
