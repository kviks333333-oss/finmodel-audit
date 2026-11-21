#!/usr/bin/env bash
# Build only an allowlisted ASCII-path context; never copy credentials or models.
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
build_only=false
skip_build=false
case "${1:-}" in
  "") ;;
  --build-only) build_only=true ;;
  --no-build) skip_build=true ;;
  *) echo "Usage: scripts/start.sh [--build-only|--no-build]" >&2; exit 2 ;;
esac
if [[ ! -f "$project_dir/.env" ]]; then
  echo "Create the private .env file from .env.example before starting." >&2
  exit 2
fi
docker info >/dev/null
docker compose --env-file "$project_dir/.env" -f "$project_dir/compose.yaml" --project-directory "$project_dir" config --quiet

if [[ "$skip_build" == false ]]; then
  context_dir="$(mktemp -d /tmp/finmodel-build.XXXXXXXX)"
  trap 'rm -rf -- "$context_dir"' EXIT
  python3 - "$project_dir" "$context_dir" <<'PY'
from pathlib import Path
import hashlib
import shutil
import sys

source, destination = map(Path, sys.argv[1:])
paths = [Path(name) for name in (
    'Dockerfile', '.dockerignore', 'pyproject.toml', 'requirements.lock',
    'frontend/package.json', 'frontend/package-lock.json',
    'frontend/index.html', 'frontend/vite.config.js',
    'pptx_runtime/package.json', 'pptx_runtime/package-lock.json', 'pptx_runtime/render.cjs',
)]
paths += [p.relative_to(source) for p in (source / 'src').rglob('*.py') if '__pycache__' not in p.parts]
paths += [p.relative_to(source) for p in (source / 'frontend/src').rglob('*') if p.is_file()]
paths += [p.relative_to(source) for p in (source / 'certs').glob('*.crt')]
fingerprint = hashlib.sha256()
for relative in sorted(set(paths)):
    origin = source / relative
    ancestors = [source.joinpath(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1)]
    if (any(path.is_symlink() for path in ancestors)
            or not origin.resolve().is_relative_to(source.resolve())
            or any(part.startswith('.env') or part == 'node_modules' for part in relative.parts)):
        raise SystemExit('Unexpected file in the build allowlist; build cancelled')
    content = origin.read_bytes()
    fingerprint.update(str(relative).encode('utf-8') + b'\0' + content + b'\0')
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(origin, target)
(destination / 'BUILD_FINGERPRINT').write_text(fingerprint.hexdigest(), encoding='ascii')
print('Build source SHA-256: ' + fingerprint.hexdigest(), flush=True)
PY
  build_fingerprint="$(cat "$context_dir/BUILD_FINGERPRINT")"
  rm "$context_dir/BUILD_FINGERPRINT"
  (
    cd "$context_dir"
    docker build --tag finmodel-audit:0.2.0 --build-arg "BUILD_SOURCE_SHA256=$build_fingerprint" .
  )
fi

if [[ "$build_only" == true ]]; then
  echo "Image built. Start with scripts/start.sh --no-build."
  exit 0
fi
(
  cd /tmp
  docker compose --env-file "$project_dir/.env" -f "$project_dir/compose.yaml" --project-directory "$project_dir" up -d --no-build
)
echo "Service: http://127.0.0.1:8000"
echo "Status: docker compose ps"
