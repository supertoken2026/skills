---
name: supertoken-video-generation
description: Generate Adobe and Leonardo videos through SuperToken, including model discovery, asynchronous task creation, safe media upload, polling, and local result saving.
---

# SuperToken Video Generation

Use this skill when a user needs to list available SuperToken video models, create an Adobe or Leonardo video task, check an asynchronous task, or save its finished videos locally.

## Credential Boundaries

- Use `SUPERTOKEN_API_KEY` only for model discovery and video-task creation.
- Use `SUPERTOKEN_RESOURCE_API_KEY` only for resource uploads, task lookups, and video downloads when the server explicitly requires `url_auth: resource_api_key`.
- Keep keys out of prompts, logs, filenames, and error messages. The bundled scripts validate key types and redact server diagnostics.

## Transport Safety

The scripts only use HTTPS, reject credentialed or private-literal media URLs, do not follow redirects, cap response and media sizes, and write downloads through same-directory `.part` files before atomic replacement.

## Scripts

`scripts/supertoken_video_config.py` provides isolated key lookup and API-base validation. `scripts/supertoken_video_api.py` provides JSON requests, media transfer helpers, and response parsing for the video workflow.
