"""invocation 的中繼資料基準線。**這是對照組，不是產品。**

分類器要花錢、要送內容給模型、要人工標註來量準確率。在做那些之前，得先
知道**完全不看內容能做到多少**——否則分類器就算跑出漂亮的數字，也沒有人
知道其中有多少是它的貢獻、多少是「長的多半是機器、短的多半是人」這種
用中繼資料就講得出來的話。

**這條規則必須在人工標註之前定下來並凍結。** 事後定的基準線無法排除是照著
分類器的結果調的——那樣它就不再是一個獨立的比較對象，只是一個被打敗的
稻草人。版控的時間戳是「它先存在」的唯一證據。

規則
----
::

    歸群（屬於 226 個前綴群集之一）              → not_human_chat
    散筆且 prompt_text_len <  HUMAN_CHAT_MAX_LEN → human_chat
    散筆且 prompt_text_len >= HUMAN_CHAT_MAX_LEN → abstain（棄權）

三件事要講清楚：

**一、它預測的是「非 human_chat」，不是某一個值。** 中繼資料分不出
``user_script``／``tool_generated``／``pipeline_relay``——那三者的差別在
外框是誰寫的、載荷從哪來，都要看內容。硬要猜會把基準線變成一個假的
四分類，讓它看起來比實際強。

**二、棄權是明寫的一類，不是預設值。** 散筆裡的長內容（中位 1,347 字元）
既沒有可辨識的外框、也不短得像隨手打的一句話，中繼資料就是判不出來。
把它們硬塞進任何一邊都會讓基準線的準確率變成人為的。**棄權率本身就是
一個要報的數字**：它是「不看內容的天花板」的直接度量。

**三、門檻用已凍結的長度切點 Q1，不是新調的參數。** 掃過 13 個候選值，
Youden J 最大的是 300（0.5954），251 是 0.5886——差 0.0068，而 251 的
精確度反而較高（84.84% vs 82.49%）。差異在雜訊等級，而 251 是分層早就在
用的切點。**選它是為了不引入新的自由參數**：一個為了好看而調出來的門檻，
會讓「基準線」變成另一個被優化過的模型，那就失去對照的意義了。

分離程度（17,365 個相異內容，2026-09-06 量測）
------------------------------------------------
==================  ==========  ==========
分位                      歸群        散筆
==================  ==========  ==========
p25                        669          40
p50（中位）              1,877         104
p75                      6,706         696
==================  ==========  ==========

AUC = 0.8391（隨機取一歸群一散筆，歸群較長的機率；0.50 = 分不開）。
兩組 IQR 幾乎不相交（交集 [669, 696]，各只有 1.2% 與 0.4% 落在其中）。

**所以「用長度就能分辨機器與人」這句話是「相當強但不夠強」**：AUC 0.84
遠高於擲骰子，但 p95 之後兩組完全重疊（11,296 vs 6,768，p99 是
24,033 vs 23,727）。長內容裡人與機器一樣多，這正是棄權那一桶。

怎麼用
------
分類器做完之後，用**同一組人工標註**同時評分類器與本規則。分類器要贏，
必須在 ``abstain`` 那 2,026 筆上真的判得出東西，或者在另外兩桶上比
本規則更準。贏不了就代表這一輪的內容分類沒有帶來新資訊——那是個結論，
不是失敗。門檻寫在 ``ref/annotation_protocol.md``。

**不要用本模組產生任何最終數字。** 它不進 metrics、不進 render、
不進 docs/。它唯一的用途是被比較。
"""

from __future__ import annotations

from typing import Iterable, Mapping

# 母體的凍結座標。規則只在這個母體上有意義——換一批資料要重新量分布，
# 而重新量出來的就是另一條基準線，不能拿去跟舊的分類器比。
POPULATION_FINGERPRINT = (
    "63af7d40e051d069dece9f14771eab6be7cb487f25c1937f6ff56ec12eb6919b"
)
POPULATION_CONTENTS = 17_365
POPULATION_REQUESTS = 58_100

# 長度切點 Q1。**沿用既有的分層切點，不是本模組調出來的**（見模組 docstring）。
HUMAN_CHAT_MAX_LEN = 251

