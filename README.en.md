# SuperToken Skills

SuperToken Agent Skills currently provides GPT Image 2 image generation and editing for Codex and Claude Code.

[中文](README.md)

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

## Notes

- `SUPERTOKEN_API_KEY` is the model Token (`sk-...`) for generation, edit, model listing, and async creation.
- `SUPERTOKEN_RESOURCE_API_KEY` is the resource Key (`ak-...`) for async task queries and waits.
- `gpt-image-2-count` is the default; use `gpt-image-2` for `n > 1` or full Images API parameters.
- Creation POST requests do not retry automatically. Webhook receiving and asynchronous Base64 editing are unsupported.

Task queries, polling sleeps, and result downloads share one deadline.

## Upgrade

```bash
npx --yes skills@1.5.19 update supertoken-gpt-image-2
npx --yes skills@1.5.19 update -g supertoken-gpt-image-2
npx --yes skills@1.5.19 update -p supertoken-gpt-image-2
```

An unversioned installation installed from the default branch can update normally. An installation using `#v0.1.0`, any tag, or a commit remains pinned to that ref until it is reinstalled without `#ref`.

## Reference

See the [GPT Image 2 API reference](skills/supertoken-gpt-image-2/references/gpt-image-2-api.md) for endpoint mapping, advanced parameters, limits, and legacy base behavior.

## Support

- Documentation: <https://docs.supertoken.cc/>
- Website: <https://supertoken.cc/>
- QQ group: `1091860777`
- QQ support: `376064105`
- WeChat: `piplszy`
- WeChat: `minus502`
