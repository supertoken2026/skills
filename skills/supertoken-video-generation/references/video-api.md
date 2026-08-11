# SuperToken 视频 API

本参考对应 `scripts/supertoken_video.py`。以下命令假设当前目录是 Skill 安装目录；否则用主 Skill 中的 `$SKILL_DIR/scripts/...` 路径。先运行 `models --all`，它输出当前账号可用的模型 ID；只从中选择 Adobe 或 Leonardo 视频模型。实时清单优先于下面的静态限制表。

## CLI 子命令

| 命令 | 用途与输出 |
| --- | --- |
| `models [--all]` | 列出模型；选择模型时使用 `--all` 的实时清单。 |
| `generate ...` | 创建任务；不带 `--wait` 时立即输出 JSON 的 `task_id`、`status` 和本次幂等键。 |
| `task <task_id>` | 用资源 Key 查询一次任务，输出 `task_id`、`status` 和可选 `progress`。 |
| `wait <task_id> --output <file>` | 轮询至完成并保存结果；可加 `--wait-timeout <seconds>`。 |
| `upload --file <path> --kind image|video|audio` | 诊断本地三步上传，输出 `kind` 和 `media_id`；正常生成不需要先运行它。 |

默认基础地址为 `https://api.supertoken.cc`。以上每个子命令都可追加 `--base-url <https://...>`，例如 `models --all --base-url https://api.example`。

## 最快的 Leonardo 示例

若实时清单中有 `leonardo-seedance-2.5-480p`，可以生成 4 秒文本视频：

```bash
python3 scripts/supertoken_video.py generate \
  --model leonardo-seedance-2.5-480p \
  --prompt "清晨的湖面，薄雾缓慢移动，固定镜头" \
  --duration 4 --aspect-ratio 16:9 \
  --wait --output ./lake.mp4
```

对 `generate`，`--wait` 与 `--output` 必须同时提供：前者轮询，后者指定下载文件。省略两者时只创建任务，JSON 的 `task_id` 可用于后续 `wait`；单独的 `wait` 始终需要 `--output`。`--output` 只适用于恰好一个视频结果；任务返回多个 `result.videos[]` 时 CLI 会拒绝该单一路径。

对已经创建的任务，可单独等待并调整总等待时间：

```bash
python3 scripts/supertoken_video.py wait <task-id> \
  --output ./result.mp4 --wait-timeout 1200
```

## 轮询与完成条件

`generate --wait` 和 `wait` 都会用资源 Key 请求 `GET /v1/video/tasks/{task_id}`。首次状态查询立即执行；任务处于 `queued` 或 `in_progress` 时，初始轮询间隔为 2 秒。服务端返回有效的 `Retry-After` 时，CLI 会采用该间隔，但不会低于 2 秒，并将最近的有效间隔作为后续查询的回退值。

状态为 `succeeded` 时，CLI 立即下载并保存视频；状态为 `failed` 时停止并报告任务失败。默认总等待上限为 900 秒，包含任务查询、等待和结果下载；需要更长时间时，先创建任务，再用独立的 `wait --wait-timeout` 命令等待。

终态失败时，CLI 会输出脱敏后的 `code`、`message`、`retryable`、`upstream_error_code` 和 `request_id`（服务端返回哪些字段就显示哪些），便于提交给渠道方；不会输出参考 URL、结果 URL 或 Key。

## 端点与 Key

| 操作 | API | 使用的 Key |
| --- | --- | --- |
| 列出模型 | `GET /v1/models` | 模型 Token `sk-...` |
| 创建视频 | `POST /v1/video/tasks` | 模型 Token `sk-...` |
| 查询任务 | `GET /v1/video/tasks/{task_id}` | 资源 Key `ak_...` |
| 上传本地素材 | `POST /v1/media/uploads`，预签名上传，`POST /v1/media/uploads/complete` | 资源 Key `ak_...` |

默认 API 基础地址是 `https://api.supertoken.cc`。上表中的模型/任务/上传 API 请求均使用 `Authorization: Bearer <对应 Key>`；第 2 步预签名上传是唯一例外，只使用服务器返回的 `method` 和 `headers`。

推荐使用 CLI。它会设置 Authorization 和幂等键、检查模型限制、上传本地素材、轮询并保存临时结果。直接调用 API 时，创建任务的请求体形状如下：

