# Experiment: compact semantic models for event illustration classification

Question: can a small local embedding model clearly beat the weighted classifier in `area_events/discovery/classify.py` (commit `1590028`) at deciding what an event is about? Nothing here is imported by production code; no model files are tracked.

**Result: no.** On 80 held-out real listings the best semantic or hybrid variant did not beat the current classifier (hybrid net -1 event), costs 200-300 MB of RAM and 10-25 s per 500 events on the test machine, and cannot be installed on the app's current Alpine image. Recommendation: **A, keep the current classifier**, with a few lexicon fixes the experiment exposed (below).

## Reproduce

```
python -m venv venv && venv/Scripts/pip install onnxruntime tokenizers numpy psutil huggingface_hub model2vec
python download_models.py            # ONNX models into ./models (ignored by Git)
python run_eval.py                   # semantic + hybrid variants, calibration -> test
python analyze.py                    # per-category errors, corrected / worsened examples, accuracy by label basis
python bench.py "minilm-l6 fp32" 4   # size, init, peak RSS, 100 / 500 events
python potion_eval.py; python loo.py
```

## Evaluation data and its limits

- `sample.json`: 120 real, unique-title listings (San Diego and Austin, public excerpts) sampled at random with a fixed seed, excluding the 55 used for the classifier's own snapshot. Rules were developed while looking at part of this wider pool, so rules may be slightly flattered.
- `labels.txt`: labels written **before** looking at any classifier output. Each has a basis: **L** the listing itself states the subject (47), **J** my judgement from the text (59), **I** the listing gives no evidence, so the correct illustration is `general` (14). Performer-only titles with no tag or description are `I`, never guessed. J labels are subjective; nothing here is ground truth, and no source fact is claimed beyond what the listing says.
- Split by position: 40 events to choose thresholds and variants (calibration), 80 never used for choices (test). Semantic and hybrid parameters come from calibration only. With 80 events the interval is wide (rules on all 120: 75%, 95% Wilson interval 67-82%).
- Category descriptions were written once from general knowledge, not tuned on the data.

## Accuracy (exact match with the label; 80 test events, 42% of which are `general`)

| Classifier | Accuracy | Confident but wrong | Wrongly `general` | `general` rate |
|---|---|---|---|---|
| Current rules | **75%** (60/80) | 10 | 10 | 44% |
| MiniLM-L6 fp32 + descriptions | 64% | 23 | 6 | 33% |
| MiniLM-L6 uint8 | 59-66% | 24-31 | 2-3 | 19-28% |
| paraphrase-MiniLM-L3 | 56-58% | 18-20 | 15-16 | 42-46% |
| bge-small-en-v1.5 | 54-64% | 24-29 | 5-8 | 25-30% |
| potion-base-8M (static) | 55% | 31 | 5 | 20% |
| Leave-one-out 1/3/5-NN on the 120 labels | 61-66% | | | |
| Hybrid: semantic only where the rules gave no answer | 74% | 20 | 1 | 24% |
| Other hybrids (four strategies, six models) | 60-71% | 17-26 | | |

Always answering `general` would score 38% on all 120. By label basis on the test set (rules / semantic / hybrid): **L** 89% / 82% / 96% (n=28), **J** 63% / 47% / 56% (n=43), **I** 89% / 89% / 89% (n=9). Where a listing states its subject the rules are already right 9 times in 10, and the hybrid's gain there is 2 events out of 28.

Paired comparison on the test set: hybrid corrected 6 and worsened 7 relative to the rules; pure semantic corrected 8 and worsened 17 (not significant, p about 0.1, but clearly not better).

## Examples (test set)

Corrected by the semantic layer: "San Diego Gulls Home Game" (sports), "Student Affairs Meeting" and "Public Assembly" (community), "Virtual Talk - Understanding Mannerism" (arts), "The Haunted Trail of Balboa Park" (festival), "Popsicle Pop-Up" (food).

Worsened: "Hocus Pocus at Rooftop Cinema Club" (a film screening became festival), "Religious Roots of Halloween" (a lecture became festival), "Career Storytelling Workshop" (arts), "Graduate Job Search Working Group" (community), and, if used alone, "Foster The People and Goth Babe" (music became festival), "Spooky Shakespeare" (theater became festival), "ChatGPT Work Training" (science became general). Embeddings drift toward seasonal and festival words and cannot tell a screening from a festival.

