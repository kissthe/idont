"""把 golden_triples.jsonl（每个锚点带 A/B/C 三个变体）转成评测题目，按对话分文件夹输出 Markdown。

用法：
    python tools/build_from_triples.py data/questions/specs/golden_triples.spec.json [--prompts]

每个锚点出 4 题：
    A → H（history_supported）
    A → I-无历史关联（同一 X，删除证据所在 session 并重新编号；spec 里写了 skip_cf 的不出）
    B → P（present_cause_sufficient）
    C → I-近错（insufficient_evidence / near_miss）
--prompts 会额外为每题输出“给完整历史”条件下的模型输入原文（.txt）。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_format  # noqa: E402
from build_questions import (EMOTIONS, LABEL, LEAK_RE, build_history, load_locomo_sessions,  # noqa: E402
                             make_options, ngrams, render_turn, INSTRUCTIONS)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data/questions/golden_triples"

VARIANTS = [  # (key, 来源变体, 标签, I 子类, 中文名)
    ("H", "A", "H", None, "H"),
    ("CF", "A", "I", "no_history_link", "I-无历史关联"),
    ("P", "B", "P", None, "P"),
    ("NM", "C", "I", "near_miss", "I-近错"),
]
SAMPLE_TYPE = {"H": "H", "CF": "I_no_history", "P": "P_same_cue", "NM": "I_near_miss"}
ROLE_ZH = {"anchor": "锚点线索", "history_unrelated": "历史中出现但无关", "present_salient": "当下显眼", "none": "无"}


def uid_to_tid(uid):
    s, i = uid.split(":", 1)[1][1:].split(":")
    return f"S{int(s):02d}_T{int(i):02d}"


def sid(tid):
    return tid.split("_")[0]


def make_history(sessions, target, removed):
    window = [s for s in sessions if int(s["session_id"][1:]) < target]
    return build_history(window, set(removed))


def render_prompt(q, hist):
    u, ci = q["user"], q["x"]
    parts = [INSTRUCTIONS.format(u=u, emos=", ".join(EMOTIONS)), "", "## Conversation history"]
    for se in hist:
        parts += ["", f"### {se['session_id']} ({se['date']})"] + [render_turn(t) for t in se["turns"]]
    parts += ["", f"## Current situation: {ci['session_id']} ({ci['date']})",
              render_turn(ci["target_turn"]) + "   <- target turn", "", "## Candidate cues"]
    parts += [f"- {o['cue_id']}: {o['name']}" for o in q["options"]]
    return "\n".join(parts)


def build_anchor(tr, sp, sessions):
    conv, user, target = tr["sample_id"], tr["speaker"], tr["target_session"]
    src = {t["turn_id"]: t for se in sessions for t in se["turns"]}
    required = [uid_to_tid(u) for u in tr["h_star"]]
    supporting = [uid_to_tid(e["turn_uid"]) for e in tr["b_evidence"] if uid_to_tid(e["turn_uid"]) not in required]
    supporting += [t for t in sp.get("supporting_add", []) if t not in supporting]
    evidence_turns = sorted(set(required) | set(supporting))
    emotion = sp.get("emotion", tr["target_emotion"])
    texts = {k: v["text"] for k, v in tr["variants"].items()}
    for k, fixes in sp.get("text_fixes", {}).items():
        for a, b in fixes:
            texts[k] = texts[k].replace(a, b)

    hist_full, _ = make_history(sessions, target, [])
    cf_removed = sorted({sid(t) for t in evidence_turns})
    hist_cf, cf_map = make_history(sessions, target, cf_removed)
    kws = sp["residual_keywords"]
    residual = [f"{t['turn_id']}({t['speaker']})" for se in hist_cf for t in se["turns"]
                if any(re.search(rf"\b{re.escape(k)}\b", t["text"], re.I) for k in kws)]

    h1 = (sp["hist"][0]["name"], "history_unrelated")
    alt_a = (sp["a_present"], "present_salient") if sp["a_present"] else (sp["hist"][1]["name"], "history_unrelated")
    option_sets = {
        "A": [(sp["cue_name"], "anchor"), h1, alt_a],
        "B": [(sp["cue_name"], "anchor"), h1, (sp["b_present"], "present_salient")],
        "C": [(sp["cue_name"], "anchor"), h1, (sp["c_lookalike"], "present_salient")],
    }
    date = next(f"{s['date']}" for s in sessions if s["session_id"] == f"S{target:02d}")

    questions = []
    for key, var, lab, sub, zh in VARIANTS:
        if key == "CF" and sp.get("skip_cf"):
            continue
        hist = hist_cf if key == "CF" else hist_full
        x_sid = f"S{len(hist) + 1:02d}"
        opts, roles, _ = make_options(f"{tr['anchor_id']}_{var}", [(n, r, None) for n, r in option_sets[var]])
        gold_cue = next(c for c, r in roles.items() if r == "anchor") if lab == "H" else "none"
        q = {
            "qid": f"{tr['anchor_id']}-{key}", "type_zh": zh, "variant": var, "user": user,
            "label": LABEL[lab], "i_subtype": sub, "gold_cue": gold_cue,
            "evidence": required if lab == "H" else [], "supporting": supporting if lab == "H" else [],
            "emotion": emotion, "options": opts, "roles": roles,
            "removed": cf_removed if key == "CF" else [], "session_map": cf_map if key == "CF" else None,
            "hist_len": len(hist),
            "x": {"session_id": x_sid, "date": date,
                  "target_turn": {"turn_id": f"{x_sid}_T01", "speaker": user, "text": texts[var]}},
            "explanation": tr["variants"][var]["explanation"],
            "key": key, "cutoff_after": f"S{target - 1:02d}",
            "text_fixed": texts[var] != tr["variants"][var]["text"],
        }
        # 自动检查
        flags = []
        txt = texts[var]
        hits = sorted({m.group(0).lower() for t in [txt] + [o["name"] for o in opts] for m in LEAK_RE.finditer(t)})
        if hits:
            flags.append("泄漏词：" + ", ".join(hits))
        if key != "CF":
            ov = ngrams(txt) & ngrams(" ".join(src[t]["text"] for t in evidence_turns))
            if ov:
                flags.append("与证据原文重合的 5 词片段：" + " | ".join(sorted(ov)))
        if key == "CF" and residual:
            flags.append("反事实残留（需人工确认与本题无关）：" + ", ".join(residual))
        if any(sid(t) >= f"S{target:02d}" for t in q["evidence"]):
            flags.append("结构：证据不早于 X 所在 session")
        if emotion not in EMOTIONS:
            flags.append("结构：情绪不在枚举内")
        for h in sp["hist"]:
            if sid(h["turn"]) >= f"S{target:02d}" or (key == "CF" and sid(h["turn"]) in cf_removed):
                flags.append(f"结构：干扰项 “{h['name']}” 不在本题历史中")
        q["flags"] = flags
        q["prompt"] = render_prompt(q, hist)
        questions.append(q)
    return {"tr": tr, "sp": sp, "required": required, "supporting": supporting, "emotion": emotion,
            "src": src, "residual": residual, "cf_removed": cf_removed, "questions": questions}


def to_sample(q, a, spec_rel):
    """转成与 data/questions/locomo/conv-48.json 相同的题目结构（gold 里多一个 supporting_turn_ids）。"""
    tr, conv = a["tr"], a["tr"]["sample_id"]
    ops = [f"truncate_after:{q['cutoff_after']}"]
    if q["removed"]:
        ops.append("remove_sessions:" + ",".join(q["removed"]) + "; renumber")
    ops.append(f"add_session:{q['x']['session_id']}")
    if q["text_fixed"]:
        ops.append("text_fix:" + "; ".join(f"{x}->{y}" for x, y in a["sp"]["text_fixes"][q["variant"]]))
    if q["key"] == "CF":
        note = f"与 {tr['anchor_id']}-H 的 X 和选项完全相同，历史中删除了证据所在的 {', '.join(q['removed'])}。"
    else:
        note = "；".join(a["sp"]["review_notes_zh"])
    return {
        "sample_id": f"{conv}_{q['qid']}",
        "family_id": f"{conv}_{tr['anchor_id']}",
        "sample_type": SAMPLE_TYPE[q["key"]],
        "eval_user_id": q["user"],
        "history": {"conv_id": conv, "source": "locomo", "cutoff_after": q["cutoff_after"],
                    "removed_sessions": q["removed"], "session_id_map": q["session_map"]},
        "current_input": {"session_id": q["x"]["session_id"], "date": q["x"]["date"],
                          "context_turns": [], "target_turn": q["x"]["target_turn"],
                          "image_refs": [], "cue_options": q["options"]},
        "gold": {"label": q["label"], "i_subtype": q["i_subtype"], "gold_cue_id": q["gold_cue"],
                 "evidence_turn_ids": q["evidence"],
                 "evidence_session_ids": sorted({sid(t) for t in q["evidence"]}),
                 "supporting_turn_ids": q["supporting"],
                 "current_emotion": q["emotion"], "option_roles": q["roles"], "confidence": 2},
        "edit_log": {"generator": a["spec_generator"], "spec": spec_rel,
                     "spec_key": f"{tr['anchor_id']}.{q['variant']}",
                     "pair_of_key": f"{tr['anchor_id']}.A" if q["key"] == "CF" else None,
                     "operations": ops},
        "note_zh": note,
        "explanation": q["explanation"],
        "review": None,
        "auto_checks": q["flags"],
    }


def anchor_md(a):
    tr, sp, src = a["tr"], a["sp"], a["src"]
    L = [f"# {tr['anchor_id']} · {tr['sample_id']} · {tr['speaker']} · {tr['trigger']}", "",
         f"- 类别：{tr['schema_category']}（{tr['trigger_type']}）",
         f"- 情绪：原标注 `{tr['target_emotion']}`（{tr['emotion_note']}）→ 本题 `{a['emotion']}`",
         f"- 历史窗口：S01–S{tr['target_session'] - 1:02d}；X 作为新的 S{tr['target_session']:02d} 接在后面"
         f"（原对话 S{tr['target_session']:02d} 及以后不进入题目）",
         f"- 生成：X 文本沿用原 {', '.join(sorted({q['variant'] for q in a['questions']}))} 变体；候选线索、反事实题、审查提示由 Claude 补齐",
         "", "## 锚点证据", ""]
    for t in a["required"]:
        L.append(f"- **必需** `{t}` {src[t]['speaker']}: {src[t]['text']}")
    for t in a["supporting"]:
        L.append(f"- 辅助 `{t}` {src[t]['speaker']}: {src[t]['text']}")
    if sp.get("skip_cf"):
        L += ["", f"> 未出“I-无历史关联”题：{sp['skip_cf']}"]
    L += ["", "## 审查提示", ""] + [f"- {n}" for n in sp["review_notes_zh"]] + [""]
    for q in a["questions"]:
        L += ["---", "", f"## {q['qid']} · {q['type_zh']}" + ("（由 A 派生）" if q["qid"].endswith("CF") else f"（原变体 {q['variant']}）"), ""]
        L.append("**历史**：" + (f"S01–S{q['hist_len']:02d} 完整" if not q["removed"] else
                 f"删除 {', '.join(q['removed'])} 后重新编号，剩 {q['hist_len']} 个 session"))
        t = q["x"]["target_turn"]
        L += ["", f"**当前情境（{q['x']['session_id']}，{q['x']['date']}）**", "",
              f"> `{t['turn_id']}` **{t['speaker']}（目标轮）**: {t['text']}", "", "**候选线索**", ""]
        for o in q["options"]:
            mark = " ✔" if o["cue_id"] == q["gold_cue"] else ""
            L.append(f"- `{o['cue_id']}` {o['name']}　*（{ROLE_ZH[q['roles'][o['cue_id']]]}）*{mark}")
        L += ["", f"**gold**：`{q['label']}`" + (f"（{q['i_subtype']}）" if q["i_subtype"] else "") +
              f" · cue `{q['gold_cue']}` · 证据 " + (", ".join(q["evidence"]) or "无") +
              (f"（辅助：{', '.join(q['supporting'])}）" if q["supporting"] else "") + f" · 情绪 `{q['emotion']}`", ""]
        expl = q["explanation"] if not q["qid"].endswith("CF") else \
            f"与 {tr['anchor_id']}-H 的 X 和选项完全相同，但历史中删除了证据所在的 {', '.join(q['removed'])}：有反应，当下解释不了，历史里也找不到关联。"
        L += [f"**解释**：{expl}", "", "**自动检查**：" + ("通过" if not q["flags"] else "")]
        L += [f"- ⚠ {f}" for f in q["flags"]] + [""]
    return "\n".join(L)


def main():
    spec_path = Path(sys.argv[1]).resolve()
    spec = json.load(open(spec_path))
    triples = [json.loads(l) for l in open(ROOT / spec["source_file"]) if l.strip()]
    cache, anchors = {}, []
    for tr in triples:
        conv = tr["sample_id"]
        cache.setdefault(conv, load_locomo_sessions(conv))
        anchors.append(build_anchor(tr, spec["anchors"][tr["anchor_id"]], cache[conv]))

    OUT.mkdir(parents=True, exist_ok=True)
    spec_rel = str(spec_path.relative_to(ROOT))
    by_conv = {}
    for a in anchors:
        a["spec_generator"] = spec["generator"]
        by_conv.setdefault(a["tr"]["sample_id"], []).append(a)
    for conv, group in by_conv.items():
        (OUT / conv).mkdir(exist_ok=True)
        samples = [to_sample(q, a, spec_rel) for a in group for q in a["questions"]]
        eval_format.write(OUT / conv / f"{conv}.json", samples, cache[conv][-1]["session_id"])
    rows = []
    for a in anchors:
        tr = a["tr"]
        d = OUT / tr["sample_id"]
        d.mkdir(exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", tr["trigger"].lower()).strip("-")
        name = f"{tr['anchor_id']}_{tr['speaker']}_{slug}.md"
        (d / name).write_text(anchor_md(a))
        if "--prompts" in sys.argv:
            for q in a["questions"]:
                (d / f"{q['qid']}_prompt_full_history.txt").write_text(q["prompt"] + "\n")
        for q in a["questions"]:
            rows.append((tr["sample_id"], f"{tr['sample_id']}/{name}", q))

    labs = [q["label"] for _, _, q in rows]
    L = ["# golden_triples 评测题目", "",
         f"由 `tools/build_from_triples.py` 根据 `{spec['source_file']}` 和 "
         f"`{spec_path.relative_to(ROOT)}` 生成。每个对话一个文件夹，同一对话的锚点放在同一个文件夹里。", "",
         "每个文件夹里有：每个锚点一个 Markdown（便于阅读）；`<conv_id>.json` 是评测题目，格式与 eval.json 相同；"
         "`<conv_id>.meta.json` 按 sample_id 记录主文件放不下的信息，其中 `history_filter` 评测时必须用到："
         "`until_session` 表示历史只取到该 session 为止，`exclude_sessions` 表示要从历史中删掉的 session"
         "（“I-无历史关联”题）。", "",
         f"共 {len(anchors)} 个锚点、{len(rows)} 题：H {labs.count(LABEL['H'])} / P {labs.count(LABEL['P'])} / "
         f"I {labs.count(LABEL['I'])}（其中近错 {sum(q['i_subtype'] == 'near_miss' for _, _, q in rows)}、"
         f"无历史关联 {sum(q['i_subtype'] == 'no_history_link' for _, _, q in rows)}）。", "",
         "| 题号 | 对话 | 类型 | gold | 情绪 | 自动检查 | 文件 |", "|---|---|---|---|---|---|---|"]
    for conv, path, q in rows:
        L.append(f"| {q['qid']} | {conv} | {q['type_zh']} | {q['label']} | {q['emotion']} | "
                 f"{'⚠ ' + str(len(q['flags'])) if q['flags'] else '通过'} | [{path.split('/')[1]}]({path}) |")
    (OUT / "README.md").write_text("\n".join(L) + "\n")
    print(f"{len(anchors)} anchors, {len(rows)} questions -> {OUT.relative_to(ROOT)}  "
          f"H/P/I = {labs.count(LABEL['H'])}/{labs.count(LABEL['P'])}/{labs.count(LABEL['I'])}")
    for conv, path, q in rows:
        for f in q["flags"]:
            print(f"  {q['qid']} {f}")
    for a in anchors:
        if a["sp"].get("skip_cf"):
            print(f"  {a['tr']['anchor_id']} skip_cf; residual after removing {a['cf_removed']}: {a['residual'][:12]}")


if __name__ == "__main__":
    main()
