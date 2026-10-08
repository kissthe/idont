"""由审查通过的锚点 + 生成 spec 组装评测题目。

用法：
    python tools/build_questions.py data/questions/specs/conv-48.spec.json

spec 里是需要 LLM（或人）写的内容：第①步 H 可用性筛查、第②步每道题的 X 文本与干扰项选择。
本脚本只做确定性的部分：截断/删除历史、组装候选线索与 gold、自动校验、渲染模型输入。
"""
import json
import random
import re
import sys
from pathlib import Path

import eval_format

ROOT = Path(__file__).resolve().parent.parent
LOCOMO = ROOT / "locomo10.json"

LABEL = {"H": "history_supported", "P": "present_cause_sufficient", "I": "insufficient_evidence"}
TYPE_LABEL = {"H": "H", "I_no_history": "I", "I_no_reaction": "I", "P_same_cue": "P", "P_unrelated": "P"}
I_SUBTYPE = {"I_no_history": "no_history_link", "I_no_reaction": "no_reaction"}
EMOTIONS = ["sadness", "fear", "anxiety", "anger", "nostalgia", "comfort", "joy", "guilt", "not_expressed"]

LEAK_RE = re.compile(
    r"\b(remember\w*|remind\w*|used to|back when|ever since|last time|years ago|mom|mother|mum|dad|father|"
    r"grandma|passed away|funeral|died|late (?:mother|mom|father|dad|friend))\b", re.I)


# ---------- 历史 ----------

def load_locomo_sessions(conv_id):
    conv = {x["sample_id"]: x for x in json.load(open(LOCOMO))}[conv_id]["conversation"]
    nums = sorted(int(k.split("_")[1]) for k in conv if re.fullmatch(r"session_\d+", k))
    sessions = []
    for n in nums:
        turns = []
        for t in conv[f"session_{n}"]:
            s, i = t["dia_id"][1:].split(":")
            turns.append({"turn_id": f"S{int(s):02d}_T{int(i):02d}", "speaker": t["speaker"],
                          "text": t["text"], "image_caption": t.get("blip_caption")})
        sessions.append({"session_id": f"S{n:02d}", "date": conv[f"session_{n}_date_time"], "turns": turns})
    return sessions


def sid(turn_id):
    return turn_id.split("_")[0]


def build_history(sessions, removed):
    """删掉 removed 中的 session 并重新编号，返回 (新历史, 旧→新 session 映射)。"""
    kept = [s for s in sessions if s["session_id"] not in removed]
    id_map = {s["session_id"]: f"S{i:02d}" for i, s in enumerate(kept, 1)}
    out = []
    for s in kept:
        new = id_map[s["session_id"]]
        out.append({**s, "session_id": new,
                    "turns": [{**t, "turn_id": new + "_" + t["turn_id"].split("_")[1]} for t in s["turns"]]})
    return out, (id_map if removed else None)


def history_for(sample, sessions):
    """按 cutoff_after 截断、删除 removed_sessions 并重新编号，得到这道题的历史。"""
    h = sample["history"]
    window = [s for s in sessions if s["session_id"] <= h["cutoff_after"]]
    return build_history(window, set(h["removed_sessions"]))[0]


# ---------- 组装 ----------

def anchor_turns(a):
    return sorted(set(a["fact"]["turn_ids"]) | set(a["meaning"]["turn_ids"]))


def anchor_sessions(a):
    return sorted({sid(t) for t in anchor_turns(a)})


def make_options(seed, entries):
    """entries: [(name, role, event_id or None)]；非 none 的三项按 seed 固定打乱，none 放最后。"""
    rng = random.Random(seed)
    entries = entries[:]
    rng.shuffle(entries)
    options, roles, ids = [], {}, {}
    for i, (name, role, eid) in enumerate(entries, 1):
        cid = f"c{i}"
        options.append({"cue_id": cid, "name": name})
        roles[cid] = role
        ids[cid] = eid
    options.append({"cue_id": "none", "name": "none of these (no supporting cue in the history)"})
    roles["none"] = "none"
    return options, roles, ids


