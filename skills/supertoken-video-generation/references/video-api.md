# SuperToken Video API Reference

本参考对应 `scripts/supertoken_video.py`。选择前必须运行 `models --all`，取得 `GET /v1/models` 的原始实时账号清单；它始终优先于下表的静态已知 ID 和模型族，静态列表不能证明当前账号可用。清单也可能包含非视频 ID（例如图片模型），只能从中选择 Adobe 或 Leonardo 视频模型。默认 `models` 仅按已知模型族做便利过滤，可能遗漏当前账号已有权限的 ID，不能用于选择。

## 端点与密钥

| 操作 | 端点 | 密钥 |
| --- | --- | --- |
| 模型发现 | `GET /v1/models` | 模型 Token `sk-...` |
| 创建 | `POST /v1/video/tasks` | 模型 Token `sk-...` |
| 查询/轮询 | `GET /v1/video/tasks/{task_id}` | 资源 Key `ak_...` |
| 本地素材 | `POST /v1/media/uploads` -> 预签名 `PUT` -> `POST /v1/media/uploads/complete` | 资源 Key `ak_...` |

本地素材的三步上传必须全部成功，随后才把 complete 返回的 HTTPS URL 放进任务。创建使用 `Idempotency-Key`，不自动重试。结果 `result.videos[]` 的 URL 是临时公网 HTTPS 地址：省略/`null` 或显式 `url_auth: none` 均不附加 Authorization；仅 `url_auth: resource_api_key` 下载附加资源 Key。

## 模型矩阵

`GET /v1/models` wins over the static list in every model table.