Everything the semantic layer fixed is also fixable with the rules, which is the useful finding: "Gulls Home Game" is a **defect in the current classifier** (the `home game` negative phrase wrongly removes a sports term), and "Meeting", "Assembly", "Mannerism", "Haunted Trail" and "Popsicle" need small lexicon weight changes. Those are not applied here, because this task changes no production behaviour.

## Resources (measured: Intel i7-5500U, 2 cores / 4 threads, 2.4 GHz, Windows, Python 3.14, onnxruntime 1.31; 4 threads)

| Model | File | Cold init | RAM after load / peak (process incl. ~50 MB baseline) | 100 events | 500 events |
|---|---|---|---|---|---|
| MiniLM-L6 fp32 | 90 MB | 0.4 s | 159 / 270 MB | 2.1 s | 10.9 s |
| MiniLM-L6 uint8 (avx2 build) | 23 MB | 0.3 s | 94 / 207 MB | 2.3 s | 11.9 s |
| paraphrase-MiniLM-L3 fp32 / uint8 | 69 / 17.5 MB | 0.2 s | 135 / 247 and 85 / 198 MB | 1.3 / 1.0 s | 7.4 / 6.8 s |
| bge-small-en-v1.5 fp32 | 133 MB | 0.7 s | 205 / 275 MB | 5.0 s | 24.8 s |
| potion-base-8M (numpy only) | 30 MB | 5.0 s (first load) | 138 / 161 MB | | 0.06 s |
| Current rules | 0 | 0 | negligible | | about 0.2 s for 579 |

Raspberry Pi 4 was **not available**. Estimate, not a measurement: the Pi's four Cortex-A72 cores at 1.5 GHz are in the same order as this 2015 two-core laptop, so expect about 1x-3x these times (roughly 12-40 s per 500 events for MiniLM-L6) and the same memory, plus a transient spike when many new events arrive. Caching embeddings by text hash would make repeat refreshes nearly free. Zero-shot NLI classification needs one model pass per event per label (11 passes), so about 11 times the embedding cost (an estimate from that arithmetic, not measured); it was not run.

## Packaging and licence findings (from PyPI and Hugging Face metadata)

- **onnxruntime 1.31.0 has no musllinux wheels** (Windows, macOS and glibc `manylinux_2_28` only, aarch64 included, 21 MB). The app's current base image, `base-python:3.14-alpine3.24`, is Alpine (musl), so onnxruntime cannot be pip-installed there; it would need a Debian-based app image or a source build. `tokenizers` (10 MB) and `numpy` (17 MB) do have musllinux aarch64 wheels.
- Added footprint if adopted: roughly 60 MB of wheels plus 17-90 MB of model, a first-start model download or a bundled model, and about 200-300 MB of RAM while embedding.
- Licences: all-MiniLM-L6-v2 and paraphrase-MiniLM-L3-v2 Apache-2.0; bge-small-en-v1.5 and potion-base-8M MIT.

## Combining evidence

Title-only, title + tags + description, and a 0.4/0.6 blend were compared, each with mean or best-of-description similarity. Best-of-description on the combined text won on calibration for every model. Embeddings of a bare performer name carry no genre, so a semantic model either guesses from the name's sound (false confidence) or abstains; the rules abstain deliberately.

## Recommendation: A, keep the current classifier

- Do not add a semantic layer on this evidence. A change would be worth its cost only if, on at least 150 independently labelled events, it improved accuracy by **8 or more points absolute** (about 12 more events right out of 150) **without raising the confident-but-wrong count**, while adding **no more than 150 MB** of RAM, **10 s** of work per refresh on the Pi, and **no base-image change**. The best variant here moved accuracy by -1 point.
- Cheaper, certain gains: fix the `home game` negative phrase and tune the few lexicon weights above, then re-check on this sample (a fresh sample is needed for an unbiased number).
- If the decision is revisited: a **hybrid** (rules first, semantic only for events with no rules evidence), MiniLM-L6 uint8, embeddings cached by text hash, on a Debian-based app image, is the only combination that was not worse; it would need the larger labelled set above to justify itself.

## Blockers for any later implementation

No Raspberry Pi 4 test; musl/Alpine incompatibility of onnxruntime; no large independently labelled set (the 120 labels are one reader's); J-basis labels are subjective.