def build(spec_path):
    spec = json.load(open(spec_path))
    anchors = {e["event_id"]: e for e in json.load(open(ROOT / spec["anchor_file"]))["events"]}
    sessions = load_locomo_sessions(spec["conv_id"])
    last = sessions[-1]["session_id"]
    names = spec["cue_names"]
    by_key = {s["key"]: s for s in spec["samples"]}

    samples = []
    for n, s in enumerate(spec["samples"], 1):
        sample_id = f"{spec['conv_id']}_q{n:02d}"
        base = by_key[s["pair_of"]] if s["type"] == "I_no_history" else s
        anchor = anchors.get(base.get("anchor"))
        user = anchor["user_id"] if anchor else base["user"]
        other = base["context"][0]["speaker"]

        removed = anchor_sessions(anchors[s["remove_sessions_of"]]) if s["type"] == "I_no_history" else []
        hist, id_map = build_history(sessions, set(removed))
        x_sid = f"S{len(hist) + 1:02d}"

        ctx = [{"turn_id": f"{x_sid}_T{i:02d}", "speaker": c["speaker"], "text": c["text"]}
               for i, c in enumerate(base["context"], 1)]
        target = {"turn_id": f"{x_sid}_T{len(ctx) + 1:02d}", "speaker": user, "text": base["target"]}

        if s["type"] == "P_unrelated":
            entries = [(base["present_cue"], "present_salient", None)] + \
                      [(names[l], "history_unrelated", l) for l in base["lures"]]
        else:
            entries = [(names[base["anchor"]], "anchor", base["anchor"]),
                       (names[base["lures"][0]], "history_unrelated", base["lures"][0]),
                       (base["present_cue"], "present_salient", None)]
        # 成对的题（H 与它的无历史关联题）用同一个种子，选项顺序完全一致
        options, roles, opt_events = make_options(f"{spec['conv_id']}_{base['key']}", entries)

        lab = TYPE_LABEL[s["type"]]
        if s["type"] == "H":
            ev = [t for t in anchor_turns(anchor) if sid(t) < x_sid]
            gold_cue = next(c for c, r in roles.items() if r == "anchor")
            emotion = anchor["meaning"]["emotion"]
        else:
            ev, gold_cue = [], "none"
            if s["type"] == "I_no_history":   # X 与 H 相同，反应仍在，只是失去了历史依据
                emotion = anchor["meaning"]["emotion"]
            elif s["type"] == "I_no_reaction":
                emotion = "not_expressed"
            else:
                emotion = base["emotion"]

        ops = [f"truncate_after:{last}"]
        if removed:
            ops.append("remove_sessions:" + ",".join(removed) + "; renumber")
        ops.append(f"add_session:{x_sid}")
        samples.append({
            "sample_id": sample_id,
            "family_id": base.get("anchor") or f"{spec['conv_id']}_{user.lower()}_unrelated",
            "sample_type": s["type"],
            "eval_user_id": user,
            "history": {"conv_id": spec["conv_id"], "source": spec["source"], "cutoff_after": last,
                        "removed_sessions": removed, "session_id_map": id_map},
            "current_input": {"session_id": x_sid, "date": spec["x_session_date"],
                              "context_turns": ctx, "target_turn": target,
                              "image_refs": [], "cue_options": options},
            "gold": {"label": LABEL[lab], "i_subtype": I_SUBTYPE.get(s["type"]),
                     "gold_cue_id": gold_cue, "evidence_turn_ids": ev,
                     "evidence_session_ids": sorted({sid(t) for t in ev}),
                     "current_emotion": emotion, "option_roles": roles,
                     "confidence": base.get("confidence", 2)},
            "edit_log": {"generator": spec["generator"], "spec": str(Path(spec_path).relative_to(ROOT)),
                         "spec_key": s["key"], "pair_of_key": s.get("pair_of"), "operations": ops},
            "note_zh": s.get("note_zh", ""),
            "review": None,
            "_opt_events": opt_events,
        })
    return spec, anchors, sessions, samples


# ---------- 自动校验（只做标记） ----------

