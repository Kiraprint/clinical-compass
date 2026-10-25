#!/usr/bin/env bash
# Полное воспроизведение матрицы из bench/REPORT-retrieval.md одной командой.
#
#   bash bench/run_retrieval_suite.sh            # всё: разметка → QA → матрица → отчёт
#   bash bench/run_retrieval_suite.sh --matrix   # без разметки (qrels уже есть)
#
# Требует: Postgres/Qdrant не нужны (матрица считается in-memory по снимку
# корпуса), GPU с ~16 ГБ для qwen3-4b (можно исключить --no-heavy).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
JUDGE_PY=${JUDGE_PY:-../bench/.venv/bin/python}
MATRIX_ONLY=0
HEAVY=1
for a in "$@"; do
  case "$a" in
    --matrix) MATRIX_ONLY=1 ;;
    --no-heavy) HEAVY=0 ;;
  esac
done

if [ ! -f bench/data/kb_corpus.jsonl ] || [ ! -f bench/data/kb_queries.jsonl ]; then
  echo "[1/5] снимок корпуса из Qdrant + набор запросов"
  $PY bench/build_kb_bench.py --export
fi

if [ "$MATRIX_ONLY" -eq 0 ]; then
  echo "[2/5] пул кандидатов (объединение top-20 всех стеков)"
  $PY bench/eval_retrieval.py --stacks all --dump-pools bench/data/kb_pool_merged.jsonl >/dev/null
  echo "[3/5] разметка локальным судьёй (батчи; докачивается)"
  $JUDGE_PY bench/grade_local.py --pool bench/data/kb_pool_merged.jsonl --out bench/data/qrels.jsonl
fi

echo "[4/5] проверки золотого набора"
$PY bench/qa_qrels.py --qrels bench/data/qrels.jsonl

echo "[5/5] матрица и таблица"
STACKS="hash-dense+sparse,bge-m3-dense,bge-m3-dense+sparse,bge-m3-dense+sparse-k10,bge-m3-dense+sparse-k30,bge-m3-dense+sparse-k100,bge-m3-dense+sparse+colbert,user-bge-m3-dense,e5-large-instruct-dense,qwen3-0.6b-dense,frida-dense,bge-m3-dense+rerank-bge-d20,bge-m3-dense+rerank-bge-d50,bge-m3-dense+rerank-bge-d100,qwen3-0.6b-dense+rerank-bge-d20,qwen3-0.6b-dense+rerank-bge-d50,qwen3-0.6b-dense+rerank-bge-d100,bge-m3-dense+rerank-jina-d20,bge-m3-dense+rerank-jina-d50,bge-m3-dense+rerank-jina-d100,qwen3-0.6b-dense+rerank-jina-d50,bge-m3-dense+rerank-qwen3-d20,bge-m3-dense+rerank-qwen3-d50,bge-m3-dense+rerank-qwen3-d100,qwen3-0.6b-dense+rerank-qwen3-d50"
$PY bench/eval_retrieval.py --stacks "$STACKS" --qrels bench/data/qrels.jsonl \
  --json-out bench/data/eval_suite.json
if [ "$HEAVY" -eq 1 ]; then
  # qwen3-4b не помещается рядом с остальными моделями — отдельным процессом
  $PY bench/eval_retrieval.py --stacks qwen3-4b-dense --qrels bench/data/qrels.jsonl \
    --json-out bench/data/eval_qwen3_4b.json
fi
$PY bench/report_table.py bench/data/eval_suite.json bench/data/eval_qwen3_4b.json \
  --qrels bench/data/qrels.jsonl | tee bench/data/table.md
