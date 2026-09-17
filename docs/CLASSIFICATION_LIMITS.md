# 用途分流層的能力界線

> 產生時間：2026-09-12T11:48:49+08:00  
> 版本：1.0  
> 本檔只記既有分流結果的限制，不提出新值域或新判準。

## 指標說明

格式沿用 [INDEX.md](INDEX.md)：每一個數字先說清楚它回答什麼、單位、來源、
分母、覆蓋率與限制。這裡的「一致」只比 `disposition`；人工三方比較另比
`frame_owner` 與 `disposition`，不使用 Ray 手填、已有 7 筆 must 違反的
`axis_level` v1。

| 指標 | 回答什麼 | 單位 | 來源表 | 分母 | 覆蓋率 | 注意事項 | 版本 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `cross_model_disposition_agreement` | 同提示詞下，Opus 與 Codex 的 `disposition` 是否逐群相同？ | 群 | `judge_output.e13f9738.csv`、`codex_judge_output.csv`、`blind_label_sample.csv` | 分層抽出的 58 個 clean 群；逐類分母是 **Opus 判定**的該類群數 | 58/58（100%） | 58 群依 Opus 判定分層抽樣，故逐類數字不是母體精確率；無獨立真值。Codex 58 群工具事件皆為 0。 | 1.0 |
| `tool_injected_spread_check` | 兩模型的 `tool_injected` 共識是否同時對應較高散布度？ | 群；uid／unit_type／model 的相異種類數 | 上述兩份判定、`clusters.parquet`、`cluster_members.parquet` | 同一批 58 群；共識 11、只有一方 8、兩方都不判 39 | 58/58（100%） | 勝出率是所有跨組配對的 `P(共識組 uid > 單方組 uid) + 0.5P(相等)`，不是正確率。 | 1.0 |
| `current_tool_injected_rule_fit` | 現行 225 群的模型結果是否符合預先寫定的散布度判準？ | 群；uid／unit_type 相異種類數 | `judge_output.e13f9738.csv`、`clusters.parquet`、`cluster_members.parquet` | 225 個 eligible clean 群 | 225/225（100%） | 這是模型產生的描述性結果，不是已驗證分類。 | 1.0 |
| `ray_three_way_comparison` | 兩模型與 Ray 的前 30 群人工盲標如何重合？ | 群 | 上述兩份模型判定、`blind_labels.csv` | Ray 已標的 30/58 群 | 30/58（51.7%） | 人工值只用了兩個 `disposition`；尚未完成至少間隔 3 天的 15 群盲重標，不能當參考真值。 | 1.0 |
| `prefilter_flow_nature` | 四條機械前置規則能確定多少流量性質？ | 相異內容；請求 | `prefilter_counts.csv`；由 `prefilter.build()` 同輸入重跑兩次核對 | 31,011 個長度至少 20 的相異內容；106,993 筆對應請求 | 31,011/31,011（100%） | 100% 指相同凍結輸入重跑兩次逐 sha256 指派完全相同，不是用途分類正確率。 | 1.0 |

## A. 兩模型的交叉一致率

Opus 與 Codex 的 `disposition` 逐群相同 **31/58，53.4%**。

| Opus 判定的類別 | 相同群數 | 分母（群） | 一致率 |
| --- | ---: | ---: | ---: |
| `tool_injected` | 11 | 12 | 91.7% |
| `user_envelope` | 9 | 15 | 60.0% |
| `service_relay` | 4 | 10 | 40.0% |
| `batch_project` | 7 | 20 | 35.0% |
| `insufficient` | 0 | 1 | 只報 0/1，不出比例 |

兩側使用同一份提示詞與字典內容；逐筆提示詞由位元組相同比對測試固定，差別
只在模型與 CLI 執行端。兩側座標如下：

| 執行端 | 實際模型字串 | 指紋 | 產生時間／判定區間 |
| --- | --- | --- | --- |
| Opus | `claude-opus-5` | 本地請求設定 `e13f9738fba32a8f5167807157718758954dc2e2f1304392bdf86b3b7dd97114` | 2026-09-10T15:56:35 至 2026-09-11T09:33:20（輸出未帶時區） |
| Codex | `gpt-5.6-sol` | 提示詞內容 `7d8ec49fde30d875c85d7136b7d3560ad43558cf9151b9b0fe9c385b2c3864b4`；執行 `804f7e6f4211c8109c183a593430ca8ce45bcc2071996be425d6bc5e6d29d31e` | 2026-09-11T16:58:52+08:00 至 2026-09-11T21:32:57+08:00 |

## B. 91.7% 不是中繼資料上的一致

兩模型都判為 `tool_injected` 的 11 群，uid 數中位數是 **2**；只有一方判為
`tool_injected` 的 8 群，中位數反而是 **4.5**。共識組 uid 數相對於單方組
的勝出率是 **31.2%**（比較兩組全部 88 個跨組配對，相等各算半點）。