```json
{
  "model": "<live-model-id>",
  "operation": "generation",
  "input": {
    "prompt": "...",
    "reference_mode": "frame | images | media"
  },
  "output": {
    "duration": 4,
    "aspect_ratio": "16:9",
    "generate_audio": true
  }
}
```

向 `POST /v1/video/tasks` 发送模型 Token 的 Bearer Authorization，并为每次新建任务提供唯一的 `Idempotency-Key`。成功响应包含任务 `id` 和 `status`；用资源 Key 查询任务，直到状态为 `succeeded` 或 `failed`。

## 本地参考素材上传

日常使用无需手工上传：把本地文件传给 `generate --image`、`--video` 或 `--audio` 即可。每个本地参考文件最大 `512 MiB`；每个结果视频下载也最大 `512 MiB`。若直接调用 API，严格按三步进行：

1. `POST /v1/media/uploads`，请求体为：

   ```json
   {
     "files": [{
       "kind": "image | video | audio",
       "filename": "reference.png",
       "mime_type": "image/png",
       "size_bytes": 12345
     }]
   }
   ```

   返回 `data[0]` 中的 `id`、`method`、`upload_url` 和 `headers`。

2. 用返回的 `method` 向 `upload_url` 上传文件原始字节，并原样带上返回的 `headers`。这是预签名请求，不要自行附加资源 Key Authorization。

3. `POST /v1/media/uploads/complete`，请求体为 `{"upload_ids":["<id>"]}`。从返回 `data[0].url` 取得参考 URL；按“原始任务参考字段”中的模型规则把 URL 包装为 `{"url":"..."}` 后放入任务。

## `generate` 参数

| 参数 | 是否必填 | 说明 |
| --- | --- | --- |
| `--model` | 是 | `models --all` 中的 Adobe/Leonardo 视频 ID。 |
| `--prompt` | 是 | 1-1200 个字符的提示词。 |
| `--duration` | 是 | 秒数，必须符合模型限制。 |
| `--aspect-ratio` | 否 | 默认 `16:9`，可用比例取决于模型。 |
| `--no-audio` | 否 | 请求关闭音频；MiniMax H3 不支持。 |
| `--reference-mode` | 有参考素材时必填 | `frame`、`images` 或 `media`，必须符合模型限制。 |
| `--image` / `--video` / `--audio` | 否，可重复 | 本地文件自动上传；也可传直接参考 URL。 |
| `--wait` / `--output` | 否 | 对 `generate` 必须同时提供；仅支持恰好一个视频结果。 |
| `--client-reference-id` | 否 | 透传业务标识。 |
| `--metadata-json` | 否 | 严格的 JSON 对象，例如 `'{"project":"demo"}'`。 |
| `--idempotency-key` | 否 | 自定义创建幂等键；否则 CLI 自动生成。 |

`frame` 用图片作为起始/结束帧，允许的图片数由模型表决定；`images` 使用多图参考；`media` 可使用图片、视频和音频。无参考的文本生成不要传 `--reference-mode`，但 Veo 文本生成由 CLI 自动使用 `frame`。

本地路径必须存在。直接参考 URL 必须是干净的绝对公网 HTTPS URL：不能含首尾空白、用户名/密码，且无查询参数或片段。`--metadata-json` 必须是 JSON 对象，不接受数组、字符串、`NaN` 或 `Infinity`；`--idempotency-key` 为 1-255 个可打印 ASCII 字符（不含空格）。

## 模型限制

下列是 CLI 可做本地预校验的静态 ID 模式；它们不表示当前账号有权限，仍以 `models --all` 为准：

- Adobe Kling 3.0: `adobe-kling-3.0(?:-omni)?-(720p|1080p)`
- Adobe Veo 3.1: `adobe-veo-3.1-(standard|fast)-(720p|1080p)`
- Adobe Seedance 2.0: `adobe-seedance-2.0(?:-fast)?-(480p|720p)`
- Leonardo Seedance 2.0: `leonardo-seedance-2.0(?:-fast)?-[A-Za-z0-9]+`
- Leonardo Seedance 2.5: `leonardo-seedance-2.5-(480p|720p)`
- Leonardo MiniMax H3: `leonardo-minimax-h3-1440p`

