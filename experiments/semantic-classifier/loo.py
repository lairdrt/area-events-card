import numpy as np,collections,math
from semlib import CATEGORIES, Embedder, event_texts, load_sample, metrics, prototypes  # noqa: F401
from discovery import classify
sample=load_sample()
emb=Embedder("models/sentence-transformers__all-MiniLM-L6-v2/onnx/model.onnx","models/sentence-transformers__all-MiniLM-L6-v2/tokenizer.json","mean")
fulls=[event_texts(e)[1] for e in sample]; X=emb.embed(fulls); y=[e["label"] for e in sample]
S=X@X.T; np.fill_diagonal(S,-1)
for k in (1,3,5):
    pred=[]
    for i in range(len(y)):
        idx=np.argsort(-S[i])[:k]; c=collections.Counter()
        for j in idx: c[y[j]]+=S[i][j]
        pred.append(c.most_common(1)[0][0])
    print(f"leave-one-out {k}-NN on 120 labelled events: accuracy {np.mean([p==g for p,g in zip(pred,y)]):.3f}")
rp=[classify.classify({"title":e["title"],"description":e["description"],"categories":e["categories"],"venue":e["venue"]})["category"] for e in sample]
acc=np.mean([p==g for p,g in zip(rp,y)]); n=len(y)
z=1.96; lo=(acc+z*z/(2*n)-z*math.sqrt(acc*(1-acc)/n+z*z/(4*n*n)))/(1+z*z/n); hi=(acc+z*z/(2*n)+z*math.sqrt(acc*(1-acc)/n+z*z/(4*n*n)))/(1+z*z/n)
print(f"rules on all 120: accuracy {acc:.3f} (95% Wilson interval {lo:.2f}-{hi:.2f}); majority baseline 'always general' {np.mean([g=='general' for g in y]):.3f}")
by=collections.Counter(e["basis"] for e in sample)
for b in ("L","J","I"):
    sel=[i for i,e in enumerate(sample) if e["basis"]==b]
    print(f"  rules accuracy where label basis={b} ({len(sel)}): {np.mean([rp[i]==y[i] for i in sel]):.2f}")
