import time,psutil,numpy as np
from semlib import CATEGORIES, Embedder, event_texts, load_sample, metrics, prototypes  # noqa: F401
from model2vec import StaticModel
proc=psutil.Process(); t0=time.perf_counter()
m=StaticModel.from_pretrained("minishlab/potion-base-8M")
init=time.perf_counter()-t0
class E:
    def embed(self,texts,batch=0):
        v=np.asarray(m.encode(list(texts)),dtype=np.float32); return v/np.maximum(np.linalg.norm(v,axis=1,keepdims=True),1e-9)
emb=E()
sample=load_sample(); CAL=[e for e in sample if e["n"]<40]; TEST=[e for e in sample if e["n"]>=40]
cats,protos,groups=prototypes(emb,False)
titles,fulls=zip(*[event_texts(e) for e in sample]); ef=emb.embed(list(fulls)); et=emb.embed(list(titles))
variants={"full":ef@protos.T,"full_max":np.stack([(ef@groups[c].T).max(1) for c in cats],1),"title":et@protos.T}
def decide(sim,th,mg):
    o=np.argsort(-sim); return cats[o[0]] if sim[o[0]]>=th and sim[o[0]]-sim[o[1]]>=mg else "general"
best=None
for v,mat in variants.items():
    for th in np.linspace(float(mat.min()),float(mat.max()),20):
        for mg in (0,0.01,0.02,0.04):
            pc=[decide(mat[e["n"]],th,mg) for e in CAL]; acc=np.mean([p==e["label"] for p,e in zip(pc,CAL)]); wrong=sum(1 for p,e in zip(pc,CAL) if p!="general" and p!=e["label"])
            sc=acc-0.01*wrong
            if best is None or sc>best[0]: best=(sc,v,th,mg)
_,v,th,mg=best; mat=variants[v]
mt=metrics([decide(mat[e["n"]],th,mg) for e in TEST],[e["label"] for e in TEST]); mc=metrics([decide(mat[e["n"]],th,mg) for e in CAL],[e["label"] for e in CAL])
print(f"potion-base-8M variant={v} th={th:.3f} mg={mg} | cal acc {mc['accuracy']:.3f} | TEST acc {mt['accuracy']:.3f} wrong-specific {mt['wrong_specific']} wrongly-general {mt['wrongly_general']} general {mt['general_rate']:.2f}")
texts=(list(fulls)*5)[:500]
t0=time.perf_counter(); emb.embed(texts); dt=time.perf_counter()-t0
print(f"init {init:.2f}s; 500 events {dt:.2f}s; rss {proc.memory_info().rss/1e6:.0f} MB peak {proc.memory_info().peak_wset/1e6:.0f} MB; model file ~30 MB")
