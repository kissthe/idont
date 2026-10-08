"""按 docs/评测方案.md 第 4.4、5 节给模型作答打分。

用法：
    python tools/score.py <run_id> [题目文件 ...]
读取 results/<run_id>/predictions.jsonl（每行 {"sample_id", "condition", "response"}），
输出 results/<run_id>/scores.json 和 results/<run_id>/report.md。
"""
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_eval import CONDITIONS, EMOTIONS, ROOT, load_questions, question_files  # noqa: E402

H, P, I = "history_supported", "present_cause_sufficient", "insufficient_evidence"
LABELS = [H, P, I]
SHORT = {H: "H", P: "P", I: "I"}
TID = re.compile(r"^D\d+:\d+$")


def parse(response, item):
    """返回 (label, cue_id, evidence set, emotion, ok)。"""
    objs = re.findall(r"\{[^{}]*\}", response or "", re.S)
    data = None
    for raw in reversed(objs):
        try:
            data = json.loads(raw)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(data, dict) or data.get("label") not in LABELS:
        return None, "none", set(), None, False
    opts = {o["cue_id"] for o in item["current_input"]["cue_options"]}
    label = data["label"]
    cue = data.get("cue_id") if data.get("cue_id") in opts else "none"
    ev = {re.sub(r"\s+", "", str(t)) for t in data.get("evidence_turn_ids") or []}
    ev = {t for t in ev if TID.match(t)}
    if label != H:
        cue, ev = "none", set()
    emo = data.get("current_emotion") if data.get("current_emotion") in EMOTIONS else None
    return label, cue, ev, emo, True


def f1(p, r):
    return 2 * p * r / (p + r) if p + r else 0.0


def macro_f1(pairs):
    out = {}
    for lab in LABELS:
        tp = sum(g == lab and y == lab for g, y in pairs)
        fp = sum(g != lab and y == lab for g, y in pairs)
        fn = sum(g == lab and y != lab for g, y in pairs)
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        out[lab] = {"precision": p, "recall": r, "f1": f1(p, r), "support": tp + fn}
    return sum(v["f1"] for v in out.values()) / 3, out


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def metrics(rows):
    """rows：同一条件下每题的 dict（含 gold 与预测）。"""
    pairs = [(r["gold"], r["pred"]) for r in rows]
    mf1, per = macro_f1(pairs)
    hs = [r for r in rows if r["gold"] == H]
    nonh = [r for r in rows if r["gold"] != H]
    ev = []
    for r in hs:
        gold, sup, pred = r["ev_gold"], r["ev_sup"], r["ev_pred"]
        rec = len(pred & gold) / len(gold) if gold else 0.0
        strict = len(pred & gold) / len(pred) if pred else 0.0
        lenient = len(pred & (gold | sup)) / len(pred) if pred else 0.0
        sess = any(t.split(":")[0][1:] in r["sess_gold"] for t in pred)
        ev.append((rec, strict, lenient, sess, r["cue_ok"], r["pred"] == H))
    out = {
        "n": len(rows),
        "parse_fail": sum(not r["ok"] for r in rows),
        "accuracy": mean(r["gold"] == r["pred"] for r in rows),
        "macro_f1": mf1,
        "per_class": per,
        "confusion": {SHORT[g]: {SHORT[y] if y else "fail": sum(r["gold"] == g and r["pred"] == y for r in rows)
                                 for y in LABELS + [None]} for g in LABELS},
        "T2_cue_acc": mean(e[4] for e in ev),
        "T3_recall": mean(e[0] for e in ev),
        "T3_precision_strict": mean(e[1] for e in ev),
        "T3_precision_lenient": mean(e[2] for e in ev),
        "T3_f1_strict": mean(f1(e[1], e[0]) for e in ev),
        "T3_session_hit": mean(e[3] for e in ev),
        "joint": mean(e[5] and e[4] and e[0] >= 0.5 for e in ev),
        "misattribution": mean(r["pred"] == H for r in nonh),
        "misattribution_by_type": {t: mean(r["pred"] == H for r in nonh if r["type"] == t)
                                   for t in sorted({r["type"] for r in nonh})},
        "acc_by_type": {t: mean(r["gold"] == r["pred"] for r in rows if r["type"] == t)
                        for t in sorted({r["type"] for r in rows})},
        "emotion_acc": mean(r["emo_pred"] == r["emo_gold"] for r in rows if r["emo_gold"] != "none_observed"),
    }
    return out


def bootstrap(rows, key, n=1000, seed=0):
    rng = random.Random(seed)
    vals = []
    for _ in range(n):
        sample = [rng.choice(rows) for _ in rows]
        v = metrics(sample)[key]
        if v is not None:
            vals.append(v)
    vals.sort()
    return [vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]] if vals else None


def pair_consistency(by_id, metas):
    """H 题与对应 I_no_history 题（同 family、同 X）都答对的比例。"""
    hits = []
    for sid, m in metas.items():
        if m["sample_type"] != "I_no_history" or sid not in by_id:
            continue
        h = [s for s, mm in metas.items() if mm["family_id"] == m["family_id"] and mm["sample_type"] == "H"
             and s in by_id and mm["conv_id"] == m["conv_id"]]
        h = [s for s in h if by_id[s]["text"] == by_id[sid]["text"]]
        if h:
            hits.append(by_id[h[0]]["gold"] == by_id[h[0]]["pred"] and by_id[sid]["gold"] == by_id[sid]["pred"])
    return mean(hits), len(hits)


