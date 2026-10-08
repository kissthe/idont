"""渲染评测输入（docs/评测方案.md 第 2–4 节）。

用法：
    python tools/run_eval.py render <run_id> [题目文件 ...]
        把每道题按三种条件渲染成模型输入，写到 results/<run_id>/prompts/<condition>/<sample_id>.txt，
        并写 results/<run_id>/manifest.jsonl。不传题目文件时，使用 data/questions 下全部题目。

    python tools/run_eval.py batch <run_id>
        把历史相同的题合成批次文件（results/<run_id>/batches/），供一次读入多题作答。
    python tools/run_eval.py collect <run_id>
        把 results/<run_id>/answers/*.jsonl 汇总成 predictions.jsonl。

模型作答后，把回复按 {"sample_id", "condition", "response"} 一行一个写进
results/<run_id>/predictions.jsonl，再用 tools/score.py 打分。
"""
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCOMO = ROOT / "locomo10.json"
CONDITIONS = ["x_only", "full_history", "evidence_only"]
EMOTIONS = ["sadness", "fear", "anxiety", "anger", "nostalgia", "comfort", "joy", "guilt", "none_observed"]

INSTRUCTIONS = """You will read the long-term conversation history between {u} and {o}, followed by a current situation in which {u} speaks.
Decide whether {u}'s reaction in the target turn can only be explained by {u}'s own personal history.

What counts as a reaction: an explicitly stated emotion, a bodily reaction (e.g. hands freezing, eyes welling up), a change in behavior (e.g. stopping, avoiding, checking repeatedly), or clear longing. Merely mentioning something is not a reaction.

Labels:
- history_supported: {u} shows a reaction; the present situation alone cannot explain the type or intensity of this reaction; AND {u}'s own earlier statements in the history specifically connect one cue in the current situation with this feeling.
- present_cause_sufficient: {u} shows a reaction, and the present situation alone is enough for an ordinary person to react the same way with the same intensity. Related things mentioned in the history do not change this.
- insufficient_evidence: neither of the above. For example, {u} shows no reaction; or the reaction cannot be explained by the present situation and no specific connection can be found in the history; or the history only mentions something similar whose details do not match.

Answer fields:
- label: one of history_supported, present_cause_sufficient, insufficient_evidence.
- cue_id: the id of the candidate cue that triggers the reaction; "none" unless label is history_supported.
- evidence_turn_ids: IDs of {u}'s own turns in the history (e.g. "D4:28") that establish the connection; [] unless label is history_supported. Only turns that appear before the current situation may be used.
- current_emotion: {u}'s emotion in the target turn, one of {emos}.

Output only one JSON object and nothing else:
{{"label": "...", "cue_id": "...", "evidence_turn_ids": [], "current_emotion": "..."}}"""


def question_files(args):
    if args:
        return [Path(a).resolve() for a in args]
    return sorted(Path(p).resolve() for p in glob.glob(str(ROOT / "data/questions/**/*.json"), recursive=True)
                  if not p.endswith(".meta.json") and "/specs/" not in p)


def load_questions(files):
    """返回 [(题, meta, 同文件全部题)]。"""
    out = []
    for f in files:
        items = json.load(open(f))
        meta = json.load(open(f.with_name(f.stem + ".meta.json")))
        for it in items:
            out.append((it, meta[it["sample_id"]], items, meta))
    return out


_CONVS = {}


def conversation(conv_id):
    if not _CONVS:
        _CONVS.update({x["sample_id"]: x["conversation"] for x in json.load(open(LOCOMO))})
    return _CONVS[conv_id]


def history(meta):
    """按 history_filter 截取历史；删过 session 时重新编号。返回 [(显示用 session 号, 日期, [turn])]。"""
    conv = conversation(meta["conv_id"])
    hf = meta["history_filter"]
    until = int(hf["until_session"].split("_")[1]) if hf["until_session"] else None
    excluded = {int(s.split("_")[1]) for s in hf["exclude_sessions"]}
    out, k, new = [], 1, 0
    while f"session_{k}" in conv:
        if (until is None or k <= until) and k not in excluded:
            new += 1
            n = new if excluded else k
            turns = [{**t, "dia_id": f"D{n}:{t['dia_id'].split(':')[1]}"} for t in conv[f"session_{k}"]]
            out.append((n, conv[f"session_{k}_date_time"], turns, k))
        k += 1
    return out


