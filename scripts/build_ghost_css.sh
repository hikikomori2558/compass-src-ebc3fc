#!/bin/sh
# scripts/build_ghost_css.sh — пересборка CSS дизайн-системы Ghost (Shade)
# для МИС-стенда. Один раз клонировать репо (источник вендоренных файлов):
#   git clone --depth 1 --filter=blob:none --sparse https://github.com/TryGhost/Ghost.git /tmp/ghost-design
#   (cd /tmp/ghost-design && git sparse-checkout set --skip-checks apps/shade apps/admin/src)
# Затем:
#   mkdir -p /tmp/shade-build && cd /tmp/shade-build
#   npm init -y && npm i -D tailwindcss @tailwindcss/cli tw-animate-css
#   ln -sfn "$(pwd)"-link # (см. ниже — линк на репо)
# Сборка (Tailwind v4 CLI, их пайплайн — apps/shade/styles.css):
set -e
BUILD_DIR="${SHADE_BUILD_DIR:-/tmp/shade-build}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -d "$BUILD_DIR/node_modules/@tailwindcss/cli" ]; then
  echo "[build_ghost_css] нет $BUILD_DIR/node_modules — см. комментарий в шапке" >&2
  exit 1
fi
ln -sfn "$REPO/frontend_mis_mock" "$BUILD_DIR/frontend_mis_mock"
cd "$BUILD_DIR"
npx @tailwindcss/cli -i frontend_mis_mock/ghost-shade.css \
  -o frontend_mis_mock/vendor/ghost/shade.compiled.css
echo "[build_ghost_css] OK -> frontend_mis_mock/vendor/ghost/shade.compiled.css"
