"""Попарный dense-разбор e5-multilingual vs bge-m3: победы/потери по запросам."""
import sys, os
sys.path.insert(0, "/home/kir/Code/clinical-compass/bench")
os.chdir("/home/kir/Code/clinical-compass/bench")
import json
import run_bench as rb

def ndcg(ranked, qrels, k=10):
    return rb.ndcg_at(ranked, qrels, k)

corpus = [json.loads(l) for l in open(rb.DATA + "/corpus.jsonl", encoding="utf-8")]
queries = [json.loads(l) for l in open(rb.DATA + "/queries.jsonl", encoding="utf-8")]
rb.DOC_IDS = [d["id"] for d in corpus]
texts = [d["text"] for d in corpus]

models = {"e5": rb.STEmbedder("intfloat/multilingual-e5-large", prefix_query="query: ", prefix_doc="passage: "),
          "bge": rb.BgeM3Embedder()}
res = {}
for nm, m in models.items():
    de = m.embed(texts)
    qe = m.embed_queries([q["q"] for q in queries])
    res[nm] = [ndcg(rb.retrieve(de, qe[i], None, None, variant="dense"), q["qrels"]) for i, q in enumerate(queries)]

import statistics
e5, bge = res["e5"], res["bge"]
w = t = l = 0
for x, y in zip(e5, bge):
    if abs(x - y) < 1e-9:
        t += 1
    elif x > y:
        w += 1
    elif y > x:
        l += 1
print(f"poparno: e5 wins={w} ties={t} bge wins={l}")
print(f"e5 mean={statistics.mean(e5):.3f}  bge mean={statistics.mean(bge):.3f}")
print(f"e5>=bge в {sum(1 for x,y in zip(e5,bge) if x>y)} из {len(e5)} запросов")
# запросы, где расходятся сильно
for i, (q, x, y) in enumerate(zip(queries, e5, bge)):
    if abs(x - y) >= 0.15:
        print(f"  q{i} {q['id']}: e5={x:.2f} bge={y:.2f}")