def evidence_sessions(item, meta, items, metas):
    """evidence_only 条件下给哪些（原始编号的）session；返回 None 表示不跑这个条件。"""
    g = item["gold"]
    if g["label"] == "history_supported":
        return {int(s.split("_")[1]) for s in g["evidence_session_ids"]}
    if meta["sample_type"] in ("I_no_history", "P_unrelated"):
        return None
    for other in items:
        m = metas[other["sample_id"]]
        if m["family_id"] == meta["family_id"] and other["gold"]["label"] == "history_supported":
            return {int(s.split("_")[1]) for s in other["gold"]["evidence_session_ids"]}
    return None


def render(item, meta, items, metas, condition):
    conv = conversation(meta["conv_id"])
    u = item["user_id"][2:].capitalize()
    u = next(conv[k] for k in ("speaker_a", "speaker_b") if conv[k].lower() == u.lower())
    o = conv["speaker_b"] if conv["speaker_a"] == u else conv["speaker_a"]
    sessions = history(meta)
    if condition == "x_only":
        sessions = []
    elif condition == "evidence_only":
        keep = evidence_sessions(item, meta, items, metas)
        if keep is None:
            return None
        sessions = [s for s in sessions if s[3] in keep]
    parts = [INSTRUCTIONS.format(u=u, o=o, emos=", ".join(EMOTIONS)), "", "## Conversation history"]
    if not sessions:
        parts.append("(no history provided)")
    for n, date, turns, _ in sessions:
        parts += ["", f"### session_{n} ({date})"]
        for t in turns:
            img = f" [shares an image: {t['blip_caption']}]" if t.get("blip_caption") else ""
            parts.append(f"[{t['dia_id']}] {t['speaker']}: {t['text']}{img}")
    parts += ["", f"## Current situation ({meta['x_date']})"]
    parts += [f"{c['speaker']}: {c['text']}" for c in meta["context_turns"]]
    parts.append(f"{u}: {item['current_input']['text']}   <- target turn")
    parts += ["", "## Candidate cues"] + [f"- {o_['cue_id']}: {o_['name']}" for o_ in item["current_input"]["cue_options"]]
    return "\n".join(parts) + "\n"


