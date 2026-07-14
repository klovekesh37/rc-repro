"""Dynamic `s3_minio` preset: a MinIO S3-compatible object store wired to
Rocket.Chat's file upload storage.

Reproduces external-object-storage tickets: uploads failing, broken previews,
bucket/path-style/signature misconfigurations and — the classic — presigned URLs
the browser can't reach.

THE GOTCHA (same class as the oidc preset): the S3 endpoint URL is used by RC's
backend (uploads) AND, in presigned mode, by the browser (downloads). The
default mode sidesteps it by proxying downloads through RC
(FileUpload_S3_Proxy_*=true) — zero setup, works out of the box.
`--set presigned=true` switches to real presigned MinIO URLs, which needs the
hosts entry `127.0.0.1  minio` (printed on `up`) — and IS the faithful repro
for presigned-URL tickets: remove the hosts line and previews break exactly
like the customer's.

Uploaded objects live in the `minio_data` named volume (via Preset.volumes), so
files survive `down`/`up` just like Mongo data — no dangling attachment cards
whose objects vanished.

Parameters (via `--set`):
  presigned   serve downloads via presigned MinIO URLs instead of proxying
              through RC (default false; needs the /etc/hosts line).
  bucket      bucket name (default rcrepro-uploads).
"""

from __future__ import annotations

from rc_repro.presets import Preset

_S3_PORT = 9000       # S3 API — same inside and published, so presigned URLs are
_CONSOLE_PORT = 9001  # valid from the browser (via /etc/hosts) and RC alike
_USER = "rcrepro"
_PASSWORD = "rcrepro-secret"   # MinIO requires >= 8 chars; throwaway repro creds
# Pinned multi-arch (amd64/arm64) tags — verified via docker manifest inspect.
_MINIO_TAG = "RELEASE.2025-09-07T16-13-09Z"
_MC_TAG = "RELEASE.2025-08-13T08-35-41Z"


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def build(params: dict) -> Preset:
    presigned = _truthy(params.get("presigned", False))
    bucket = str(params.get("bucket", "rcrepro-uploads") or "rcrepro-uploads")

    services = {
        "minio": {
            "image": f"docker.io/minio/minio:{_MINIO_TAG}",
            "restart": "unless-stopped",
            "command": ["server", "/data", "--console-address", f":{_CONSOLE_PORT}"],
            "environment": {
                "MINIO_ROOT_USER": _USER,
                "MINIO_ROOT_PASSWORD": _PASSWORD,
            },
            "volumes": ["minio_data:/data"],
            "ports": [f"{_S3_PORT}:{_S3_PORT}", f"{_CONSOLE_PORT}:{_CONSOLE_PORT}"],
        },
        # One-shot: wait for MinIO to accept connections, then create the
        # bucket. `mc mb -p` is idempotent (no error if it already exists).
        "minio-init": {
            "image": f"docker.io/minio/mc:{_MC_TAG}",
            "restart": "no",
            "depends_on": ["minio"],
            "entrypoint": [
                "sh", "-c",
                f"until mc alias set local http://minio:{_S3_PORT} {_USER} {_PASSWORD}; "
                f"do sleep 1; done; mc mb -p local/{bucket}",
            ],
        },
    }

    env = {
        "OVERWRITE_SETTING_FileUpload_Storage_Type": "AmazonS3",
        "OVERWRITE_SETTING_FileUpload_S3_Bucket": bucket,
        "OVERWRITE_SETTING_FileUpload_S3_BucketURL": f"http://minio:{_S3_PORT}/{bucket}",
        "OVERWRITE_SETTING_FileUpload_S3_AWSAccessKeyId": _USER,
        "OVERWRITE_SETTING_FileUpload_S3_AWSSecretAccessKey": _PASSWORD,
        "OVERWRITE_SETTING_FileUpload_S3_Region": "us-east-1",   # MinIO default
        "OVERWRITE_SETTING_FileUpload_S3_ForcePathStyle": "true",  # mandatory for MinIO
        "OVERWRITE_SETTING_FileUpload_S3_SignatureVersion": "v4",
        # Default: stream files through RC so the browser never needs to reach
        # MinIO. Presigned mode: real presigned URLs (the faithful flow).
        "OVERWRITE_SETTING_FileUpload_S3_Proxy_Uploads": "false" if presigned else "true",
        "OVERWRITE_SETTING_FileUpload_S3_Proxy_Avatars": "false" if presigned else "true",
    }

    notes = [
        f"MinIO console: http://localhost:{_CONSOLE_PORT}  ({_USER} / {_PASSWORD})",
        f"  — bucket '{bucket}' is created automatically; watch uploads land in it.",
        "Upload any file/image in RC; it goes to MinIO instead of GridFS.",
    ]
    if presigned:
        notes += [
            "PRESIGNED MODE: the browser fetches files straight from MinIO, so add",
            "this line to /etc/hosts (needs sudo):",
            "    127.0.0.1  minio",
            "Remove the line to reproduce the classic 'presigned URL unreachable'",
            "ticket symptom (uploads work, previews/downloads break).",
        ]
    else:
        notes += [
            "Downloads are proxied through RC (no hosts entry needed). For real",
            "presigned-URL behaviour: --set presigned=true (reproduces that ticket class).",
        ]

    return Preset(
        name="s3_minio",
        description=(
            f"MinIO S3-compatible storage wired as RC's file upload backend "
            f"(bucket '{bucket}', console :{_CONSOLE_PORT}). Default proxies "
            "downloads through RC; --set presigned=true for real presigned URLs."
        ),
        env=env,
        services=services,
        depends_on=["minio"],
        requires_license=False,
        source="built-in (dynamic)",
        params_help={
            "presigned": "browser fetches presigned MinIO URLs (default false; needs /etc/hosts line)",
            "bucket": "bucket name (default rcrepro-uploads)",
        },
        volumes={"minio_data": {"driver": "local"}},
        notes=notes,
    )
