"""Experiment helpers: ONNX sentence embeddings with onnxruntime + tokenizers, category prototypes, metrics."""
import json
import os
import re
import sys
import time

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "area_events"))

CATEGORIES = ["music", "theater", "festival", "food", "sports", "arts", "science", "family", "community", "outdoors", "general"]

DESCRIPTIONS = {
    "music": ["A concert with live music, a band or a musician performing songs.",
              "A live show by a singer, songwriter, rapper or DJ at a club or music venue.",
              "An orchestra, choir, jazz ensemble or symphony performance.",
              "A tour date, album release show or recital."],
    "theater": ["A theater production: a play, musical or drama performed on stage by actors.",
                "A comedy show with stand-up comedians or improv.",
                "Ballet, dance performance, opera or other performing arts on stage."],
    "festival": ["A festival, parade, carnival or street fair celebrating a holiday or a culture.",
                 "A Halloween haunted attraction or seasonal celebration with crowds and decorations."],
    "food": ["A farmers market selling fresh produce and food.",
             "Food trucks, restaurants, wine and beer tasting, cooking, brunch and dining events.",
             "A culinary event about eating and drinking."],
    "sports": ["A sports game or match between two teams, such as football, basketball, hockey or baseball.",
               "A race, marathon, fitness class or athletic competition."],
    "arts": ["An art exhibition, gallery opening or museum show with paintings and sculpture.",
             "An artist talk, craft workshop, poetry reading or book author event."],
    "science": ["A scientific talk, research lecture, engineering or technology event.",
                "An astronomy, robotics, computer science, artificial intelligence or data science workshop."],
    "family": ["An activity for kids and families with children, such as trick-or-treating or story time."],
    "community": ["A community meeting, support group, volunteer activity or civic gathering.",
                  "A student government committee meeting or public assembly."],
    "outdoors": ["An outdoor recreation event such as hiking, camping, kayaking or a nature walk.",
                 "Exploring a park, garden, beach or wildlife outdoors."],
    "general": ["An academic information session, training course, seminar or administrative event.",
                "A listing that does not say what the event is about."],
}


class Embedder:
    def __init__(self, model_path, tokenizer_path, pooling="mean", threads=None, max_len=128):
        t0 = time.perf_counter()
        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads
        self.session = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        self.tok = Tokenizer.from_file(tokenizer_path)
        self.tok.enable_truncation(max_length=max_len)
        self.tok.enable_padding()
        self.pooling = pooling
        self.names = {i.name for i in self.session.get_inputs()}
        self.init_seconds = time.perf_counter() - t0

    def embed(self, texts, batch=16):
        out = []
        for i in range(0, len(texts), batch):
            enc = self.tok.encode_batch(texts[i:i + batch])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.names:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = self.session.run(None, feed)[0]
            if self.pooling == "cls":
                vec = hidden[:, 0]
            else:
                m = mask[:, :, None].astype(np.float32)
                vec = (hidden * m).sum(1) / np.maximum(m.sum(1), 1e-9)
            vec = vec / np.maximum(np.linalg.norm(vec, axis=1, keepdims=True), 1e-9)
            out.append(vec)
        return np.vstack(out)


def clean(text):
    text = re.sub(r"&amp;|&#039;|&quot;", lambda m: {"&amp;": "&", "&#039;": "'", "&quot;": '"'}[m.group(0)], str(text or ""))
    return " ".join(text.split())


def event_texts(e):
    title = clean(e["title"])
    tags = ", ".join(e.get("categories") or [])
    desc = clean(e.get("description"))[:300]
    if desc in ("-", "N/A"):
        desc = ""
    full = ". ".join(x for x in (title, tags, desc) if x)
    return title, full


def load_sample():
    sample = json.load(open(os.path.join(HERE, "sample.json"), encoding="utf-8"))
    labels = {}
    for line in open(os.path.join(HERE, "labels.txt")):
        n, label, basis = line.split()
        labels[int(n)] = (label, basis)
    for e in sample:
        e["label"], e["basis"] = labels[e["n"]]
    return sample


def prototypes(embedder, with_general=True):
    cats = [c for c in CATEGORIES if with_general or c != "general"]
    protos, groups = [], {}
    for c in cats:
        vecs = embedder.embed(DESCRIPTIONS[c])
        groups[c] = vecs
        v = vecs.mean(0)
        protos.append(v / np.linalg.norm(v))
    return cats, np.vstack(protos), groups


def metrics(pred, gold):
    n = len(gold)
    acc = sum(p == g for p, g in zip(pred, gold)) / n
    wrong_specific = sum(1 for p, g in zip(pred, gold) if p != "general" and p != g)
    missed = sum(1 for p, g in zip(pred, gold) if p == "general" and g != "general")
    general_rate = sum(1 for p in pred if p == "general") / n
    specific = [(p, g) for p, g in zip(pred, gold) if p != "general"]
    precision = (sum(1 for p, g in specific if p == g) / len(specific)) if specific else float("nan")
    return {"accuracy": acc, "wrong_specific": wrong_specific, "wrongly_general": missed, "general_rate": general_rate,
            "precision_of_specific": precision, "n": n}
