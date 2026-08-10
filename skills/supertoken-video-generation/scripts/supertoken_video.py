#!/usr/bin/env python3
"""Command line client for SuperToken's unified asynchronous video tasks."""

import argparse
import json
import math
import re
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

import supertoken_video_api as api
from supertoken_video_config import (
    DEFAULT_BASE_URL,
    ConfigError,
    get_model_key,
    get_resource_key,
    normalize_base_url,
)


HTTP_TIMEOUT = 30
WAIT_TIMEOUT = 900
MIN_POLL_DELAY = 2.0
MAX_PROMPT_LENGTH = 1200
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_IDEMPOTENCY_KEY = re.compile(r"[!-~]{1,255}\Z")
_KLING = re.compile(r"adobe-kling-3\.0(?:-omni)?-(?:720p|1080p)\Z")
_VEO = re.compile(r"adobe-veo-3\.1-(standard|fast)-(?:720p|1080p)\Z")
_ADOBE_SEEDANCE = re.compile(r"adobe-seedance-2\.0-(?:480p|720p)\Z")
_LEONARDO_SEEDANCE_20 = re.compile(r"leonardo-seedance-2\.0(?:-fast)?-[A-Za-z0-9]+\Z")
_LEONARDO_SEEDANCE_25 = re.compile(r"leonardo-seedance-2\.5-(?:480p|720p)\Z")
_H3 = "leonardo-minimax-h3-1440p"


class _ArgumentParser(argparse.ArgumentParser):
    """Raise a controlled error without echoing untrusted command-line values."""

    def error(self, _message):
        raise api.ApiUsageError("invalid command line arguments")


def _common_options(parser, key_name):
    parser.add_argument("--base-url")
    parser.add_argument(key_name)


