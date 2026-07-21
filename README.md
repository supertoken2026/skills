# SuperToken Skills

这是 SuperToken 官方 Agent Skills 仓库。首个 Skill 通过 SuperToken 兼容 OpenAI 的图片接口生成并保存图片，支持 Codex 和 Claude Code。

## 可用 Skills

| Skill | 说明 |
| --- | --- |
| `supertoken-gpt-image-2` | 默认使用 `gpt-image-2-count` 生成图片，也支持切换到 `gpt-image-2` |

## 环境要求

- Python 3.10 或更高版本
- 可以访问 `https://api.supertoken.cc/image-wrapper/v1`
- 有效的 SuperToken API Key
- 使用安装和升级命令时，需要 Node.js 22.20.0 或更高版本

## 安装

安装到 Codex：

```bash
npx skills add supertoken2026/skills \
  --skill supertoken-gpt-image-2 \
  --agent codex \
  --global
```

安装到 Claude Code：

```bash
npx skills add supertoken2026/skills \
  --skill supertoken-gpt-image-2 \
  --agent claude-code \
  --global
```

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

## 配置 API Key

推荐使用环境变量：

```bash
read -rsp "SuperToken API Key: " SUPERTOKEN_API_KEY
export SUPERTOKEN_API_KEY
```

也可以运行安全配置脚本：

```bash
python3 skills/supertoken-gpt-image-2/scripts/setup.py
```

脚本在 macOS 使用 Keychain，在 Windows 使用 DPAPI，在安装了 `secret-tool` 的 Linux 环境使用 Secret Service。找不到系统安全存储时，不会自动保存明文 Key。

## 直接运行

```bash
python3 skills/supertoken-gpt-image-2/scripts/generate_image.py \
  --prompt "一只坐在阳光里的小猫" \
  --output /tmp/supertoken-kitten.png
```

切换到 `gpt-image-2`：

```bash
python3 skills/supertoken-gpt-image-2/scripts/generate_image.py \
  --prompt "一只坐在阳光里的小猫" \
  --model gpt-image-2 \
  --output /tmp/supertoken-kitten.png
```

接口地址为 `https://api.supertoken.cc/image-wrapper/v1/images/generations`。生成请求不会自动重试。

## 支持

- 文档：<https://docs.supertoken.cc/>
- 官网：<https://supertoken.cc/>
- 官方 QQ 群：`1091860777`
- QQ 客服：`376064105`
- 微信客服：`piplszy`
- 微信客服：`minus502`

## English

This is the official Agent Skills repository for SuperToken. The first Skill generates and saves images through the SuperToken OpenAI-compatible image API and supports both Codex and Claude Code.

### Requirements

- Python 3.10 or newer
- Network access to `https://api.supertoken.cc/image-wrapper/v1`
- A valid SuperToken API Key
- Node.js 22.20.0 or newer for installation and updates through `npx skills`

### Install

Install for Codex:

```bash
npx skills add supertoken2026/skills --skill supertoken-gpt-image-2 --agent codex --global
```

Install for Claude Code:

```bash
npx skills add supertoken2026/skills --skill supertoken-gpt-image-2 --agent claude-code --global
```

Update the Skill:

```bash
npx skills update supertoken-gpt-image-2
```

Use `npx skills update -g supertoken-gpt-image-2` for global installations and `npx skills update -p supertoken-gpt-image-2` for project installations. Automatic updates require an installation created by `npx skills add`.

### Configure the API Key

Use an environment variable or the secure setup script:

```bash
read -rsp "SuperToken API Key: " SUPERTOKEN_API_KEY
export SUPERTOKEN_API_KEY
```

```bash
python3 skills/supertoken-gpt-image-2/scripts/setup.py
```

The setup script uses Keychain on macOS, DPAPI on Windows, and Secret Service on Linux when `secret-tool` is installed. It does not silently fall back to plaintext storage.

### Run directly

```bash
python3 skills/supertoken-gpt-image-2/scripts/generate_image.py \
  --prompt "A tiny fluffy kitten sitting in sunlight" \
  --output /tmp/supertoken-kitten.png
```

The default model is `gpt-image-2-count`. Pass `--model gpt-image-2` to use the alternate model. Requests go to `https://api.supertoken.cc/image-wrapper/v1/images/generations` and are not retried automatically.

### Support

- Documentation: <https://docs.supertoken.cc/>
- Website: <https://supertoken.cc/>
- Official QQ group: `1091860777`
- QQ support: `376064105`
- WeChat support: `piplszy`
- WeChat support: `minus502`