def main():
    run_id = sys.argv[1]
    out = ROOT / "results" / run_id
    qs = {it["sample_id"]: (it, m) for it, m, _, _ in load_questions(question_files(sys.argv[2:]))}
    metas = {sid: m for sid, (_, m) in qs.items()}
    preds = [json.loads(l) for l in open(out / "predictions.jsonl") if l.strip()]
    rows = defaultdict(dict)
    for p in preds:
        it, m = qs[p["sample_id"]]
        label, cue, ev, emo, ok = parse(p["response"], it)
        g = it["gold"]
        rows[p["condition"]][p["sample_id"]] = {
            "sample_id": p["sample_id"], "type": m["sample_type"], "source": "golden_triples" if "_A" in p["sample_id"] else "conv48",
            "text": it["current_input"]["text"], "gold": g["label"], "pred": label, "ok": ok,
            "cue_ok": cue == g["gold_cue_id"], "ev_gold": set(g["evidence_turn_ids"]),
            "ev_sup": set(m["supporting_turn_ids"]), "ev_pred": ev,
            "sess_gold": {s.split("_")[1] for s in g["evidence_session_ids"]},
            "emo_gold": g["current_emotion"], "emo_pred": emo,
        }
    scores = {}
    for cond in CONDITIONS:
        rs = list(rows[cond].values())
        if not rs:
            continue
        s = metrics(rs)
        s["macro_f1_ci95"] = bootstrap(rs, "macro_f1")
        s["misattribution_ci95"] = bootstrap(rs, "misattribution")
        s["pair_consistency"], s["pairs"] = pair_consistency(rows[cond], metas)
        s["by_source"] = {src: metrics([r for r in rs if r["source"] == src])["macro_f1"]
                          for src in sorted({r["source"] for r in rs})}
        scores[cond] = s
    # x_only 下答对的 H 题：可能泄漏
    x = rows.get("x_only", {})
    scores["x_only_solved_H"] = sorted(sid for sid, r in x.items() if r["gold"] == H and r["pred"] == H)
    scores["errors_full_history"] = [
        {"sample_id": r["sample_id"], "type": r["type"], "gold": SHORT[r["gold"]], "pred": SHORT.get(r["pred"], "fail")}
        for r in rows.get("full_history", {}).values() if r["gold"] != r["pred"]]
    json.dump(scores, open(out / "scores.json", "w"), ensure_ascii=False, indent=2, default=list)
    (out / "report.md").write_text(report(run_id, scores))
    print((out / "report.md").read_text())


def fmt(v, pct=False):
    if v is None:
        return "—"
    return f"{v * 100:.1f}" if pct else f"{v:.3f}"


def report(run_id, S):
    conds = [c for c in CONDITIONS if c in S]
    L = [f"# 评测报告：{run_id}", "", "## 主表", "",
         "| 指标 | " + " | ".join(conds) + " |", "|---|" + "---|" * len(conds)]
    rowdefs = [("题数", "n", None), ("解析失败", "parse_fail", None), ("T1 准确率", "accuracy", "p"),
               ("**T1 macro-F1**", "macro_f1", "f"), ("T2 线索准确率（gold H）", "T2_cue_acc", "p"),
               ("T3 证据召回", "T3_recall", "p"), ("T3 证据精确率（严格）", "T3_precision_strict", "p"),
               ("T3 证据精确率（宽松）", "T3_precision_lenient", "p"), ("T3 session 命中率", "T3_session_hit", "p"),
               ("联合正确率", "joint", "p"), ("**误归因率**（gold P/I 判成 H）", "misattribution", "p"),
               ("成对一致率（H 与无历史关联都对）", "pair_consistency", "p"), ("情绪准确率", "emotion_acc", "p")]
    for name, k, kind in rowdefs:
        cells = []
        for c in conds:
            v = S[c][k]
            cells.append(str(v) if kind is None else fmt(v, kind == "p"))
        L.append(f"| {name} | " + " | ".join(cells) + " |")
    L.append("| macro-F1 95% CI | " + " | ".join(
        f"[{S[c]['macro_f1_ci95'][0]:.2f}, {S[c]['macro_f1_ci95'][1]:.2f}]" for c in conds) + " |")
    L += ["", "百分比指标单位为 %。成对一致率的分母为 " + ", ".join(f"{c}: {S[c]['pairs']}" for c in conds) + "。", ""]
    L += ["## 按题型的准确率（%）", "", "| 题型 | " + " | ".join(conds) + " |", "|---|" + "---|" * len(conds)]
    for t in sorted({t for c in conds for t in S[c]["acc_by_type"]}):
        L.append(f"| {t} | " + " | ".join(fmt(S[c]["acc_by_type"].get(t), True) for c in conds) + " |")
    L += ["", "## 混淆矩阵（行 gold，列预测）", ""]
    for c in conds:
        L += [f"**{c}**", "", "| gold \\ pred | H | P | I | fail |", "|---|---|---|---|---|"]
        for g in "HPI":
            row = S[c]["confusion"][g]
            L.append(f"| {g} | {row['H']} | {row['P']} | {row['I']} | {row['fail']} |")
        L.append("")
    L += ["## 按数据来源的 macro-F1", "", "| 来源 | " + " | ".join(conds) + " |", "|---|" + "---|" * len(conds)]
    for src in sorted({s for c in conds for s in S[c]["by_source"]}):
        L.append(f"| {src} | " + " | ".join(fmt(S[c]["by_source"].get(src)) for c in conds) + " |")
    L += ["", f"## 只给 X 就判对的 H 题（{len(S['x_only_solved_H'])} 道，可能泄漏）", ""]
    L += [f"- {s}" for s in S["x_only_solved_H"]] or ["- 无"]
    L += ["", f"## full_history 下的错题（{len(S['errors_full_history'])} 道）", "",
          "| 题号 | 题型 | gold | 预测 |", "|---|---|---|---|"]
    L += [f"| {e['sample_id']} | {e['type']} | {e['gold']} | {e['pred']} |" for e in S["errors_full_history"]]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
