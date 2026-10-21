import collections,numpy as np
from semlib import CATEGORIES, Embedder, event_texts, load_sample, metrics, prototypes  # noqa: F401
from discovery import classify
sample=load_sample(); TEST=[e for e in sample if e["n"]>=40]; CAL=[e for e in sample if e["n"]<40]
emb=Embedder("models/sentence-transformers__all-MiniLM-L6-v2/onnx/model.onnx","models/sentence-transformers__all-MiniLM-L6-v2/tokenizer.json","mean")
cats,protos,groups=prototypes(emb,False)
titles,fulls=zip(*[event_texts(e) for e in sample]); et,ef=emb.embed(list(titles)),emb.embed(list(fulls))
mat=np.stack([(ef@groups[c].T).max(1) for c in cats],1)
TH,MG=0.264,0.02
def sem(i):
    o=np.argsort(-mat[i]); return cats[o[0]] if mat[i][o[0]]>=TH and mat[i][o[0]]-mat[i][o[1]]>=MG else "general"
rules={e["n"]:classify.classify({"title":e["title"],"description":e["description"],"categories":e["categories"],"venue":e["venue"]}) for e in sample}
def hyb(i): r=rules[i]; return sem(i) if r["fallback"] else r["category"]
gold={e["n"]:e["label"] for e in sample}
title={e["n"]:e["title"][:52] for e in sample}
def table(name,pred,es):
    tp=collections.Counter(); fp=collections.Counter(); fn=collections.Counter()
    for e in es:
        p,g=pred(e["n"]),gold[e["n"]]
        if p==g: tp[g]+=1
        else: fp[p]+=1; fn[g]+=1
    print(f"\n{name}: per-category on test (correct / predicted-wrong-as-this / missed)")
    for c in CATEGORIES: 
        if tp[c]+fp[c]+fn[c]: print(f"   {c:10} tp={tp[c]:2} fp={fp[c]:2} fn={fn[c]:2}")
table("RULES",lambda i:rules[i]["category"],TEST); table("SEMANTIC",sem,TEST); table("HYBRID rules_then_semantic",hyb,TEST)
def compare(a,b,an,bn,es):
    fixed=[(title[e["n"]],gold[e["n"]],a(e["n"]),b(e["n"])) for e in es if a(e["n"])!=gold[e["n"]] and b(e["n"])==gold[e["n"]]]
    broke=[(title[e["n"]],gold[e["n"]],a(e["n"]),b(e["n"])) for e in es if a(e["n"])==gold[e["n"]] and b(e["n"])!=gold[e["n"]]]
    print(f"\n{bn} vs {an} on test: corrected {len(fixed)}, worsened {len(broke)}")
    for r in fixed: print("  + fixed  ",r)
    for r in broke: print("  - worsened",r)
compare(lambda i:rules[i]["category"],hyb,"rules","hybrid",TEST)
compare(lambda i:rules[i]["category"],sem,"rules","semantic",TEST)
print("\nrules mistakes on test (gold, predicted):")
for e in TEST:
    if rules[e["n"]]["category"]!=gold[e["n"]]: print("  ",title[e["n"]],"|",gold[e["n"]],"|",rules[e["n"]]["category"],rules[e["n"]]["strength"],"|",e["basis"])

print("\naccuracy on test by label basis (L = stated in listing, J = maintainer judgement, I = insufficient evidence -> general):")
for b in ("L","J","I"):
    es=[e for e in TEST if e["basis"]==b]
    f=lambda pred: sum(pred(e["n"])==gold[e["n"]] for e in es)/len(es)
    print(f"  {b} (n={len(es)}): rules {f(lambda i:rules[i]['category']):.2f}  semantic {f(sem):.2f}  hybrid {f(hyb):.2f}")
print("\nwrong-specific examples (false confident) on test: rules vs semantic")
print(" rules   :",[(title[e['n']][:32],gold[e['n']],rules[e['n']]['category']) for e in TEST if rules[e['n']]['category'] not in ('general',gold[e['n']])][:10])
print(" semantic:",[(title[e['n']][:32],gold[e['n']],sem(e['n'])) for e in TEST if sem(e['n']) not in ('general',gold[e['n']])][:12])
