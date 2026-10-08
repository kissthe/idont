# 评测报告：pilot-claude

## 主表

| 指标 | x_only | full_history | evidence_only |
|---|---|---|---|
| 题数 | 63 | 63 | 45 |
| 解析失败 | 0 | 0 | 0 |
| T1 准确率 | 68.3 | 87.3 | 95.6 |
| **T1 macro-F1** | 0.571 | 0.876 | 0.956 |
| T2 线索准确率（gold H） | 0.0 | 95.0 | 100.0 |
| T3 证据召回 | 0.0 | 82.2 | 87.2 |
| T3 证据精确率（严格） | 0.0 | 80.0 | 87.8 |
| T3 证据精确率（宽松） | 0.0 | 87.5 | 95.3 |
| T3 session 命中率 | 0.0 | 95.0 | 100.0 |
| 联合正确率 | 0.0 | 90.0 | 90.0 |
| **误归因率**（gold P/I 判成 H） | 0.0 | 14.0 | 8.0 |
| 成对一致率（H 与无历史关联都对） | 0.0 | 83.3 | — |
| 情绪准确率 | 98.3 | 100.0 | 97.6 |
| macro-F1 95% CI | [0.53, 0.61] | [0.79, 0.94] | [0.88, 1.00] |

百分比指标单位为 %。成对一致率的分母为 x_only: 12, full_history: 12, evidence_only: 0。

## 按题型的准确率（%）

| 题型 | x_only | full_history | evidence_only |
|---|---|---|---|
| H | 0.0 | 95.0 | 100.0 |
| I_near_miss | 100.0 | 50.0 | 80.0 |
| I_no_history | 100.0 | 83.3 | — |
| I_no_reaction | 100.0 | 100.0 | 100.0 |
| P_same_cue | 100.0 | 100.0 | 100.0 |
| P_unrelated | 100.0 | 100.0 | — |

## 混淆矩阵（行 gold，列预测）

**x_only**

| gold \ pred | H | P | I | fail |
|---|---|---|---|---|
| H | 0 | 0 | 20 | 0 |
| P | 0 | 18 | 0 | 0 |
| I | 0 | 0 | 25 | 0 |

**full_history**

| gold \ pred | H | P | I | fail |
|---|---|---|---|---|
| H | 19 | 1 | 0 | 0 |
| P | 0 | 18 | 0 | 0 |
| I | 6 | 1 | 18 | 0 |

**evidence_only**

| gold \ pred | H | P | I | fail |
|---|---|---|---|---|
| H | 20 | 0 | 0 | 0 |
| P | 0 | 12 | 0 | 0 |
| I | 2 | 0 | 11 | 0 |

## 按数据来源的 macro-F1

| 来源 | x_only | full_history | evidence_only |
|---|---|---|---|
| conv48 | 0.528 | 0.958 | 1.000 |
| golden_triples | 0.594 | 0.820 | 0.933 |

## 只给 X 就判对的 H 题（0 道，可能泄漏）

- 无

## full_history 下的错题（8 道）

| 题号 | 题型 | gold | 预测 |
|---|---|---|---|
| conv-26_A01-NM | I_near_miss | I | H |
| conv-41_A03-NM | I_near_miss | I | H |
| conv-48_A07-NM | I_near_miss | I | H |
| conv-48_A08-NM | I_near_miss | I | H |
| conv-48_A08-H | H | H | P |
| conv-49_A09-NM | I_near_miss | I | H |
| conv-26_A01-CF | I_no_history | I | P |
| conv-48_q12 | I_no_history | I | H |
