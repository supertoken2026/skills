# SuperToken Skills

SuperToken 官方 Agent Skills。当前提供 GPT Image 2 图片生成与编辑能力，可在 Codex 和 Claude Code 中使用。

[English](README.en.md)

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

## 注意事项

- `SUPERTOKEN_API_KEY` 是 `sk-...` 模型 Token，用于生成、编辑、模型列表和异步创建。
- `SUPERTOKEN_RESOURCE_API_KEY` 是 `ak_...` 资源 Key，只用于异步任务查询和等待。
- `gpt-image-2-count` 是默认模型；当 `n > 1` 或需要完整 Images API 参数时，使用 `gpt-image-2`。
- 创建请求使用 POST，且不会自动重试；不支持 Webhook 接收和异步 Base64 编辑。

查询、等待任务或使用 `--async --wait` 前，在 Bash 或 zsh 中隐藏输入资源 Key：

```bash
printf "SuperToken Resource API Key: " >&2
IFS= read -r -s SUPERTOKEN_RESOURCE_API_KEY
printf "\n" >&2
export SUPERTOKEN_RESOURCE_API_KEY
```

任务查询、轮询休眠和结果下载共用一个截止时间。

## 升级

```bash
npx --yes skills@1.5.19 update supertoken-gpt-image-2
npx --yes skills@1.5.19 update -g supertoken-gpt-image-2
npx --yes skills@1.5.19 update -p supertoken-gpt-image-2
```

从默认分支安装且未指定 `#ref` 的版本可以正常更新。`#v0.1.0`、其他 tag 或 commit 安装会固定在该 ref；如需跟随默认分支，请重新执行未带 `#ref` 的安装命令。

## 详细参考

[GPT Image 2 API 参考](skills/supertoken-gpt-image-2/references/gpt-image-2-api.md) 说明端点映射、高级参数、限制和旧版基址行为。

## 支持

- 文档：<https://docs.supertoken.cc/>
- 官网：<https://supertoken.cc/>
- 官方 QQ 群：`1091860777`
- QQ 客服：`376064105`
- 微信客服：`piplszy`
- 微信客服：`minus502`
