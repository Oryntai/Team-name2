#!/usr/bin/env bash
# Своя модель-судья локально: OpenAI-совместимый llama-server на http://127.0.0.1:8081 для провайдера `local`.
#
#   bash lab/local/serve.sh            # Qwen3.5-9B Q4 + наш LoRA-адаптер (если он есть в lab/local/adapters/)
#   bash lab/local/serve.sh base       # Qwen3.5-9B Q4 как есть, без дообучения
#   bash lab/local/serve.sh gemma      # Gemma 4 12B (QAT Q4) — второй кандидат из замера
#
# Нужно: llama.cpp (brew install llama.cpp), ~6–7 ГБ свободной памяти. Модель скачивается один раз в ~/models/cqa.
# Продукт: CQA_LOCAL_BASE_URL=http://127.0.0.1:8081 CQA_LOCAL_MODEL=<alias> (в .env или в окружении).
set -euo pipefail

MODE="${1:-tuned}"
DIR="${CQA_MODELS_DIR:-$HOME/models/cqa}"
PORT="${CQA_LOCAL_PORT:-8081}"
SLOTS="${CQA_LOCAL_SLOTS:-4}"          # параллельных диалогов; контекст на слот — 12k токенов
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ADAPTER="${CQA_LORA:-$ROOT/lab/local/adapters/qwen35-cqa/adapter.gguf}"

case "$MODE" in
  tuned|base)
    FILE="Qwen3.5-9B-Q4_K_M.gguf"; URL="https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/resolve/main/$FILE"
    ALIAS="qwen3.5-9b"; [ "$MODE" = tuned ] && ALIAS="qwen3.5-9b-cqa" ;;
  gemma)
    FILE="gemma-4-12b-it-qat-q4_0.gguf"; URL="https://huggingface.co/google/gemma-4-12B-it-qat-q4_0-gguf/resolve/main/$FILE"
    ALIAS="gemma-4-12b" ;;
  *) echo "режим: tuned | base | gemma"; exit 1 ;;
esac

command -v llama-server >/dev/null || { echo "нет llama-server: brew install llama.cpp"; exit 1; }
mkdir -p "$DIR"
if [ ! -s "$DIR/$FILE" ]; then
  echo "скачиваю $FILE в $DIR (один раз, 5–7 ГБ)…"
  curl -L --fail -C - -o "$DIR/$FILE.part" "$URL" && mv "$DIR/$FILE.part" "$DIR/$FILE"
fi

LORA=()
if [ "$MODE" = tuned ]; then
  if [ -s "$ADAPTER" ]; then LORA=(--lora "$ADAPTER"); else
    echo "адаптер не найден ($ADAPTER) — запускаю базовую модель"; ALIAS="qwen3.5-9b"; fi
fi

echo "модель: $ALIAS → http://127.0.0.1:$PORT  (CQA_LOCAL_BASE_URL=http://127.0.0.1:$PORT CQA_LOCAL_MODEL=$ALIAS)"
exec llama-server -m "$DIR/$FILE" ${LORA[@]+"${LORA[@]}"} --alias "$ALIAS" --host 127.0.0.1 --port "$PORT" \
  -ngl 99 -c $((SLOTS * 12288)) -np "$SLOTS" --reasoning off --no-webui