def parse_args(argv=None):
    parser = _ArgumentParser(description="Create and retrieve SuperToken video tasks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    models = subparsers.add_parser("models")
    models.add_argument("--all", action="store_true")
    _common_options(models, "--api-key")

    upload = subparsers.add_parser("upload")
    upload.add_argument("--file", required=True)
    upload.add_argument("--kind", choices=("image", "video", "audio"), required=True)
    _common_options(upload, "--resource-api-key")

    generate = subparsers.add_parser("generate")
    generate.add_argument("--model", required=True)
    generate.add_argument("--prompt", required=True)
    generate.add_argument("--duration", required=True, type=int)
    generate.add_argument("--aspect-ratio", default="16:9")
    generate.add_argument("--no-audio", action="store_true")
    generate.add_argument("--reference-mode", choices=("frame", "images", "media"))
    for kind in ("image", "video", "audio"):
        generate.add_argument(f"--{kind}", action="append", default=[])
    generate.add_argument("--client-reference-id")
    generate.add_argument("--metadata-json")
    generate.add_argument("--idempotency-key")
    generate.add_argument("--wait", action="store_true")
    generate.add_argument("--output")
    _common_options(generate, "--api-key")
    generate.add_argument("--resource-api-key")

    task = subparsers.add_parser("task")
    task.add_argument("task_id")
    _common_options(task, "--resource-api-key")

    wait = subparsers.add_parser("wait")
    wait.add_argument("task_id")
    wait.add_argument("--output", required=True)
    wait.add_argument("--wait-timeout", type=float, default=WAIT_TIMEOUT)
    _common_options(wait, "--resource-api-key")
    return parser.parse_args(argv)


def _model_kind(model):
    if _KLING.fullmatch(model):
        return "kling"
    if _VEO.fullmatch(model):
        return "veo"
    if _ADOBE_SEEDANCE.fullmatch(model):
        return "adobe-seedance"
    if _LEONARDO_SEEDANCE_20.fullmatch(model):
        return "leonardo-seedance-20"
    if _LEONARDO_SEEDANCE_25.fullmatch(model):
        return "leonardo-seedance-25"
    if model == _H3:
        return "h3"
    return None


def _reference_values(args):
    return [(kind, value) for kind in ("image", "video", "audio") for value in getattr(args, kind, [])]


def _validate_reference_url(value):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise api.ApiUsageError("reference URL must be a clean absolute HTTPS URL")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise api.ApiUsageError("reference URL must be a clean absolute HTTPS URL") from exc
    if (
        parsed.scheme.lower() != "https" or not parsed.netloc or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment
    ):
        raise api.ApiUsageError("reference URL must be a clean absolute HTTPS URL")
    return api.validate_public_url(value, "reference URL")


def _validate_task_id(task_id):
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
        raise api.ApiUsageError("task ID format is invalid")
    return task_id


def _validate_generate_args(args):
    prompt = args.prompt.strip()
    if not prompt or len(prompt) > MAX_PROMPT_LENGTH:
        raise api.ApiUsageError("prompt must contain 1 to 1200 Unicode characters")
    args.prompt = prompt
    if args.duration <= 0:
        raise api.ApiUsageError("--duration must be positive")
    references = _reference_values(args)
    if references and args.reference_mode is None:
        raise api.ApiUsageError("--reference-mode is required with references")
    if args.wait and not args.output:
        raise api.ApiUsageError("--wait requires --output")
    if args.output and not args.wait:
        raise api.ApiUsageError("--output requires --wait for generate")
    if args.idempotency_key is not None and not _IDEMPOTENCY_KEY.fullmatch(args.idempotency_key):
        raise api.ApiUsageError("--idempotency-key must be printable ASCII")
    if args.metadata_json is not None:
        try:
            metadata = json.loads(
                args.metadata_json,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise api.ApiUsageError("--metadata-json must be a JSON object") from exc
        if not isinstance(metadata, dict):
            raise api.ApiUsageError("--metadata-json must be a JSON object")
        args.metadata = metadata
    else:
        args.metadata = None
    for _kind, value in references:
        if isinstance(value, str) and "://" in value:
            _validate_reference_url(value)
        elif not Path(value).expanduser().is_file():
            raise api.ApiUsageError("reference media file does not exist")
    _validate_model_constraints(args, references)


def _validate_model_constraints(args, references):
    kind = _model_kind(args.model)
    duration = args.duration
    mode = args.reference_mode
    if kind == "kling":
        if not 3 <= duration <= 15 or args.aspect_ratio not in {"16:9", "9:16"}:
            raise api.ApiUsageError("Kling 3.0 requires 3-15 seconds and 16:9 or 9:16")
    elif kind == "veo":
        variant = _VEO.fullmatch(args.model).group(1)
        if duration not in {4, 6, 8}:
            raise api.ApiUsageError("Veo 3.1 duration must be 4, 6, or 8 seconds")
        if mode not in {None, "frame", "images"}:
            raise api.ApiUsageError("Veo 3.1 supports frame or standard images mode")
        if mode == "frame":
            if len(references) > 2 or any(item[0] != "image" for item in references):
                raise api.ApiUsageError("Veo 3.1 frame supports up to two images")
        elif mode == "images":
            if variant != "standard":
                raise api.ApiUsageError("Veo 3.1 fast supports frame references only")
            if not 1 <= len(references) <= 3 or any(item[0] != "image" for item in references):
                raise api.ApiUsageError("Veo 3.1 standard images require one to three images")
            if duration != 8 or args.aspect_ratio != "16:9":
                raise api.ApiUsageError("Veo 3.1 standard images require 8 seconds and 16:9")
        elif references:
            raise api.ApiUsageError("Veo 3.1 references require frame or standard images mode")
    elif kind == "adobe-seedance":
        if not 4 <= duration <= 15:
            raise api.ApiUsageError("Adobe Seedance 2.0 requires 4-15 seconds")
        if references and mode not in {"frame", "media"}:
            raise api.ApiUsageError("Adobe Seedance 2.0 supports frame or media references")
    elif kind == "leonardo-seedance-20":
        if not 4 <= duration <= 15:
            raise api.ApiUsageError("Leonardo Seedance 2.0 requires 4-15 seconds")
        if references and mode != "media":
            raise api.ApiUsageError("Leonardo Seedance 2.0 supports media references only")
    elif kind == "leonardo-seedance-25":
        if not 4 <= duration <= 30:
            raise api.ApiUsageError("Leonardo Seedance 2.5 requires 4-30 seconds")
        if references and mode not in {"frame", "media"}:
            raise api.ApiUsageError("Leonardo Seedance 2.5 supports frame or media references")
    elif kind == "h3":
        if not 5 <= duration <= 15:
            raise api.ApiUsageError("MiniMax H3 requires 5-15 seconds")
        if args.no_audio:
            raise api.ApiUsageError("MiniMax H3 always generates audio")
    if kind != "veo" and mode == "frame" and (len(references) != 1 or references[0][0] != "image"):
        raise api.ApiUsageError("frame reference mode requires exactly one image")
    if kind != "veo" and mode == "images" and (not references or any(item[0] != "image" for item in references)):
        raise api.ApiUsageError("images reference mode requires image references")


def build_task_payload(args, references) -> dict:
    _validate_generate_args(args)
    input_data = {"prompt": args.prompt}
    reference_mode = args.reference_mode
    is_veo = _model_kind(args.model) == "veo"
    if is_veo and not references and reference_mode is None:
        reference_mode = "frame"
    if references or (is_veo and reference_mode == "frame"):
        input_data["reference_mode"] = reference_mode
    for kind, singular, plural in (
        ("image", "image", "reference_images"),
        ("video", "video", "reference_videos"),
        ("audio", "audio", "reference_audios"),
    ):
        urls = [{"url": item["url"]} for item in references if item["kind"] == kind]
        if urls:
            if kind == "image" and reference_mode == "images":
                input_data[plural] = urls
            else:
                input_data[singular] = urls[0]
                if len(urls) > 1:
                    input_data[plural] = urls[1:]
    payload = {
        "model": args.model,
        "operation": "generation",
        "input": input_data,
        "output": {
            "duration": args.duration,
            "aspect_ratio": args.aspect_ratio,
            "generate_audio": not args.no_audio,
        },
    }
    if args.client_reference_id is not None:
        payload["client_reference_id"] = args.client_reference_id
    if args.metadata is not None:
        payload["metadata"] = args.metadata
    return payload


def _base_url(args):
    return normalize_base_url(args.base_url or DEFAULT_BASE_URL)


def _model_key(args):
    key = getattr(args, "_model_key", None) or get_model_key(args.api_key)
    args._model_key = key
    return key


def _resource_key(args):
    key = getattr(args, "_resource_key", None) or get_resource_key(args.resource_api_key)
    args._resource_key = key
    return key


def _summary_secrets(args):
    return tuple(
        value for value in (getattr(args, "_model_key", None), getattr(args, "_resource_key", None))
        if isinstance(value, str) and value
    )


def _parse_response(response, label):
    value = api.parse_json_response(response)
    if not isinstance(value, dict):
        raise api.ApiResponseError(f"{label} response was not an object")
    return value


def _upload_local_media(path, kind, args, resource_key):
    source = Path(path).expanduser()
    base_url = _base_url(args)
    prepared = _parse_response(api.request_json(
        "POST", api.endpoint_url(base_url, "/v1/media/uploads"), resource_key,
        HTTP_TIMEOUT, {"filename": source.name, "kind": kind},
    ), "media upload")
    upload_id = prepared.get("id")
    upload_url = prepared.get("upload_url")
    if not isinstance(upload_id, str) or not _TASK_ID.fullmatch(upload_id) or not isinstance(upload_url, str):
        raise api.ApiResponseError("media upload response was invalid")
    api.upload_media_files(upload_url, [source], HTTP_TIMEOUT)
    completed = _parse_response(api.request_json(
        "POST", api.endpoint_url(base_url, "/v1/media/uploads/complete"), resource_key,
        HTTP_TIMEOUT, {"id": upload_id},
    ), "media completion")
    url = completed.get("url")
    _validate_reference_url(url)
    return {"kind": kind, "url": url, "media_id": upload_id}


def _resolve_references(args):
    values = _reference_values(args)
    if not values:
        return []
    resource_key = None
    resolved = []
    for kind, value in values:
        if "://" in value:
            resolved.append({"kind": kind, "url": _validate_reference_url(value)})
            continue
        if resource_key is None:
            resource_key = _resource_key(args)
        resolved.append(_upload_local_media(value, kind, args, resource_key))
    return resolved


def _task_from_response(response, expected_id=None):
    task = _parse_response(response, "task")
    task_id = task.get("id")
    status = task.get("status")
    if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id) or not isinstance(status, str):
        raise api.ApiResponseError("task response was invalid")
    progress = task.get("progress")
    if progress is not None and (
        not isinstance(progress, (int, float))
        or isinstance(progress, bool)
        or not math.isfinite(progress)
    ):
        raise api.ApiResponseError("task response progress was invalid")
    if expected_id is not None and task_id != expected_id:
        raise api.ApiResponseError("task response ID did not match the requested task")
    return task


def create_task(args) -> dict:
    _validate_generate_args(args)
    model_key = _model_key(args)
    if args.wait:
        _resource_key(args)
    references = _resolve_references(args)
    payload = build_task_payload(args, references)
    idempotency_key = args.idempotency_key or uuid.uuid4().hex
    args._idempotency_key = idempotency_key
    response = api.request_json(
        "POST", api.endpoint_url(_base_url(args), "/v1/video/tasks"),
        model_key, HTTP_TIMEOUT, payload,
        {"Idempotency-Key": idempotency_key},
    )
    return _task_from_response(response)


def _request_timeout(args):
    deadline = getattr(args, "_deadline", None)
    if deadline is None:
        return HTTP_TIMEOUT
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise api.ApiResponseError("task wait timeout exceeded")
    return min(HTTP_TIMEOUT, remaining)


def read_task(task_id, args) -> dict:
    task_id = _validate_task_id(task_id)
    response = api.request_json(
        "GET", api.endpoint_url(_base_url(args), f"/v1/video/tasks/{task_id}"),
        _resource_key(args),
        _request_timeout(args),
    )
    args._last_response_headers = response.headers
    return _task_from_response(response, task_id)


def _poll_delay(headers, fallback):
    value = api.header_value(headers, "Retry-After", fallback)
    try:
        return max(MIN_POLL_DELAY, float(value))
    except (TypeError, ValueError):
        return max(MIN_POLL_DELAY, fallback)


def _result_videos(task):
    result = task.get("result")
    videos = result.get("videos") if isinstance(result, dict) else None
    if not isinstance(videos, list) or not videos:
        raise api.ApiResponseError("succeeded task did not contain videos")
    return videos


def wait_for_task(task_id, args) -> dict:
    task_id = _validate_task_id(task_id)
    wait_timeout = getattr(args, "wait_timeout", WAIT_TIMEOUT)
    if (
        not isinstance(wait_timeout, (int, float))
        or isinstance(wait_timeout, bool)
        or not math.isfinite(wait_timeout)
        or wait_timeout <= 0
    ):
        raise api.ApiUsageError("--wait-timeout must be positive")
    args._deadline = time.monotonic() + wait_timeout
    delay = MIN_POLL_DELAY
    while True:
        task = read_task(task_id, args)
        status = task["status"]
        if status == "succeeded":
            resource_key = _resource_key(args)
            remaining = _request_timeout(args)
            output = Path(args.output).expanduser()
            saved = api.download_video_items(
                _result_videos(task), output.parent, remaining, resource_key,
                output_path=output, deadline=args._deadline, monotonic=time.monotonic,
            )
            return {"task": task, "outputs": saved}
        if status == "failed":
            raise api.ApiResponseError("video task failed")
        if status not in {"queued", "in_progress"}:
            raise api.ApiResponseError("video task returned an unknown status")
        delay = _poll_delay(getattr(args, "_last_response_headers", {}), delay)
        remaining = args._deadline - time.monotonic()
        if remaining <= 0:
            raise api.ApiResponseError("task wait timeout exceeded")
        time.sleep(min(delay, remaining))


def _task_summary(task, *secrets):
    result = {
        "task_id": api.sanitize_diagnostic(task["id"], *secrets),
        "status": api.sanitize_diagnostic(task["status"], *secrets),
    }
    if isinstance(task.get("progress"), (int, float)):
        result["progress"] = task["progress"]
    return result


def _run_models(args):
    model_key = _model_key(args)
    response = api.request_json(
        "GET", api.endpoint_url(_base_url(args), "/v1/models"),
        model_key, HTTP_TIMEOUT,
    )
    data = _parse_response(response, "models")
    items = data.get("data")
    if not isinstance(items, list):
        raise api.ApiResponseError("models response was invalid")
    models = [item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if not args.all:
        models = [model for model in models if _model_kind(model) is not None]
    print(json.dumps({"models": [api.sanitize_diagnostic(model, model_key) for model in models]}, ensure_ascii=True))


def _run_upload(args):
    source = Path(args.file).expanduser()
    if not source.is_file():
        raise api.ApiUsageError("media file does not exist")
    resource_key = _resource_key(args)
    item = _upload_local_media(source, args.kind, args, resource_key)
    print(json.dumps({"kind": args.kind, "media_id": api.sanitize_diagnostic(item["media_id"], resource_key)}, ensure_ascii=True))


def main(argv=None) -> int:
    try:
        args = parse_args(argv)
    except api.ApiUsageError as exc:
        print(api.sanitize_diagnostic(str(exc)), file=sys.stderr)
        return 2
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    secrets = [value for value in (getattr(args, "api_key", None), getattr(args, "resource_api_key", None)) if isinstance(value, str)]
    try:
        if args.command == "models":
            _run_models(args)
        elif args.command == "upload":
            _run_upload(args)
        elif args.command == "generate":
            task = create_task(args)
            summary = _task_summary(task, *_summary_secrets(args))
            summary["idempotency_key"] = api.sanitize_diagnostic(args._idempotency_key, *_summary_secrets(args))
            if args.wait:
                waited = wait_for_task(task["id"], args)
                summary.update(_task_summary(waited["task"], *_summary_secrets(args)))
                summary["outputs"] = [{"path": api.sanitize_diagnostic(item["path"], *_summary_secrets(args))} for item in waited["outputs"]]
            print(json.dumps(summary, ensure_ascii=True))
        elif args.command == "task":
            print(json.dumps(_task_summary(read_task(args.task_id, args), *_summary_secrets(args)), ensure_ascii=True))
        elif args.command == "wait":
            waited = wait_for_task(args.task_id, args)
            summary = _task_summary(waited["task"], *_summary_secrets(args))
            summary["outputs"] = [{"path": api.sanitize_diagnostic(item["path"], *_summary_secrets(args))} for item in waited["outputs"]]
            print(json.dumps(summary, ensure_ascii=True))
        return 0
    except (ConfigError, api.ApiUsageError, api.ApiResponseError, ValueError) as exc:
        print(api.sanitize_diagnostic(str(exc), *secrets), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