PRED_NOT_HUMAN = "not_human_chat"
PRED_HUMAN = "human_chat"
PRED_ABSTAIN = "abstain"
PREDICTIONS = (PRED_NOT_HUMAN, PRED_HUMAN, PRED_ABSTAIN)

# 本規則在 17,365 上的預測分布，量測於 2026-09-06、群集門檻 10、226 群。
# **這組數字是凍結的。** 重算若對不上，代表上游有東西變了（群集、母體、
# 或切點），那時要做的是找出變的是什麼，不是改這裡的數字。
FROZEN_PREDICTION: dict[str, dict[str, int]] = {
    PRED_NOT_HUMAN: {"contents": 11_657, "requests": 32_429},
    PRED_HUMAN: {"contents": 3_682, "requests": 18_974},
    PRED_ABSTAIN: {"contents": 2_026, "requests": 6_697},
}


def predict(clustered: bool, length: int) -> str:
    """單筆相異內容的基準線預測。**純函式，不讀任何檔案。**

    參數
    ----
    clustered:
        是否屬於 226 個前綴群集之一（``cluster_members.parquet`` 有這筆）。
    length:
        ``prompt_text_len``。**是長度不是內容**——本模組全程不碰明文。
    """
    if length is None:
        raise ValueError("length 不可為 None：長度缺值與長度為 0 是兩件事")
    if int(length) < 0:
        raise ValueError(f"length 不可為負：{length}")
    if clustered:
        return PRED_NOT_HUMAN
    return PRED_HUMAN if int(length) < HUMAN_CHAT_MAX_LEN else PRED_ABSTAIN


def predict_rows(rows: Iterable[Mapping]) -> list[str]:
    """逐列預測。每列要有 ``clustered`` 與 ``length``（或 ``prompt_text_len``）。

    刻意逐列呼叫 :func:`predict` 而不另寫向量化版本，理由同
    ``pricing.estimate_frame``：驗證報告要驗的是真正上線的那個函式。
    """
    out = []
    for row in rows:
        length = row.get("length", row.get("prompt_text_len"))
        out.append(predict(bool(row["clustered"]), length))
    return out


def summarise(predictions: Iterable[str],
              requests: Iterable[int] | None = None) -> dict[str, dict[str, int]]:
    """把逐列預測收斂成 ``FROZEN_PREDICTION`` 那個形狀。

    ``requests`` 給了就一併累加請求數；沒給則請求數為 0。三個桶一律出現，
    即使計數是 0——缺鍵與零是兩件事，而下游的比對是逐鍵做的。
    """
    counts = {p: {"contents": 0, "requests": 0} for p in PREDICTIONS}
    reqs = list(requests) if requests is not None else None
    for i, p in enumerate(predictions):
        if p not in counts:
            raise ValueError(f"未知的預測值 {p!r}，值域是 {list(PREDICTIONS)}")
        counts[p]["contents"] += 1
        if reqs is not None:
            counts[p]["requests"] += int(reqs[i])
    return counts


def verify_frozen(counts: Mapping[str, Mapping[str, int]]) -> list[str]:
    """把重算的分布對回凍結值，回傳不符之處（空 list 代表相符）。

    **回傳問題而不是 raise**：呼叫端要能一次看到全部差異。分布飄了通常
    不只飄一格，只報第一個會讓人以為問題比實際小——同 ``schema_lite``
    的 ``check_subset_counts``。
    """
    problems: list[str] = []
    for key in PREDICTIONS:
        want = FROZEN_PREDICTION[key]
        got = counts.get(key)
        if got is None:
            problems.append(f"{key}：缺這一桶")
            continue
        for field in ("contents", "requests"):
            if int(got.get(field, -1)) != want[field]:
                problems.append(
                    f"{key}.{field}：凍結值 {want[field]:,}，"
                    f"實得 {int(got.get(field, -1)):,}")
    total_c = sum(int(v.get("contents", 0)) for v in counts.values())
    total_r = sum(int(v.get("requests", 0)) for v in counts.values())
    if total_c != POPULATION_CONTENTS:
        problems.append(
            f"相異內容總數：母體 {POPULATION_CONTENTS:,}，實得 {total_c:,}")
    if total_r != POPULATION_REQUESTS:
        problems.append(
            f"請求總數：母體 {POPULATION_REQUESTS:,}，實得 {total_r:,}")
    return problems
