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

## 配置 API Key

推荐运行安全配置脚本。macOS 和 Linux：

```bash
python3 skills/supertoken-gpt-image-2/scripts/setup.py
```

Windows PowerShell：

```powershell
py -3 skills/supertoken-gpt-image-2/scripts/setup.py
```

脚本在 macOS 使用 Keychain，在 Windows 使用 DPAPI，在安装了 `secret-tool` 的 Linux 环境使用 Secret Service。找不到系统安全存储时，不会自动保存明文 Key。

运行时先读取 `SUPERTOKEN_API_KEY`，再读取系统安全存储。只有系统安全存储不可用且明确传入 `--allow-plaintext-key-store` 时，才会把 Key 写入本地明文凭据文件；POSIX 系统会将该文件权限设为 `0600`。

如果只想在当前 macOS 或 Linux shell 中使用环境变量：

```bash
read -rsp "SuperToken API Key: " SUPERTOKEN_API_KEY
export SUPERTOKEN_API_KEY
```

## 直接运行

```bash
python3 skills/supertoken-gpt-image-2/scripts/generate_image.py --prompt "一只坐在阳光里的小猫" --output ./supertoken-kitten.png
```

切换到 `gpt-image-2`：

```bash
python3 skills/supertoken-gpt-image-2/scripts/generate_image.py --prompt "一只坐在阳光里的小猫" --model gpt-image-2 --output ./supertoken-kitten.png
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

The commands below use `python3` on macOS and Linux. Use `py -3` on Windows.

### Install

Install for Codex:

```bash
npx skills add supertoken2026/skills --skill supertoken-gpt-image-2 --agent codex --global
```

To use the Skill with both Codex and Claude Code, install it for both agents in one command:

```bash
npx skills add supertoken2026/skills --skill supertoken-gpt-image-2 --agent codex claude-code --global
```

The combined installation gives both agents one shared Skill copy, so one update keeps them in sync.

Update the Skill:

```bash
npx skills update supertoken-gpt-image-2
```

Use `npx skills update -g supertoken-gpt-image-2` for global installations and `npx skills update -p supertoken-gpt-image-2` for project installations. Automatic updates require an installation created by `npx skills add`.

### Configure the API Key

Run the secure setup script. On macOS and Linux:

```bash
python3 skills/supertoken-gpt-image-2/scripts/setup.py
```

On Windows PowerShell:

```powershell
py -3 skills/supertoken-gpt-image-2/scripts/setup.py
```

The setup script uses Keychain on macOS, DPAPI on Windows, and Secret Service on Linux when `secret-tool` is installed. It does not silently fall back to plaintext storage.

At runtime, `SUPERTOKEN_API_KEY` takes precedence over the operating-system credential store. Only when that store is unavailable and `--allow-plaintext-key-store` is explicitly set will the setup script write the Key to a local plaintext credential file; on POSIX systems, that file uses mode `0600`.

To use an environment variable only in the current macOS or Linux shell:

```bash
read -rsp "SuperToken API Key: " SUPERTOKEN_API_KEY
export SUPERTOKEN_API_KEY
```

### Run directly

```bash
python3 skills/supertoken-gpt-image-2/scripts/generate_image.py --prompt "A tiny fluffy kitten sitting in sunlight" --output ./supertoken-kitten.png
```

The default model is `gpt-image-2-count`. Pass `--model gpt-image-2` to use the alternate model. Requests go to `https://api.supertoken.cc/image-wrapper/v1/images/generations` and are not retried automatically.

### Support

- Documentation: <https://docs.supertoken.cc/>
- Website: <https://supertoken.cc/>
- Official QQ group: `1091860777`
- QQ support: `376064105`
- WeChat support: `piplszy`
- WeChat support: `minus502`
