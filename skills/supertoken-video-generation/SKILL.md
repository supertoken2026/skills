---
name: supertoken-video-generation
description: Use when listing available Adobe or Leonardo video models, generating SuperToken videos, adding local image/video/audio references, polling video tasks, or saving video results.
---

# SuperToken 视频生成

通过 SuperToken 统一视频 API 使用 Adobe 和 Leonardo 模型。把本文件所在绝对目录设为 `SKILL_DIR`，并用 `python3 "$SKILL_DIR/scripts/supertoken_video.py"` 运行命令。

## 快速使用

1. 以隐藏输入保存模型 Token 和资源 Key：

```bash
python3 "$SKILL_DIR/scripts/setup.py" --with-resource-key
```

2. 查询当前账号真正可用的模型 ID，只从输出中选择 Adobe 或 Leonardo 视频模型：

```bash
python3 "$SKILL_DIR/scripts/supertoken_video.py" models --all
```

3. 若上一步包含 `leonardo-seedance-2.5-480p`，可直接生成并保存一个 4 秒视频：

```bash
python3 "$SKILL_DIR/scripts/supertoken_video.py" generate \
  --model leonardo-seedance-2.5-480p \
  --prompt "清晨湖面上缓慢升起的薄雾，固定镜头" \
  --duration 4 --wait --output ./lake.mp4
```

## 创建后再等待

去掉 `--wait --output` 会只创建任务，并立即输出 JSON；其中的 `task_id` 可交给后续命令：

```bash
python3 "$SKILL_DIR/scripts/supertoken_video.py" generate \
  --model leonardo-seedance-2.5-480p \
  --prompt "清晨湖面上缓慢升起的薄雾，固定镜头" \
  --duration 4
# {"task_id":"...","status":"...",...}

python3 "$SKILL_DIR/scripts/supertoken_video.py" wait <task-id> \
  --output ./lake.mp4 --wait-timeout 1200
```

`task <task-id>` 只查询一次状态；`upload --file <path> --kind image|video|audio` 是检查本地上传的诊断命令，日常生成直接使用 `generate --image`、`--video` 或 `--audio` 即可。所有子命令都可追加 `--base-url https://...` 覆盖默认 API 地址。

`SUPERTOKEN_API_KEY` 是模型 Token（`sk-...`），用于模型列表和创建任务；`SUPERTOKEN_RESOURCE_API_KEY` 是资源 Key（`ak_...`），用于本地素材上传、查询/等待任务和受保护结果下载。不要把任一 Key 放到命令行、日志或对话中。

本地参考素材直接传给 `generate --image`、`--video` 或 `--audio`；同时指定 `--reference-mode frame|images|media`。CLI 会自动上传素材并创建任务；只有同时传入 `--wait --output` 才会轮询并下载，否则立即输出可交给 `wait` 的 `task_id`。模型时长、画幅、参考素材限制、直接 API 请求格式，以及 Adobe/Leonardo 示例见 [参考文档](references/video-api.md)。
