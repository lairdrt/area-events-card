import json
import os

import numpy as np

from semlib import HERE, Embedder, event_texts, load_sample, metrics, prototypes
from discovery import classify

MODELS = {
    "minilm-l6 fp32": ("models/sentence-transformers__all-MiniLM-L6-v2/onnx/model.onnx", "models/sentence-transformers__all-MiniLM-L6-v2/tokenizer.json", "mean"),
    "minilm-l6 int8": ("models/sentence-transformers__all-MiniLM-L6-v2/onnx/model_quint8_avx2.onnx", "models/sentence-transformers__all-MiniLM-L6-v2/tokenizer.json", "mean"),
    "paraphrase-l3 fp32": ("models/sentence-transformers__paraphrase-MiniLM-L3-v2/onnx/model.onnx", "models/sentence-transformers__paraphrase-MiniLM-L3-v2/tokenizer.json", "mean"),
    "bge-small fp32": ("models/BAAI__bge-small-en-v1.5/onnx/model.onnx", "models/BAAI__bge-small-en-v1.5/tokenizer.json", "cls"),
}

sample = load_sample()
CAL = [e for e in sample if e["n"] < 40]
TEST = [e for e in sample if e["n"] >= 40]
rules = {e["n"]: classify.classify({"title": e["title"], "description": e["description"], "categories": e["categories"], "venue": e["venue"]}) for e in sample}


def gold(es):
    return [e["label"] for e in es]


def rule_pred(es):
    return [rules[e["n"]]["category"] for e in es]


print("labels: calibration", len(CAL), "test", len(TEST), "| general share (test)", round(sum(1 for e in TEST if e["label"] == "general") / len(TEST), 2))
base_cal, base_test = metrics(rule_pred(CAL), gold(CAL)), metrics(rule_pred(TEST), gold(TEST))
print("RULES  calibration", {k: round(v, 3) for k, v in base_cal.items()})
print("RULES  test       ", {k: round(v, 3) for k, v in base_test.items()})

results = {"rules": {"cal": base_cal, "test": base_test}}
cache = {}


def sims_for(embedder, name, with_general):
    key = (name, with_general)
    if key in cache:
        return cache[key]
    cats, protos, groups = prototypes(embedder, with_general)
    titles, fulls = zip(*[event_texts(e) for e in sample])
    et, ef = embedder.embed(list(titles)), embedder.embed(list(fulls))
    out = {"cats": cats, "title": et @ protos.T, "full": ef @ protos.T,
           "title_max": np.stack([(et @ groups[c].T).max(1) for c in cats], 1), "full_max": np.stack([(ef @ groups[c].T).max(1) for c in cats], 1)}
    cache[key] = out
    return out


def decide(sim, cats, threshold, margin):
    order = np.argsort(-sim)
    top, second = sim[order[0]], sim[order[1]]
    best = cats[order[0]]
    if best == "general" or top < threshold or top - second < margin:
        return "general"
    return best


for name, (m, t, pool) in MODELS.items():
    e = Embedder(m, t, pool)
    for with_general in (False, True):
        s = sims_for(e, name, with_general)
        cats = s["cats"]
        best = None
        for variant in ("title", "full", "title_max", "full_max", "blend", "blend_max"):
            if variant == "blend":
                mat = 0.4 * s["title"] + 0.6 * s["full"]
            elif variant == "blend_max":
                mat = 0.4 * s["title_max"] + 0.6 * s["full_max"]
            else:
                mat = s[variant]
            lo, hi = float(mat.min()), float(mat.max())
            for th in np.linspace(lo, hi, 18):
                for mg in (0.0, 0.01, 0.02, 0.04, 0.06):
                    pc = [decide(mat[e_["n"]], cats, th, mg) for e_ in CAL]
                    acc = sum(p == g for p, g in zip(pc, gold(CAL))) / len(CAL)
                    wrong = sum(1 for p, g in zip(pc, gold(CAL)) if p != "general" and p != g)
                    score = acc - 0.01 * wrong   # prefer accuracy, then fewer confident mistakes
                    if best is None or score > best[0]:
                        best = (score, variant, float(th), mg)
        _, variant, th, mg = best
        mat = {"title": s["title"], "full": s["full"], "title_max": s["title_max"], "full_max": s["full_max"], "blend": 0.4 * s["title"] + 0.6 * s["full"], "blend_max": 0.4 * s["title_max"] + 0.6 * s["full_max"]}[variant]
        pt = [decide(mat[e_["n"]], cats, th, mg) for e_ in TEST]
        mt = metrics(pt, gold(TEST))
        pc = [decide(mat[e_["n"]], cats, th, mg) for e_ in CAL]
        mc = metrics(pc, gold(CAL))
        tag = f"{name} general-prototype={with_general}"
        print(f"SEMANTIC {tag:46} variant={variant:9} th={th:.3f} mg={mg}  cal acc {mc['accuracy']:.3f} | TEST acc {mt['accuracy']:.3f} wrong-specific {mt['wrong_specific']} wrongly-general {mt['wrongly_general']} general {mt['general_rate']:.2f}")
        results[tag] = {"variant": variant, "threshold": th, "margin": mg, "cal": mc, "test": mt}

        # hybrid strategies, parameters fixed from calibration only
        def hybrid(es, mode):
            out = []
            for e_ in es:
                r = rules[e_["n"]]
                sem = decide(mat[e_["n"]], cats, th, mg)
                if mode == "rules_then_semantic":      # semantic only fills in where the rules had no clear answer
                    out.append(r["category"] if not r["fallback"] else sem)
                elif mode == "strong_rules_else_semantic":
                    out.append(r["category"] if r["strength"] in ("strong", "moderate") else sem)
                elif mode == "agree_or_strong":        # accept a weak rule result only if semantic agrees; otherwise general
                    if r["strength"] in ("strong", "moderate"):
                        out.append(r["category"])
                    elif r["fallback"]:
                        out.append(sem)
                    else:
                        out.append(r["category"] if sem == r["category"] else "general")
                elif mode == "semantic_veto":           # a confident rule result stands unless semantic confidently names a different category
                    if r["fallback"]:
                        out.append(sem)
                    elif sem not in ("general", r["category"]) and r["strength"] != "strong":
                        out.append("general")
                    else:
                        out.append(r["category"])
            return out
        for mode in ("rules_then_semantic", "strong_rules_else_semantic", "agree_or_strong", "semantic_veto"):
            mh = metrics(hybrid(TEST, mode), gold(TEST))
            mhc = metrics(hybrid(CAL, mode), gold(CAL))
            print(f"   HYBRID {mode:28} cal acc {mhc['accuracy']:.3f} | TEST acc {mh['accuracy']:.3f} wrong-specific {mh['wrong_specific']} wrongly-general {mh['wrongly_general']} general {mh['general_rate']:.2f}")
            results[tag + " / " + mode] = {"cal": mhc, "test": mh}
json.dump(results, open(os.path.join(HERE, "results.json"), "w"), indent=1, default=float)
