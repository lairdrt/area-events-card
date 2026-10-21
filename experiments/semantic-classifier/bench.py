import json,os,sys,time,psutil
from semlib import CATEGORIES, Embedder, event_texts, load_sample, metrics, prototypes  # noqa: F401
key=sys.argv[1]; threads=int(sys.argv[2]) if len(sys.argv)>2 else 4
M={"minilm-l6 fp32":("models/sentence-transformers__all-MiniLM-L6-v2/onnx/model.onnx","models/sentence-transformers__all-MiniLM-L6-v2/tokenizer.json","mean"),
   "minilm-l6 int8":("models/sentence-transformers__all-MiniLM-L6-v2/onnx/model_quint8_avx2.onnx","models/sentence-transformers__all-MiniLM-L6-v2/tokenizer.json","mean"),
   "paraphrase-l3 fp32":("models/sentence-transformers__paraphrase-MiniLM-L3-v2/onnx/model.onnx","models/sentence-transformers__paraphrase-MiniLM-L3-v2/tokenizer.json","mean"),
   "paraphrase-l3 int8":("models/sentence-transformers__paraphrase-MiniLM-L3-v2/onnx/model_quint8_avx2.onnx","models/sentence-transformers__paraphrase-MiniLM-L3-v2/tokenizer.json","mean"),
   "bge-small fp32":("models/BAAI__bge-small-en-v1.5/onnx/model.onnx","models/BAAI__bge-small-en-v1.5/tokenizer.json","cls")}
proc=psutil.Process()
base=proc.memory_info().rss/1e6
sample=load_sample()
texts=[event_texts(e)[1] for e in sample]
texts500=(texts*5)[:500]; texts100=texts500[:100]
m,t,pool=M[key]
emb=Embedder(m,t,pool,threads=threads)
after_load=proc.memory_info().rss/1e6
emb.embed(texts[:8])  # warm-up (first run allocates arenas)
res={"model":key,"threads":threads,"file_MB":round(os.path.getsize(m)/1e6,1),"tokenizer_MB":round(os.path.getsize(t)/1e6,2),
     "cold_init_s":round(emb.init_seconds,2),"rss_before_MB":round(base),"rss_after_load_MB":round(after_load)}
for name,tx in (("100",texts100),("500",texts500)):
    t0=time.perf_counter(); emb.embed(tx,batch=16); res["embed_"+name+"_s"]=round(time.perf_counter()-t0,2)
res["peak_rss_MB"]=round(proc.memory_info().peak_wset/1e6)
print(json.dumps(res))