| 渠道与模型族 | 时长 | 画幅 | 参考限制 | 音频 |
| --- | --- | --- | --- | --- |
| Adobe Kling 3.0 | 3-15 秒 | `16:9`、`9:16` | `frame` 0-2 图；仅 Omni 支持 `images` 1-3 图；不支持视频或音频参考 | 可用 `--no-audio` |
| Adobe Veo 3.1 | 4、6、8 秒 | `16:9`、`9:16` | `frame` 0-2 图；仅 Standard 支持 `images` 1-3 图，且必须 8 秒、`16:9` | 可用 `--no-audio` |
| Adobe Seedance 2.0 | 4-15 秒 | `21:9`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16` | `frame` 0-2 图；`media` 最多图/视频/音频 9/3/3，总数 12 | 可用 `--no-audio` |
| Leonardo Seedance 2.0 | 4-15 秒 | 同上六种 | 文本生成可无参考；有参考时仅 `media`，最多图/视频/音频 4/3/1，总数 8；Leonardo Seedance 2.0 的音频不能单独使用，至少同时提供一张图片或一个视频 | 可用 `--no-audio` |
| Leonardo Seedance 2.5 | 4-30 秒 | 同上六种 | `frame` 1-2 图；`media` 最多图/视频/音频 30/10/10，总数 50 | 可用 `--no-audio` |
| Leonardo MiniMax H3 | 5-15 秒 | 同上六种 | `frame` 1-2 图，`images` 1-5 图，或 `media` 1-5 图加 1-3 音频；不支持视频参考 | 始终开启 |

### 原始任务参考字段

直接 API 的每个参考值形如 `{"url":"https://..."}`。不要按字段名猜测：

- Seedance `frame` 的图片：第一张 `--image` 放入 `input.image`，其余图片依次放入 `input.reference_images[]`。
- Adobe/Leonardo Seedance `media` 的图片：全部按顺序放入 `input.reference_images[]`，不要把第一张改放到 `input.image`；视频和音频仍分别放入对应的数组。
- Kling Omni 与 Veo Standard 的 `images`：所有图片都放入 `input.reference_images[]`，不使用 `input.image`。
- MiniMax H3 的 `images`、`frame` 或 `media`：第一张放入 `input.image`，其余放入 `input.reference_images[]`。
- 视频和音频不论模式，分别使用 `input.reference_videos[]` 和 `input.reference_audios[]`。

## 参考素材与 Adobe 示例

Leonardo Seedance 2.5 的 `media` 模式可组合图片、视频和音频。音频不能单独使用，至少同时提供一张图片或一个视频：

```bash
python3 scripts/supertoken_video.py generate \
  --model leonardo-seedance-2.5-480p \
  --prompt "让画面中的人物回头看向镜头，保持柔和环境音" \
  --duration 4 --aspect-ratio 9:16 \
  --reference-mode media \
  --image ./start-frame.png --video ./motion.mp4 --audio ./ambience.mp3 \
  --wait --output ./portrait.mp4
```

Adobe 示例同样必须以实时清单为准：

```bash
python3 scripts/supertoken_video.py generate \
  --model adobe-kling-3.0-720p \
  --prompt "一只纸飞机掠过城市天际线，电影感光线" \
  --duration 3 --aspect-ratio 16:9 \
  --wait --output ./adobe-example.mp4
```

Adobe 的权限或渠道错误以 API 响应为准；记录脱敏后的错误摘要，然后改用实时清单中的 Leonardo 模型，不要猜测 ID 或反复提交。

## 已验证与常见错误

2026-08-11 已验证 Adobe Seedance 2.0 480p 的单图 `frame` 参考和 Leonardo Seedance 2.0 480p 的 `media` 图片参考，均完成 4 秒 MP4 下载；后者使用 `input.reference_images[]`。未把任务 ID、结果 URL 或任何 Key 写入文档。

| 提示 | 处理 |
| --- | --- |
| `selected model is not available in live inventory` | 重新运行 `models --all`，从输出复制模型 ID。 |
| `--reference-mode is required with references` | 参考素材存在时明确选择模式，并检查模型表。 |
| 模型时长/画幅错误 | 使用表中允许的组合；H3 最少 5 秒。 |
| `SUPERTOKEN_RESOURCE_API_KEY` 未设置 | 本地上传、`task`、`wait` 或受保护结果下载前运行 `setup.py --with-resource-key`。 |

结果 URL 为临时 HTTPS 地址。`url_auth` 省略、为 `null` 或 `none` 时，CLI 下载不附带 Authorization；仅为 `resource_api_key` 时附带 `Authorization: Bearer <资源 Key>`，其他值会报错。CLI 会先写入同目录 `.part` 文件，再原子替换目标文件；不要在日志、Issue 或对话中粘贴临时 URL 或 Key。
