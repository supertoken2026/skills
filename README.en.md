# SuperToken Skills

SuperToken Agent Skills provides GPT Image 2 image generation and editing, plus Adobe and Leonardo video generation, for Codex and Claude Code.

[中文](README.md)

## Table of Contents

- [Quick start](#quick-start)
- [Common commands](#common-commands)
- [Video generation](#video-generation)
- [Notes](#notes)
- [Upgrade](#upgrade)
- [Reference](#reference)
- [Support](#support)

## Quick start

1. Install for Codex:

   ```bash
   npx --yes skills@1.5.19 add supertoken2026/skills --skill supertoken-gpt-image-2 --agent codex --global
   ```

   Install for both Codex and Claude Code:

   ```bash
   npx --yes skills@1.5.19 add supertoken2026/skills --skill supertoken-gpt-image-2 --agent codex claude-code --global
   ```

2. Follow the `skills` CLI output to the installed directory. A common Codex global location is `~/.agents/skills/supertoken-gpt-image-2`; locations differ by client and project installation.

3. Configure the model Token. On macOS and Linux:

   ```bash
   python3 scripts/setup.py
   ```

   In Windows PowerShell:

   ```powershell
   py -3 scripts/setup.py
   ```

4. Generate one image:

   ```bash
   python3 scripts/supertoken_image.py generate \
     --prompt "A tiny kitten sitting in sunlight" \
     --output ./supertoken-kitten.png
   ```

## Common commands

List available models:

```bash
python3 scripts/supertoken_image.py models
```

Edit with a local image and local Mask:

```bash
python3 scripts/supertoken_image.py edit \
  --prompt "Replace the Mask area with a beach at sunset" \
  --image ./source.png \
  --mask ./mask.png \
  --output ./edited.png
```

Edit from a URL image:

```bash
python3 scripts/supertoken_image.py edit \
  --prompt "Convert this to a black-and-white pencil sketch" \
  --image https://example.com/source.png \
  --output ./sketch.png
```

Create an asynchronous task without waiting:

```bash
python3 scripts/supertoken_image.py generate \
  --prompt "A city skyline at night" \
  --async
```

Create, wait, and save results in one command:

```bash
python3 scripts/supertoken_image.py generate \
  --prompt "A city skyline at night" \
  --async \
  --wait \
  --output ./skyline.png
```

Wait for an existing task and save results:

```bash
python3 scripts/supertoken_image.py wait TASK_ID \
  --output ./skyline.png
```

## Video generation

Install the video Skill:

```bash
npx --yes skills@1.5.19 add supertoken2026/skills --skill supertoken-video-generation --agent codex claude-code --global
```

In the installed `supertoken-video-generation` directory reported by the `skills` CLI, save the two separate credentials through hidden prompts:

```bash
python3 scripts/setup.py --with-resource-key
```

List the account's live model IDs first. Select an Adobe or Leonardo video model only from `models --all`; that list wins over static examples. Default `models` is known-family convenience filtering and must not be used for selection:

```bash
python3 scripts/supertoken_video.py models --all
```

Minimum Leonardo Seedance create, wait, and save example (use it only when that model ID is in the live list):

```bash
python3 scripts/supertoken_video.py generate \
  --model leonardo-seedance-2.5-480p \
  --prompt "Morning mist slowly rising from a quiet lake" \
  --duration 4 \
  --wait --output ./sunrise.mp4
```

Video uses `SUPERTOKEN_API_KEY` (model Token, `sk-...`) for model discovery and task creation. `SUPERTOKEN_RESOURCE_API_KEY` (resource Key, `ak_...`) is for local media upload, task reads, waits, and a temporary result URL only when `url_auth` is `resource_api_key`. Pass local references directly to `generate --image/--video/--audio`; the CLI uploads them. See the [video API reference](skills/supertoken-video-generation/references/video-api.md) for parameters, limits, and examples.

## Notes

- Video `SUPERTOKEN_API_KEY` is the model Token (`sk-...`) for model listing and task creation.
- Video `SUPERTOKEN_RESOURCE_API_KEY` is the resource Key (`ak_...`) for local media uploads, task queries, waits, and protected result downloads.
- `gpt-image-2-count` is the default; use `gpt-image-2` for `n > 1` or full Images API parameters.
- Creation POST requests do not retry automatically. Webhook receiving and asynchronous Base64 editing are unsupported.

Before using video `task` or `wait`, enter the resource Key without echoing it in Bash or zsh:

```bash
printf "SuperToken Resource API Key: " >&2
IFS= read -r -s SUPERTOKEN_RESOURCE_API_KEY
printf "\n" >&2
export SUPERTOKEN_RESOURCE_API_KEY
```

The standalone video `wait` command defaults to 900 seconds; adjust it with `--wait-timeout`.

## Upgrade

```bash
npx --yes skills@1.5.19 update supertoken-gpt-image-2
npx --yes skills@1.5.19 update -g supertoken-gpt-image-2
npx --yes skills@1.5.19 update -p supertoken-gpt-image-2
npx --yes skills@1.5.19 update supertoken-video-generation
npx --yes skills@1.5.19 update -g supertoken-video-generation
npx --yes skills@1.5.19 update -p supertoken-video-generation
```

An unversioned installation installed from the default branch can update normally. An installation using `#v0.1.0`, any tag, or a commit remains pinned to that ref until it is reinstalled without `#ref`.

## Reference

See the [GPT Image 2 API reference](skills/supertoken-gpt-image-2/references/gpt-image-2-api.md) for endpoint mapping, advanced parameters, limits, and legacy base behavior.

See the [video API reference](skills/supertoken-video-generation/references/video-api.md) for unified video tasks, model constraints, upload, polling, and temporary result downloads.

## Support

- Documentation: <https://docs.supertoken.cc/>
- Website: <https://supertoken.cc/>
- QQ group: `1091860777`
- QQ support: `376064105`
- WeChat: `piplszy`
- WeChat: `minus502`