| 渠道 | 静态已知 ID / 模型族 | 时长 | 画幅 | 参考模式 | 音频 |
| --- | --- | --- | --- | --- | --- |
| Adobe Kling 3.0 | `adobe-kling-3.0-{720p,1080p}`、`adobe-kling-3.0-omni-{720p,1080p}` | 3-15 秒 | `16:9`、`9:16` | `frame` 0-2 图；仅 Omni `images` 1-3 图；无媒体/视频/音频参考 | 可用 `--no-audio` |
| Adobe Veo 3.1 | `adobe-veo-3.1-{standard,fast}-{720p,1080p}` | 4、6、8 秒 | `16:9`、`9:16`；Standard 的 `images` 仅 8 秒、`16:9` | Standard/Fast: `frame` 0-2 张图片；Standard: `images` 1-3 张图片 | 可用 `--no-audio` |
| Adobe Seedance 2.0 | `adobe-seedance-2.0-{480p,720p}`；`adobe-seedance-*` 为族 | 4-15 秒 | `21:9`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16` | `frame` 0-2 图；`media` 图/视频/音频最多 9/3/3，总数最多 12 | 可用 `--no-audio` |
| Leonardo Seedance 2.0 | `leonardo-seedance-2.0-*`、`leonardo-seedance-2.0-fast-*` | 4-15 秒 | 同上六种 | 仅 `media`：图/视频/音频最多 4/3/1，总数最多 8；音频须搭配图或视频 | 可用 `--no-audio` |
| Leonardo Seedance 2.5 | `leonardo-seedance-2.5-{480p,720p}` | 4-30 秒 | 同上六种 | `frame` 1-2 图；`media` 图/视频/音频最多 30/10/10，总数最多 50；音频须搭配图或视频 | 可用 `--no-audio` |
| Leonardo MiniMax H3 | `leonardo-minimax-h3-1440p` | 5-15 秒 | 同上六种 | `frame` 1-2 图；`images` 1-5 图；`media` 1-5 图 + 1-3 音频，拒绝视频 | 固定开启，拒绝 `--no-audio` |

参考字段是模型专用语义：`frame` 的第一张 `--image` 映射到 `input.image`（起始帧），允许第二帧时映射到 `input.reference_images[0]`（结束帧）。Veo Standard 与 Kling Omni 的 `images` 将所有图片放进 `input.reference_images[]`，省略 `input.image`；H3 的 `images` 则以 `input.image` 为第一张，其余放进 `input.reference_images[]`。`media` 的图片同样首图在 `input.image`，其余在 `input.reference_images[]`；视频和音频始终分别使用 `input.reference_videos[]` 和 `input.reference_audios[]`。Veo 无图片文本生成默认发送 `reference_mode: frame`。参考素材存在时必须明确 `--reference-mode frame|images|media`。模型族范围仅用于本地预校验，不生成或猜测具体 ID。

## 任务与错误

创建负载为 `model`、`operation: "generation"`、`input.prompt`、可选 `input.reference_mode` 与参考 URL，及 `output.duration`、`output.aspect_ratio`、`output.generate_audio`。不要使用废弃的 `provider_options` 字段。任务包含 `id`、`status`、可选 `progress`、失败时的 `error.code`/`message`/`retryable`，成功时的 `result.videos[]`。状态为 `queued`、`in_progress`、`succeeded` 或 `failed`；轮询遵循 `Retry-After`，查询、睡眠和下载共享截止时间。所有错误和摘要都应脱敏密钥与签名 URL。

统一 API 不提供 xAI 独立视频端点或 webhook receiver。官方信息：<https://docs.supertoken.cc/>、<https://www.adobe.com/firefly/>、<https://leonardo.ai/>。

---

# English

This reference covers `scripts/supertoken_video.py`. Run `models --all` before every selection to obtain the raw live account inventory of `GET /v1/models`; it is authoritative for account entitlement and wins over every static ID or family below. The inventory can include non-video IDs such as image models, so select only an Adobe or Leonardo video model from it. Default `models` is known-family convenience filtering only and can omit an entitled ID, so it is not a selection command.

## Endpoints and keys

| Action | Endpoint | Key |
| --- | --- | --- |
| Discover models | `GET /v1/models` | model Token `sk-...` |
| Create task | `POST /v1/video/tasks` | model Token `sk-...` |
| Read or poll | `GET /v1/video/tasks/{task_id}` | resource Key `ak_...` |
| Upload local media | prepare -> presigned `PUT` -> complete | resource Key `ak_...` |

The upload protocol is: `POST /v1/media/uploads`, send the immutable local file to the returned presigned URL, then `POST /v1/media/uploads/complete`; only its confirmed HTTPS URL can be referenced. Creation uses `Idempotency-Key` and never retries automatically. `result.videos[]` URLs are temporary public HTTPS URLs when `url_auth` is omitted, `null`, or explicit `none`; those downloads have no Authorization header. Only `url_auth: resource_api_key` adds the resource Key.

## Models

`GET /v1/models` wins over the static list in every model table.

| Provider | Static known ID or family | Duration | Aspect ratio | Reference mode | Audio |
| --- | --- | --- | --- | --- | --- |
| Adobe Kling 3.0 | `adobe-kling-3.0-{720p,1080p}`, `adobe-kling-3.0-omni-{720p,1080p}` | 3-15 s | `16:9`, `9:16` | `frame` 0-2 images; Omni-only `images` 1-3; no media, video, or audio references | `--no-audio` allowed |
| Adobe Veo 3.1 | `adobe-veo-3.1-{standard,fast}-{720p,1080p}` | 4, 6, or 8 s | `16:9`, `9:16`; Standard `images`: 8 s, `16:9` | Standard/Fast `frame`: 0-2 images; Standard `images`: 1-3 images | `--no-audio` allowed |
| Adobe Seedance 2.0 | `adobe-seedance-2.0-{480p,720p}`; `adobe-seedance-*` is a family | 4-15 s | `21:9`, `16:9`, `4:3`, `1:1`, `3:4`, `9:16` | `frame` 0-2 images; `media` image/video/audio max 9/3/3, total max 12 | `--no-audio` allowed |
| Leonardo Seedance 2.0 | `leonardo-seedance-2.0-*`, `leonardo-seedance-2.0-fast-*` | 4-15 s | same six ratios | `media` only: image/video/audio max 4/3/1, total max 8; audio needs an image or video | `--no-audio` allowed |
| Leonardo Seedance 2.5 | `leonardo-seedance-2.5-{480p,720p}` | 4-30 s | same six ratios | `frame` 1-2 images; `media` image/video/audio max 30/10/10, total max 50; audio needs an image or video | `--no-audio` allowed |
| Leonardo MiniMax H3 | `leonardo-minimax-h3-1440p` | 5-15 s | same six ratios | `frame` 1-2 images; `images` 1-5; `media` 1-5 images + 1-3 audio, no video | always on; rejects `--no-audio` |

Reference fields have model-specific semantics. The first `--image` in `frame` maps to `input.image` (the start frame); an allowed second frame maps to `input.reference_images[0]` (the end frame). Veo Standard and Kling Omni `images` put every image in `input.reference_images[]` and omit `input.image`; H3 `images` puts the first image in `input.image` and later images in `input.reference_images[]`. Media images use that same first-image/remaining-images layout, while video and audio always use `input.reference_videos[]` and `input.reference_audios[]`. Text-only Veo defaults to `reference_mode: frame`. Specify `--reference-mode` whenever references are present. Family ranges support only local preflight validation; they do not create entitlement or invent an ID.

## Task fields, states, and errors

Create with `model`, `operation: "generation"`, `input.prompt`, optional `input.reference_mode` and reference URLs, then `output.duration`, `output.aspect_ratio`, and `output.generate_audio`. Do not use deprecated `provider_options`. Task fields include `id`, `status`, optional `progress`, `error.code`/`message`/`retryable` on failure, and `result.videos[]` on success. Statuses are `queued`, `in_progress`, `succeeded`, and `failed`; honor `Retry-After` and share one deadline across reads, sleeps, and downloads. Redact credentials and signed URLs from diagnostics.

The unified API does not expose xAI endpoints or a webhook receiver. Official resources: <https://docs.supertoken.cc/>, <https://www.adobe.com/firefly/>, and <https://leonardo.ai/>.
