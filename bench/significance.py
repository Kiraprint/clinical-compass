"""Статистика e5 vs bge-m3 на расширенном наборе: per-query nDCG@10 и парный тест.

Печатает средние, разницу, 95% bootstrap-CI разницы и Wilcoxon signed-rank.
"""
import sys, os, json, random, math
sys.path.insert(0, "/home/kir/Code/clinical-compass/bench")
os.chdir("/home/kir/Code/clinical-compass/bench")
import run_bench as rb

corpus = [json.loads(l) for l in open("data/corpus.jsonl", encoding="utf-8")]
queries = [json.loads(l) for l in open("data/queries.jsonl", encoding="utf-8")]
rb.DOC_IDS = [d["id"] for d in corpus]
texts = [d["text"] for d in corpus]


def perquery_dense(emb):
    de = emb.embed(texts)
    qe = emb.embed_queries([q["q"] for q in queries])
    out = []
    for i, q in enumerate(queries):
        ranked = rb.retrieve(de, qe[i], None, None, variant="dense")
        out.append(rb.ndcg_at(ranked, q["qrels"], 10))
    return out


e5 = perquery_dense(rb.STEmbedder("intfloat/multilingual-e5-large",
                                  prefix_query="query: ", prefix_doc="passage: "))
bge = perquery_dense(rb.BgeM3Embedder())

n = len(queries)
d = [a - b for a, b in zip(e5, bge)]
mean = sum(d) / n
# bootstrap 95% CI разницы
random.seed(42)
boot = []
for _ in range(5000):
    s = [d[random.randrange(n)] for _ in range(n)]
    boot.append(sum(s) / n)
boot.sort()
lo, hi = boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot))]

# Wilcoxon signed-rank (двусторонний, нормальное приближение)
nz = [x for x in d if abs(x) > 1e-12]
ranks = sorted(range(len(nz)), key=lambda i: abs(nz[i]))
rpos = rneg = 0.0
for rank, idx in enumerate(ranks, 1):
    if nz[idx] > 0:
        rpos += rank
    else:
        rneg += rank
nn = len(nz)
mu = nn * (nn + 1) / 4
sigma = math.sqrt(nn * (nn + 1) * (2 * nn + 1) / 24)
z = (min(rpos, rneg) - mu) / sigma if sigma else 0.0
# двусторонний p по нормальному приближению
p = 2 * 0.5 * (1 + math.erf(abs(z) / math.sqrt(2)))
p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))

wins = sum(1 for x in d if x > 1e-12)
loss = sum(1 for x in d if x < -1e-12)
ties = n - wins - loss
print(f"queries={n}")
print(f"e5  dense mean nDCG@10 = {sum(e5)/n:.4f}")
print(f"bge dense mean nDCG@10 = {sum(bge)/n:.4f}")
print(f"diff (e5-bge) mean = {mean:+.4f}  95% CI [{lo:+.4f}, {hi:+.4f}]")
print(f"pairwise: e5 wins={wins} losses={loss} ties={ties}")
print(f"Wilcoxon signed-rank: W+={rpos:.0f} W-={rneg:.0f} z={z:.2f} p={p:.4f}")