def ngrams(text, n=5):
    w = re.findall(r"[a-z']+", text.lower())
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def check(spec, anchors, sessions, samples):
    for s in samples:
        flags = []
        hist = history_for(s, sessions)
        turn_ids = {t["turn_id"] for se in hist for t in se["turns"]}
        g, ci = s["gold"], s["current_input"]
        opt_ids = [o["cue_id"] for o in ci["cue_options"]]
        # 结构
        if len(opt_ids) != 4 or "none" not in opt_ids:
            flags.append("结构：候选线索不是 4 个或缺少 none")
        if g["gold_cue_id"] not in opt_ids:
            flags.append("结构：gold 线索不在选项中")
        if (g["label"] == LABEL["H"]) != (g["gold_cue_id"] != "none") or (g["label"] == LABEL["H"]) != bool(g["evidence_turn_ids"]):
            flags.append("结构：标签与线索/证据不一致")
        if any(t not in turn_ids or sid(t) >= ci["session_id"] for t in g["evidence_turn_ids"]):
            flags.append("结构：证据不在截断前的历史中")
        if g["current_emotion"] not in EMOTIONS:
            flags.append("结构：情绪不在枚举内")
        for cid, eid in s["_opt_events"].items():
            if eid and s["gold"]["option_roles"][cid] == "history_unrelated":
                if not set(anchor_sessions(anchors[eid])) - set(s["history"]["removed_sessions"]):
                    flags.append(f"结构：干扰项 {cid} 的证据已被删除")
        # 泄漏
        texts = [ci["target_turn"]["text"]] + [c["text"] for c in ci["context_turns"]] + [o["name"] for o in ci["cue_options"]]
        hits = sorted({m.group(0).lower() for t in texts for m in LEAK_RE.finditer(t)})
        if hits:
            flags.append("泄漏词：" + ", ".join(hits))
        fam = anchors.get(s["family_id"])
        if fam and s["sample_type"] != "I_no_history":   # 反事实题的证据已删除，不做重合检查
            src = {t["turn_id"]: t["text"] for se in sessions for t in se["turns"]}
            ev_text = " ".join(src[t] for t in anchor_turns(fam)) + " " + fam["fact"]["text"]
            ov = ngrams(ci["target_turn"]["text"]) & ngrams(ev_text)
            if ov:
                flags.append("与历史原文重合的 5 词片段：" + " | ".join(sorted(ov)))
        # 反事实：剩余历史中是否还残留线索
        if s["sample_type"] == "I_no_history":
            kws = spec["cue_keywords"].get(s["family_id"], [])
            res = [f"{t['turn_id']}({t['speaker']})" for se in hist for t in se["turns"]
                   if any(re.search(rf"\b{re.escape(k)}\b", t["text"], re.I) for k in kws)]
            if res:
                flags.append("反事实残留（需人工确认与本题无关）：" + ", ".join(res))
        s["auto_checks"] = flags
    # 成对检查
    by_key = {s["edit_log"]["spec_key"]: s for s in samples}
    for s in samples:
        if s["sample_type"] == "I_no_history":
            h = by_key[s["edit_log"]["pair_of_key"]]
            if h["current_input"]["target_turn"]["text"] != s["current_input"]["target_turn"]["text"] or \
               [o["name"] for o in h["current_input"]["cue_options"]] != [o["name"] for o in s["current_input"]["cue_options"]]:
                s["auto_checks"].append("成对：与对应 H 题的 X 或选项不一致")


# ---------- 渲染模型输入 ----------

INSTRUCTIONS = """You will read the long-term conversation history of a user named {u} and a current situation.
Decide whether {u}'s reaction in the target turn can only be explained by {u}'s personal history.

Labels:
- history_supported: {u} shows a reaction (an emotion, a bodily reaction, a change in behavior, or longing); the present situation alone cannot explain the type or intensity of this reaction; and {u}'s own earlier statements in the history connect one cue in the current situation with this feeling.
- present_cause_sufficient: {u} shows a reaction, and the present situation alone is enough for an ordinary person to react the same way with the same intensity.
- insufficient_evidence: neither of the above, e.g. {u} shows no reaction, or no specific connection can be found in the history.

Answer fields:
- label: one of the three labels above.
- cue_id: the candidate cue that triggers the reaction; "none" unless label is history_supported.
- evidence_turn_ids: turn IDs from the history that support the connection; [] unless label is history_supported.
- current_emotion: one of {emos}.

Output only a JSON object:
{{"label": "...", "cue_id": "...", "evidence_turn_ids": ["..."], "current_emotion": "..."}}"""


