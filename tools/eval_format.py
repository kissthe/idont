"""把内部题目结构转成评测格式（与 eval.json 相同），并输出 meta 旁注文件。

评测格式（主文件，JSON 数组）每题只有：
    sample_id, user_id, current_input{input_type, text, image_refs, cue_options[{cue_id, name, cue_type}]},
    gold{label, gold_cue_id, evidence_turn_ids, evidence_session_ids, current_emotion}
主文件放不下、但评测或分析需要的信息写进 <name>.meta.json（按 sample_id 索引）：
    history_filter：until_session（只用到这个 session 为止的历史；null 表示全部）、
                    exclude_sessions（需要从历史中删掉的 session，“I-无历史关联”题用）
    sample_type / i_subtype / option_roles / supporting_turn_ids / context_turns / note_zh 等。
"""
import json
import re

# 当下的事件、情境用 event / scene，其余（物件、动物、人以外的实物）用 object
SCENE = {
    "the spot by the water", "mom's old house", "the hospital waiting room", "the coworkers taking selfies",
    "the empty court", "a woman by the window of a house like mom's",
}
EVENT = {
    "the snake missing from its tank", "the moving boxes", "the contractor's phone call", "being late for the workshop",
    "the man who cut in line", "the yoga group leaving for coffee", "the hardware store errands", "the phone thief",
    "the submission deadline tonight", "the neighbor's barking dog", "the student's fall", "the moved-up deadline",
    "the competition announcement", "her sister's call about flying in next week", "the dance class she was heading to",
    "getting accepted into the regional dance showcase", "getting turned down for the loan",
    "hitting ten three-pointers in a row", "Pixie finally learning to roll over",
    "booking the community studio for the weekend", "her mom's birthday",
    "finding a box of her mother's unopened letters", "the rough week of health problems",
    "the doctor saying his tests came back clear", "getting booked for the Boston show",
}
NONE_NAME = "无可支持的历史线索"
STOP = {"the", "a", "an", "her", "his", "of", "on", "in", "at", "for", "from", "to", "by", "with", "she", "he", "was", "own"}
EMOTION = {"not_expressed": "none_observed"}


def cue_type(name):
    return "scene" if name in SCENE else "event" if name in EVENT else "object"


def slug(name, n=3):
    words = [w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in STOP]
    return "_".join(words[:n]) or "cue"


def to_locomo_turn(tid):
    s, t = tid.split("_")
    return f"D{int(s[1:])}:{int(t[1:])}"


def to_locomo_session(sid):
    return f"session_{int(sid[1:])}"


def convert(sample, last_session):
    """sample：内部结构（data/questions/locomo/conv-48.json 旧版里的一题）。返回 (评测题, meta)。"""
    user = sample["eval_user_id"].lower()
    roles = sample["gold"]["option_roles"]
    ids, options = {}, []
    for o in sample["current_input"]["cue_options"]:
        if o["cue_id"] == "none":
            ids["none"] = "none"
            options.append({"cue_id": "none", "name": NONE_NAME, "cue_type": "none"})
            continue
        base = f"cue_{user}_{slug(o['name'])}" if roles[o["cue_id"]] != "present_salient" else f"cue_{slug(o['name'])}"
        cid, k = base, 2
        while cid in ids.values():
            cid, k = f"{base}_{k}", k + 1
        ids[o["cue_id"]] = cid
        options.append({"cue_id": cid, "name": o["name"], "cue_type": cue_type(o["name"])})
    g, h = sample["gold"], sample["history"]
    item = {
        "sample_id": sample["sample_id"],
        "user_id": f"u_{user}",
        "current_input": {"input_type": "text", "text": sample["current_input"]["target_turn"]["text"],
                          "image_refs": sample["current_input"]["image_refs"], "cue_options": options},
        "gold": {"label": g["label"], "gold_cue_id": ids[g["gold_cue_id"]],
                 "evidence_turn_ids": [to_locomo_turn(t) for t in g["evidence_turn_ids"]],
                 "evidence_session_ids": [to_locomo_session(s) for s in g["evidence_session_ids"]],
                 "current_emotion": EMOTION.get(g["current_emotion"], g["current_emotion"])},
    }
    meta = {
        "conv_id": h["conv_id"], "source": h["source"],
        "family_id": sample["family_id"], "sample_type": sample["sample_type"], "i_subtype": g["i_subtype"],
        "history_filter": {
            "until_session": None if h["cutoff_after"] == last_session else to_locomo_session(h["cutoff_after"]),
            "exclude_sessions": [to_locomo_session(s) for s in h["removed_sessions"]],
        },
        "option_roles": {ids[c]: r for c, r in roles.items()},
        "supporting_turn_ids": [to_locomo_turn(t) for t in g.get("supporting_turn_ids", [])],
        "context_turns": [{"speaker": t["speaker"], "text": t["text"]} for t in sample["current_input"]["context_turns"]],
        "x_date": sample["current_input"]["date"],
        "confidence": g["confidence"],
        "note_zh": sample.get("note_zh", ""),
        "explanation": sample.get("explanation"),
        "edit_log": sample["edit_log"],
        "auto_checks": sample.get("auto_checks", []),
    }
    return item, meta


def write(path, samples, last_session):
    """path：评测主文件路径（.json）；同目录写 <stem>.meta.json。"""
    items, metas = [], {}
    for s in samples:
        item, meta = convert(s, last_session)
        items.append(item)
        metas[item["sample_id"]] = meta
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    with open(path.with_name(path.stem + ".meta.json"), "w") as f:
        json.dump(metas, f, ensure_ascii=False, indent=2)
    return items
