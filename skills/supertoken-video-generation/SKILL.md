---
name: supertoken-video-generation
description: Use when generating, uploading reference media for, querying, waiting for, or saving Adobe or Leonardo videos through the SuperToken unified video task API.
---

# SuperToken Video Generation

Resolve the absolute-at-runtime directory containing this `SKILL.md` as `SKILL_DIR`; run every command with `python3 "$SKILL_DIR/scripts/supertoken_video.py"`. Read [the video API reference](references/video-api.md) before selecting parameters outside these minimum examples.

## Start With Models

`models --all` is mandatory before choosing an ID: it returns the raw live account inventory of `GET /v1/models`, which is the authority and wins over static examples. The inventory can include non-video IDs, such as image models; select an Adobe or Leonardo video model from that result. Default `models` is known-family convenience filtering only; it can omit a currently entitled ID and must not be used for selection.

```bash
python3 "$SKILL_DIR/scripts/setup.py" --with-resource-key
python3 "$SKILL_DIR/scripts/supertoken_video.py" models --all
python3 "$SKILL_DIR/scripts/supertoken_video.py" generate \
  --model <id-from-models-all> --prompt "A quiet sunrise over a lake" \
  --duration 4 --wait --output ./sunrise.mp4
```

Use `SUPERTOKEN_API_KEY` (model Token, `sk-...`) only for `models` and task creation. Use `SUPERTOKEN_RESOURCE_API_KEY` (resource Key, `ak_...`) for upload, task reads, waits, and only result downloads whose `url_auth` is `resource_api_key`.

```bash
python3 "$SKILL_DIR/scripts/supertoken_video.py" upload \
  --file ./reference.png --kind image
python3 "$SKILL_DIR/scripts/supertoken_video.py" task <task-id>
python3 "$SKILL_DIR/scripts/supertoken_video.py" wait <task-id> --output ./result.mp4
```

Pass a local reference to `generate` with `--image`, `--video`, or `--audio`; the script performs the upload protocol. Result URLs are temporary: save them locally and never report signed URLs. `url_auth` controls whether the resource Key is sent. This Skill does not expose xAI endpoints or a webhook receiver.
