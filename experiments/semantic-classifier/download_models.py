"""Download the experiment's ONNX models into ./models (not tracked by Git)."""
import os
import time

from huggingface_hub import hf_hub_download

WANT = {
    "sentence-transformers/all-MiniLM-L6-v2": ["onnx/model.onnx", "onnx/model_quint8_avx2.onnx", "onnx/model_qint8_arm64.onnx", "tokenizer.json"],
    "sentence-transformers/paraphrase-MiniLM-L3-v2": ["onnx/model.onnx", "onnx/model_quint8_avx2.onnx", "tokenizer.json"],
    "BAAI/bge-small-en-v1.5": ["onnx/model.onnx", "tokenizer.json"],
}
for repo, files in WANT.items():
    for name in files:
        started = time.time()
        path = hf_hub_download(repo, name, local_dir=os.path.join("models", repo.replace("/", "__")))
        print(repo, name, round(os.path.getsize(path) / 1e6, 1), "MB", round(time.time() - started, 1), "s")