def render_turn(t):
    img = f" [shares an image: {t['image_caption']}]" if t.get("image_caption") else ""
    return f"[{t['turn_id']}] {t['speaker']}: {t['text']}{img}"


def condition_sessions(s, hist, anchors):
    if s["gold"]["evidence_session_ids"]:
        keep = set(s["gold"]["evidence_session_ids"])
    elif s["family_id"] in anchors:   # 非 H 题：给锚点所在的 session（诱饵历史），已被删除的就没有
        keep = set(anchor_sessions(anchors[s["family_id"]])) - set(s["history"]["removed_sessions"])
    else:
        keep = set()
    return [se for se in hist if se["session_id"] in keep]


def render_prompt(s, sessions, anchors, condition):
    """condition: x_only | full_history | evidence_only"""
    hist = history_for(s, sessions)
    if condition == "x_only":
        hist = []
    elif condition == "evidence_only":
        hist = condition_sessions(s, hist, anchors)
    u, ci = s["eval_user_id"], s["current_input"]
    parts = [INSTRUCTIONS.format(u=u, emos=", ".join(EMOTIONS)), "", "## Conversation history"]
    if not hist:
        parts.append("(no history provided)")
    for se in hist:
        parts += ["", f"### {se['session_id']} ({se['date']})"] + [render_turn(t) for t in se["turns"]]
    parts += ["", f"## Current situation: {ci['session_id']} ({ci['date']})"]
    parts += [render_turn(t) for t in ci["context_turns"]]
    parts.append(render_turn(ci["target_turn"]) + "   <- target turn")
    parts += ["", "## Candidate cues"] + [f"- {o['cue_id']}: {o['name']}" for o in ci["cue_options"]]
    return "\n".join(parts)


# ---------- 输出 ----------

TYPE_ZH = {"H": "H", "I_no_history": "I-无历史关联", "I_no_reaction": "I-无反应",
           "P_same_cue": "P-同线索", "P_unrelated": "P-无关"}


