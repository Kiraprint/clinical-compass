#!/usr/bin/env bash
# Локальный LLM для роутера раздела БЗ ассистента врача (docs/04 FR-17, docs/08).
# Поднимает llama.cpp server (OpenAI-совместимый /v1) с Qwen3.8-27B на RTX 5090 —
# весь трафик локальный, ПДн наружу не уходит (роутер получает только очищенный запрос).
#
# Требования (на хосте):
#   - собранный llama-server: $LLAMA_SERVER (по умолчанию
#     /home/kir/Code/LLM/llama.cpp-pr27742/build/bin/llama-server — сборка с
#     поддержкой qwen4exp/MTP); старый ollama-bundle qwen4exp НЕ читает;
#   - модель: $LLM_MODEL (по умолчанию Qwen3.8-27B-NVFP4-MTP-Q8attn.gguf).
#
# Использование:
#   ./tools/llm.sh           — запустить сервер (порт 18080) и вывести status
#   ./tools/llm.sh status    — показать состояние
#   ./tools/llm.sh --stop    — остановить
#
# После старта сервера впиши в infra/.env:
#   CC_ASSISTANT_LLM_URL=http://host.docker.internal:18080/v1
# и перезапусти api: docker compose -f infra/docker-compose.yml up -d api.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${LLAMA_SERVER:-/home/kir/Code/LLM/llama.cpp-pr27742/build/bin/llama-server}"
MODEL="${LLM_MODEL:-/home/kir/models/Qwen3.8-27B-NVFP4-MTP-Q8attn-GGUF/Qwen3.8-27B-NVFP4-MTP-Q8attn.gguf}"
PORT="${LLM_PORT:-18080}"
PIDFILE="$ROOT/tools/.llm.pid"
LOGFILE="$ROOT/tools/.llm.log"

start() {
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "LLM уже запущен (pid $(cat "$PIDFILE"), порт $PORT)"
    return 0
  fi
  if [[ ! -x "$BIN" ]]; then
    echo "⚠️  Нет llama-server: $BIN" >&2
    echo "    Собери: cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=120 && cmake --build build -j14 --target llama-server" >&2
    exit 1
  fi
  if [[ ! -f "$MODEL" ]]; then
    echo "⚠️  Нет модели: $MODEL" >&2
    exit 1
  fi
  "$BIN" -m "$MODEL" --host 127.0.0.1 --port "$PORT" -ngl 99 --jinja > "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  # Ждём health (модель 27B грузится ~30-60 с).
  for _ in $(seq 1 60); do
    if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null; then
      echo "LLM готов: http://127.0.0.1:$PORT/v1 (model=$MODEL)"
      echo "Затем: CC_ASSISTANT_LLM_URL=http://host.docker.internal:$PORT/v1 в infra/.env и рестарт api"
      return 0
    fi
    sleep 2
  done
  echo "⚠️  LLM не поднялся за 120 с — смотри $LOGFILE" >&2
  exit 1
}

status() {
  if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "LLM запущен (pid $(cat "$PIDFILE")): $(curl -s http://127.0.0.1:$PORT/health 2>/dev/null | head -c 40)"
  else
    echo "LLM остановлен"
    exit 1
  fi
}

stop() {
  if [[ -f "$PIDFILE" ]]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"
    echo "LLM остановлен"
  else
    echo "LLM не запущен"
  fi
}

case "${1:-start}" in
  start) start ;;
  status) status ;;
  --stop | stop) stop ;;
  *) echo "usage: $0 [start|status|--stop]" >&2; exit 2 ;;
esac