`tool_injected` 的原判準是外框「跨越沒有理由共用程式碼的使用者」。共識組並未
呈現更高散布度；可觀察到的模型一致是前綴文字判讀的一致，不是這條中繼資料
判準的一致。

## C. 機械不可分證明

共識組 11 群中，有 **3 群**的 `(uid數, unit_type種類, model種類)` 完全等於
`(1, 1, 1)`。非共識的 47 群中，有 **17 群**也是 `(1, 1, 1)`。

任何只看這三欄的確定性規則，都必須把這 20 群映射成同一結果，不能同時重現
兩模型目前的共識與非共識分組。這是輸入特徵相同造成的不可分，不是統計不顯著；
調整門檻或加權無法增加三欄裡不存在的資訊。

## D. 預先寫定的判準從頭沒有作用

字典要求 `tool_injected` 同時滿足 `uid數 ≥ 10`、`unit_type種類 ≥ 3`、
`model種類 ≥ 4`。現行 225 個 clean 群中，模型判為 `tool_injected` 的 13 群，
uid 數中位數是 **2**、unit_type 種類中位數是 **1**。

A001 有 **182 uid**，但三次執行都沒有被判為 `tool_injected`：舊值域同指紋兩次
均為 `split`，現行指紋一次為 `user_envelope`。三次的值域不同，所以這裡只比較
「是否命中 tool_injected」，不把 `split` 與 `user_envelope` 說成同一類。

## E. 人工標註只使用兩個值

Ray 已標的 30 群中，`batch_project` 是 **23 群**、`tool_injected` 是 **7 群**；
`user_envelope`、`service_relay`、`insufficient` 均為 **0 群**。

三方在 `frame_owner` 與 `disposition` 都相同的是 **4/30 群**；兩模型相同而 Ray
不同的是 **10/30 群**，其中 Ray 判 `batch_project` **8 群**、`tool_injected`
**2 群**。兩模型彼此不同的是 **16/30 群**。這 30 群尚不足以把任何一方升格為
參考標籤，但足以顯示五值字典沒有被人工判讀實際使用成五個值。

## F. 結論與交付含意

分流層降為探索性材料，不進報告結論。校方問「做什麼」，現有資料能穩定回答的
是四條機械前置規則所描述的流量性質，不是用途類別的來源歸屬：

| 機械類別 | 相異內容 | 分母 31,011 | 請求 | 分母 106,993 |
| --- | ---: | ---: | ---: | ---: |
| 向量化 | 587 | 1.9% | 1,010 | 0.9% |
| 影像生成 | 401 | 1.3% | 442 | 0.4% |
| 工具自動發出 | 2,387 | 7.7% | 11,116 | 10.4% |
| 流程中繼處理 | 10,271 | 33.1% | 36,325 | 34.0% |
| **合計** | **13,646** | **44.0%** | **48,893** | **45.7%** |

同一凍結輸入連跑兩次，31,011 個相異內容的逐 sha256 指派完全相同；兩次安全
彙總雜湊皆為 `243198528ac418bdf7a1d9bc93511a8416c2e3366d21861a9646e882d2b5b48d`。
這裡的重現性是 **31,011/31,011（100%）**。

因此「做什麼」目前能交付到流量性質為止。這與「誰在用」的最小可靠粒度是學院、
「花多少」只能報牌價等值且非帳單、非全量，是同一種能力界線：說清楚資料能支持
到哪裡，也用可重算的反例說明為什麼不能再往下寫成結論。

## 來源與版本

| 來源 | 本次用途 | 來源產生時間 | 指紋／版本 |
| --- | --- | --- | --- |
| repo 外 `_rescued_scratchpad/judge_output.e13f9738.csv` | Opus 的 225 群描述性判定 | 2026-09-10 至 2026-09-11 | `e13f9738…`；`claude-opus-5` |
| repo 外 `codex_blind_2026-09/codex_judge_output.csv` | Codex 的 58 群交叉判定 | 2026-09-11 | `804f7e6f…`；`gpt-5.6-sol` |
| repo 外 `_rescued_scratchpad/blind_label_sample.csv` | 固定 58 群分母 | 2026-09-11 | 抽樣種子 `20260911` |
| repo 外 `_rescued_scratchpad/blind_labels.csv` | Ray 的 30 群 v1 手填結果 | 2026-09-11 | v1；`axis_level` 不使用 |
| repo 外 `_rescued_scratchpad/clusters.parquet` 與 `cluster_members.parquet` | uid、unit_type、model 的相異種類數 | 2026-09-06 至 2026-09-10 | 聚類門檻 10；群集座標見 `ref/annotation_protocol.md` 第一節 |
| `runs/2026-09-05T1700_prefilter/classify_lite/prefilter_counts.csv` | 四條前置規則的相異內容與請求數 | 2026-09-05T20:12:04+08:00 | 可分類母體 `63af7d40e051d069dece9f14771eab6be7cb487f25c1937f6ff56ec12eb6919b` |
| `src/classify_lite/summarize_classification_limits.py` | 重算 A–F 的安全聚合 | 2026-09-12 | 本檔 1.0 |