def preview(spec, anchors, sessions, samples, prompt_file):
    src = {t["turn_id"]: t for se in sessions for t in se["turns"]}
    labs = [s["gold"]["label"] for s in samples]
    L = [f"# 题目预览：{spec['conv_id']}", "",
         "由 `tools/build_questions.py` 根据 `" + spec["anchor_file"] + "` 和 `" +
         str(Path(spec["_path"]).relative_to(ROOT)) + "` 生成。",
         "", "> 演示说明：锚点文件按“已审查通过”处理，未做修改；第①步筛查和第②步的 X 文本由 Claude 代替生成模型手写。"
         "题目尚未经过人工审题。", "",
         f"共 {len(samples)} 题：H {labs.count(LABEL['H'])} / P {labs.count(LABEL['P'])} / I {labs.count(LABEL['I'])}。"
         f"第①步筛查中 51 个锚点里有 {sum(v[0] for v in spec['h_screen'].values())} 个可以出 H 题。", "",
         "| 题号 | 类型 | 用户 | 锚点 | gold 标签 | 情绪 | 自动检查 |", "|---|---|---|---|---|---|---|"]
    for s in samples:
        L.append(f"| {s['sample_id'][-3:]} | {TYPE_ZH[s['sample_type']]} | {s['eval_user_id']} | "
                 f"{s['family_id'].split('_', 1)[1] if s['family_id'] in anchors else '—'} | "
                 f"{s['gold']['label']} | {s['gold']['current_emotion']} | {'⚠ ' + str(len(s['auto_checks'])) if s['auto_checks'] else '通过'} |")
    L += ["", "---", ""]
    for s in samples:
        ci, g = s["current_input"], s["gold"]
        fam = anchors.get(s["family_id"])
        L.append(f"## {s['sample_id'][-3:]} · {TYPE_ZH[s['sample_type']]} · {s['eval_user_id']}")
        L.append("")
        if fam:
            L.append(f"**锚点** `{fam['event_id']}`：{fam['cue']}（{fam['meaning']['emotion']}）")
            for t in anchor_turns(fam)[:3]:
                txt = src[t]["text"]
                L.append(f"- `{t}` {txt[:140]}{'…' if len(txt) > 140 else ''}")
            if len(anchor_turns(fam)) > 3:
                L.append(f"- …共 {len(anchor_turns(fam))} 轮")
        h = s["history"]
        L.append("")
        L.append("**历史**：" + (f"S01–{h['cutoff_after']} 完整" if not h["removed_sessions"] else
                 f"删除 {', '.join(h['removed_sessions'])} 后重新编号，剩 {len(history_for(s, sessions))} 个 session"))
        L += ["", f"**当前情境 X（{ci['session_id']}）**", ""]
        for t in ci["context_turns"]:
            L.append(f"> `{t['turn_id']}` {t['speaker']}: {t['text']}")
            L.append(">")
        L.append(f"> `{ci['target_turn']['turn_id']}` **{ci['target_turn']['speaker']}（目标轮）**: {ci['target_turn']['text']}")
        L += ["", "**候选线索**", ""]
        role_zh = {"anchor": "锚点线索", "history_unrelated": "历史中出现但无关", "present_salient": "当下显眼",
                   "none": "无"}
        for o in ci["cue_options"]:
            mark = " ✔" if o["cue_id"] == g["gold_cue_id"] else ""
            L.append(f"- `{o['cue_id']}` {o['name']}　*（{role_zh[g['option_roles'][o['cue_id']]]}）*{mark}")
        L += ["", f"**gold**：`{g['label']}`" + (f"（{g['i_subtype']}）" if g["i_subtype"] else "") +
              f" · cue `{g['gold_cue_id']}` · 证据 {', '.join(g['evidence_turn_ids']) or '无'} · "
              f"情绪 `{g['current_emotion']}` · confidence {g['confidence']}",
              "", f"**说明**：{s['note_zh']}", ""]
        L.append("**自动检查**：" + ("通过" if not s["auto_checks"] else ""))
        for f in s["auto_checks"]:
            L.append(f"- ⚠ {f}")
        L += ["", "---", ""]
    ex = samples[0]
    L += ["## 被测模型实际看到的输入", "",
          f"以 {ex['sample_id'][-3:]} 为例。三种输入条件只差在 `## Conversation history` 部分："
          "只给 X 时为空；给完整历史时是全部 session（完整版见 `" + prompt_file + "`）；只给证据时只保留证据所在的 session。"
          "`gold`、`option_roles`、`sample_type` 等字段都不会给模型看。", "",
          "**只给 X：**", "", "```text", render_prompt(ex, sessions, anchors, "x_only"), "```", "",
          "**只给证据 session（只列历史部分）：**", "", "```text"]
    ev = render_prompt(ex, sessions, anchors, "evidence_only")
    L += [ev[ev.index("## Conversation history"):ev.index("## Current situation")].rstrip(), "```", ""]
    return "\n".join(L)


def main():
    spec_path = Path(sys.argv[1]).resolve()
    spec, anchors, sessions, samples = build(spec_path)
    spec["_path"] = str(spec_path)
    check(spec, anchors, sessions, samples)
    conv = spec["conv_id"]
    prompt_file = f"docs/examples/prompt_{conv}_q01_full_history.txt"
    (ROOT / prompt_file).parent.mkdir(parents=True, exist_ok=True)
    (ROOT / prompt_file).write_text(render_prompt(samples[0], sessions, anchors, "full_history") + "\n")
    (ROOT / f"docs/examples/题目预览_{conv}.md").write_text(preview(spec, anchors, sessions, samples, prompt_file))
    out = ROOT / f"data/questions/{spec['source']}/{conv}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    for s in samples:
        del s["_opt_events"]
    eval_format.write(out, samples, sessions[-1]["session_id"])   # 评测格式 + <conv>.meta.json
    labs = [s["gold"]["label"] for s in samples]
    print(f"{len(samples)} samples -> {out.relative_to(ROOT)}  H/P/I = "
          f"{labs.count(LABEL['H'])}/{labs.count(LABEL['P'])}/{labs.count(LABEL['I'])}")
    for s in samples:
        for f in s["auto_checks"]:
            print(f"  {s['sample_id']} [{s['sample_type']}] {f}")


if __name__ == "__main__":
    main()