def cmd_render(run_id, files):
    out = ROOT / "results" / run_id
    rows = []
    for item, meta, items, metas in load_questions(question_files(files)):
        for cond in CONDITIONS:
            text = render(item, meta, items, metas, cond)
            if text is None:
                continue
            p = out / "prompts" / cond / f"{item['sample_id']}.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            rows.append({"sample_id": item["sample_id"], "condition": cond, "conv_id": meta["conv_id"],
                         "prompt_file": str(p.relative_to(ROOT)), "chars": len(text)})
    with open(out / "manifest.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    by = {c: sum(r["condition"] == c for r in rows) for c in CONDITIONS}
    print(f"{len(rows)} prompts -> {out.relative_to(ROOT)}  {by}")


# ---------- 分批作答（同一历史只读一次） ----------

BATCH_HEADER = """You are being evaluated on a benchmark. Each item below shows a current situation in which a target speaker talks, plus candidate cues. All items in this file share the conversation history shown once below{x_note}.
Judge EACH item independently, as if it were the only one: the items are unrelated test cases, labels are not balanced in any particular way, and you must not compare items with each other.

"""


def split_prompt(text):
    i, j = text.index("## Conversation history"), text.index("## Current situation")
    return text[:i], text[i:j], text[j:]


def cmd_batch(run_id, x_chunk=21):
    import hashlib
    import random
    out = ROOT / "results" / run_id
    rows = [json.loads(l) for l in open(out / "manifest.jsonl")]
    groups = {}
    for r in rows:
        text = (ROOT / r["prompt_file"]).read_text()
        instr, hist, item = split_prompt(text)
        key = (r["condition"], hashlib.md5(hist.encode()).hexdigest())
        groups.setdefault(key, {"condition": r["condition"], "history": hist, "items": []})
        groups[key]["items"].append((r["sample_id"], instr, item))
    batches, bdir = [], out / "batches"
    bdir.mkdir(parents=True, exist_ok=True)
    counter = {}
    for (cond, _), g in sorted(groups.items(), key=lambda kv: (CONDITIONS.index(kv[0][0]), -len(kv[1]["items"]))):
        items = g["items"]
        random.Random(f"{run_id}-{cond}-{items[0][0]}").shuffle(items)
        chunks = [items[i:i + x_chunk] for i in range(0, len(items), x_chunk)] if cond == "x_only" else [items]
        for chunk in chunks:
            counter[cond] = counter.get(cond, 0) + 1
            name = f"{cond}_{counter[cond]:02d}"
            # 各题的任务说明只差在人名，统一用第一题的说明，每题再写明目标说话人
            parts = [BATCH_HEADER.format(x_note=" (here: no history is provided)" if cond == "x_only" else ""),
                     "# Task instructions", "", chunk[0][1].strip(), "", g["history"].strip(), ""]
            ids = []
            for n, (sid, instr, item) in enumerate(chunk, 1):
                speaker = instr.split(" between ")[1].split(" and ")[0] if " between " in instr else ""
                item_id = f"{name}-{n:02d}"
                ids.append({"item_id": item_id, "sample_id": sid})
                parts += [f"=================== ITEM {item_id} ===================",
                          f"Target speaker: {speaker} (apply the task instructions with this person as the target)",
                          "", item.strip(), ""]
            parts.append(f"Write exactly {len(chunk)} answers, one JSON object per line, each with fields "
                         '"item_id", "label", "cue_id", "evidence_turn_ids", "current_emotion".')
            (bdir / f"{name}.txt").write_text("\n".join(parts) + "\n")
            batches.append({"batch": name, "condition": cond, "file": str((bdir / f"{name}.txt").relative_to(ROOT)),
                            "answers": f"results/{run_id}/answers/{name}.jsonl", "items": ids})
    json.dump(batches, open(out / "batches.json", "w"), ensure_ascii=False, indent=2)
    print(f"{len(batches)} batches -> {(out / 'batches').relative_to(ROOT)}")
    for b in batches:
        print(f"  {b['batch']}: {len(b['items'])} items, {len((ROOT / b['file']).read_text()) // 4} tokens")


def cmd_collect(run_id):
    """把 answers/*.jsonl 转成 predictions.jsonl（缺答的题写空回复，按解析失败计）。"""
    import re as _re
    out = ROOT / "results" / run_id
    batches = json.load(open(out / "batches.json"))
    n_ok = n_missing = 0
    with open(out / "predictions.jsonl", "w") as f:
        for b in batches:
            got = {}
            p = ROOT / b["answers"]
            if p.exists():
                for line in p.read_text().splitlines():
                    for raw in _re.findall(r"\{.*\}", line):
                        try:
                            obj = json.loads(raw)
                            got[obj.get("item_id")] = obj
                        except json.JSONDecodeError:
                            pass
            for it in b["items"]:
                obj = got.get(it["item_id"])
                n_ok += obj is not None
                n_missing += obj is None
                resp = json.dumps({k: v for k, v in obj.items() if k != "item_id"}) if obj else ""
                f.write(json.dumps({"sample_id": it["sample_id"], "condition": b["condition"], "response": resp}) + "\n")
    print(f"collected {n_ok} answers, {n_missing} missing -> results/{run_id}/predictions.jsonl")


if __name__ == "__main__":
    cmds = {"render": lambda a: cmd_render(a[0], a[1:]), "batch": lambda a: cmd_batch(a[0]),
            "collect": lambda a: cmd_collect(a[0])}
    if len(sys.argv) < 3 or sys.argv[1] not in cmds:
        sys.exit(__doc__)
    cmds[sys.argv[1]](sys.argv[2:])
