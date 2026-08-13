# SuperToken Skills

SuperToken 官方 Agent Skills。当前提供 GPT Image 2 图片生成与编辑，以及 Adobe 和 Leonardo 视频生成能力，可在 Codex 和 Claude Code 中使用。

[English](README.en.md)

## 目录

- [快速开始](#快速开始)
- [常用命令](#常用命令)
- [视频生成](#视频生成)
- [注意事项](#注意事项)
- [升级](#升级)
- [详细参考](#详细参考)
- [支持](#支持)

## 快速开始

1. 安装到 Codex：

   ```bash
   npx --yes skills@1.5.19 add supertoken2026/skills --skill supertoken-gpt-image-2 --agent codex --global
   ```

   同时安装到 Codex 和 Claude Code：

   ```bash
   npx --yes skills@1.5.19 add supertoken2026/skills --skill supertoken-gpt-image-2 --agent codex claude-code --global
   ```

2. 按 `skills` CLI 输出进入安装目录。Codex 全局安装的常见位置是 `~/.agents/skills/supertoken-gpt-image-2`；不同客户端和项目安装的位置可能不同。

3. 配置模型 Token。macOS 和 Linux：

   ```bash
   python3 scripts/setup.py
   ```

   Windows PowerShell：

   ```powershell
   py -3 scripts/setup.py
   ```

4. 生成一张图片：

   ```bash
   python3 scripts/supertoken_image.py generate \
     --prompt "一只坐在阳光里的小猫" \
     --output ./supertoken-kitten.png
   ```

## 常用命令

查询可用模型：

```bash
python3 scripts/supertoken_image.py models
```

使用本地图片和本地 Mask 编辑：

```bash
python3 scripts/supertoken_image.py edit \
  --prompt "将 Mask 区域改成日落海边" \
  --image ./source.png \
  --mask ./mask.png \
  --output ./edited.png
```

使用 URL 图片编辑：

```bash
python3 scripts/supertoken_image.py edit \
  --prompt "改成黑白铅笔素描" \
  --image https://example.com/source.png \
  --output ./sketch.png
```

只创建异步任务：

```bash
python3 scripts/supertoken_image.py generate \
  --prompt "夜间城市天际线" \
  --async
```

创建任务、等待并保存结果：

```bash
python3 scripts/supertoken_image.py generate \
  --prompt "夜间城市天际线" \
  --async \
  --wait \
  --output ./skyline.png
```

等待已有任务并保存结果：

```bash
python3 scripts/supertoken_image.py wait TASK_ID \
  --output ./skyline.png
```

## 视频生成

安装视频 Skill：

```bash
npx --yes skills@1.5.19 add supertoken2026/skills --skill supertoken-video-generation --agent codex claude-code --global
```

进入 `skills` CLI 输出的 `supertoken-video-generation` 安装目录后，以隐藏输入保存两个独立凭据：

```bash
python3 scripts/setup.py --with-resource-key
```

先查询当前账号可用的模型 ID。只从 `models --all` 输出中选择 Adobe 或 Leonardo 视频模型；它优先于静态示例。默认 `models` 只是已知模型族的便利过滤，不能用于选择：

```bash
python3 scripts/supertoken_video.py models --all
```

Leonardo Seedance 最小生成、等待并保存示例（仅当模型 ID 出现在实时清单中）：

```bash
python3 scripts/supertoken_video.py generate \
  --model leonardo-seedance-2.5-480p \
  --prompt "清晨湖面上缓慢升起的薄雾" \
  --duration 4 \
  --wait --output ./sunrise.mp4
```

视频使用 `SUPERTOKEN_API_KEY`（`sk-...` 模型 Token）查询模型和创建任务；`SUPERTOKEN_RESOURCE_API_KEY`（`ak_...` 资源 Key）用于本地素材上传、任务查询、等待，以及仅在 `url_auth: resource_api_key` 时下载临时结果 URL。把本地参考素材直接传给 `generate --image/--video/--audio`，CLI 会自动上传。完整参数、模型时长/画幅限制和 Adobe/Leonardo 示例见 [视频 API 参考](skills/supertoken-video-generation/references/video-api.md)。

## 注意事项

- 视频 `SUPERTOKEN_API_KEY` 是 `sk-...` 模型 Token，用于模型列表和创建任务。
- 视频 `SUPERTOKEN_RESOURCE_API_KEY` 是 `ak_...` 资源 Key，用于本地素材上传、任务查询、等待和受保护结果下载。
- `gpt-image-2-count` 是默认模型；当 `n > 1` 或需要完整 Images API 参数时，使用 `gpt-image-2`。
- 创建请求使用 POST，且不会自动重试；不支持 Webhook 接收和异步 Base64 编辑。

使用视频 `task` 或 `wait` 前，在 Bash 或 zsh 中隐藏输入资源 Key：

```bash
printf "SuperToken Resource API Key: " >&2
IFS= read -r -s SUPERTOKEN_RESOURCE_API_KEY
printf "\n" >&2
export SUPERTOKEN_RESOURCE_API_KEY
```

独立视频 `wait` 命令默认最多等待 900 秒；可用 `--wait-timeout` 调整。

## 升级

```bash
npx --yes skills@1.5.19 update supertoken-gpt-image-2
npx --yes skills@1.5.19 update -g supertoken-gpt-image-2
npx --yes skills@1.5.19 update -p supertoken-gpt-image-2
npx --yes skills@1.5.19 update supertoken-video-generation
npx --yes skills@1.5.19 update -g supertoken-video-generation
npx --yes skills@1.5.19 update -p supertoken-video-generation
```

从默认分支安装且未指定 `#ref` 的版本可以正常更新。`#v0.1.0`、其他 tag 或 commit 安装会固定在该 ref；如需跟随默认分支，请重新执行未带 `#ref` 的安装命令。

## 详细参考

[GPT Image 2 API 参考](skills/supertoken-gpt-image-2/references/gpt-image-2-api.md) 说明端点映射、高级参数、限制和旧版基址行为。

[视频 API 参考](skills/supertoken-video-generation/references/video-api.md) 说明统一视频任务、模型限制、上传、轮询和临时结果下载。

## 支持

- 文档：<https://docs.supertoken.cc/>
- 官网：<https://supertoken.cc/>
- 官方 QQ 群：`1091860777`
- QQ 客服：`376064105`
- 微信客服：`piplszy`
- 微信客服：`minus502`
