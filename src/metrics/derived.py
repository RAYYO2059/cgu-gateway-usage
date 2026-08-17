"""derived 指標：需要先算一步才拿得到的量。

統一規則：
- 所有 token 加總排除 usage_missing=True 的列，並在輸出註明排除筆數。
- 涉及 message_count 一律用 message_count_peak（峰值，非最終值）。
- 不用 mean 當主要統計量。展開深度的 mean=6.94 但 p50=1，
  平均值會把「一半的動作只發一個請求」這件事完全蓋掉。
"""

from __future__ import annotations

import pandas as pd

from src.metrics.registry import MetricResult, metric

QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90, 0.99)
# 欄名由 QUANTILES 推導，不另外手寫一份。分開寫的話改分位數只會改到一半：
# 數值換了、欄名還是舊的，而且輸出看起來完全正常。
QUANTILE_LABELS = tuple(f"p{int(round(q * 100))}" for q in QUANTILES)

PEAK_CAVEAT = (
    "message_count_peak 是該 turn/thread 內送出過的**最長歷史長度**，不是最終訊息數。"
    "有 19 個 turn 的 has_compaction=True（歷史在同一個 turn_id 內被重置過），"
    "對這些 turn，峰值高於壓縮後的實際狀態，"
    "而壓縮前後兩段被合併成同一列（見 n_segments）。"
)


def _drop_usage_missing(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """排除 token 遺失的列，回傳 (可用資料, 排除筆數)。"""
    mask = frame["usage_missing"].fillna(False).astype(bool)
    return frame[~mask], int(mask.sum())


def _quantile_rows(series: pd.Series, label: str, extra: dict | None = None) -> list:
    values = series.dropna().astype("float64")
    row = {"分組": label, "n": int(len(values))}
    row.update(extra or {})
    if len(values):
        for label, q in zip(QUANTILE_LABELS, QUANTILES):
            row[label] = round(float(values.quantile(q)), 4)
        row["max"] = round(float(values.max()), 4)
    return [row]


# ---------------------------------------------------------------------------
# 11 turn_expansion_depth
# ---------------------------------------------------------------------------
@metric(
    name="turn_expansion_depth",
    question="一次使用者動作被 agent 展開成幾個 API 請求？",
    unit="turn",
    source="turn",
    denominator="1,025 個 turn_id 非 null 的 turn（direct 客戶端沒有 turn，不計入）",
    caveat=(
        "**不是效能指標也不是成本指標**。展開深度高只代表 agent 往返多，"
        "不代表使用者等待久或花費高。"
        "務必看 p50 而非 mean：mean=6.94 但 p50=1，"
        "一半的使用者動作只產生一個請求，平均值被長尾嚴重拉高。"
        "輸出並列兩欄：「全體」與「排除有壓縮的 turn」。"
        "有壓縮的 turn 內對話歷史被重置過，n_requests 把壓縮前後兩段合併計算，"
        "一個 turn_id 實際上是兩次以上的動作。兩欄並列是為了讓讀者直接看到"
        "這批 turn 對 p99 與 max 的影響有多大——若兩欄的 p50 相同、"
        "只有尾端不同，代表壓縮只污染極值，中位數仍可用。"
    ),
    version="1.2",
)
def turn_expansion_depth(tables: dict) -> MetricResult:
    turn = tables["turn"]
    clean = turn[~turn["has_compaction"].fillna(False).astype(bool)]

    def stats(frame: pd.DataFrame) -> dict[str, float]:
        depth = frame["n_requests"].astype("float64")
        values = {"n_turns": float(len(depth))}
        for label, q in zip(QUANTILE_LABELS, QUANTILES):
            values[label] = round(float(depth.quantile(q)), 4)
        values["max"] = float(depth.max())
        values["single_request_share"] = round(float((depth == 1).mean()), 4)
        values["mean（僅供對照，勿引用）"] = round(float(depth.mean()), 4)
        return values

    whole, without = stats(turn), stats(clean)
    data = pd.DataFrame([
        {"統計量": key, "全體": whole[key], "排除有壓縮的 turn": without[key]}
        for key in whole
    ])
    data.loc[len(data)] = {
        "統計量": "n_turns_with_compaction",
        "全體": float(int(turn["has_compaction"].fillna(False).sum())),
        "排除有壓縮的 turn": 0.0,
    }
    return MetricResult(data=data, n_total=len(turn),
                        n_covered=int(turn["n_requests"].notna().sum()))


# ---------------------------------------------------------------------------
# 12 thread_tool_message_ratio
# ---------------------------------------------------------------------------
@metric(
    name="thread_tool_message_ratio",
    question="一個 thread 的對話歷史裡，有多少比例是工具訊息而非人／模型的話？",
    unit="thread",
    source="thread",
    denominator="435 個 thread，比值 =（peak - user - assistant）/ peak",
    caveat=(
        "**這是訊息「數量」的比例，不是時間比例也不是成本比例**。"
        "一則工具訊息可能只有幾個 token，一則使用者訊息可能有幾千個，"
        "不可據此推論 agent 消耗了七成資源。"
        + PEAK_CAVEAT
    ),
    version="1.0",
)
def thread_tool_message_ratio(tables: dict) -> MetricResult:
    thread = tables["thread"]
    ratio = thread["tool_message_ratio"].astype("Float64")
    rows = _quantile_rows(ratio, "全體 thread")
    rows[0]["zero_ratio_share"] = round(
        float((ratio.dropna().astype("float64") == 0).mean()), 4)
    return MetricResult(
        data=pd.DataFrame(rows),
        n_total=len(thread),
        n_covered=int(ratio.notna().sum()),
    )


# ---------------------------------------------------------------------------
# 13 cache_hit_by_request_position
# ---------------------------------------------------------------------------
@metric(
    name="cache_hit_by_request_position",
    question="在一個 turn 內，第幾個請求開始吃到 prompt 快取？",
    unit="request",
    source="request",
    denominator=(
        "turn_id 非 null 且 usage_missing=False 且 prompt_tokens>0 的請求，"
        "依 turn 內時間序分成第 1、第 2、第 3 個以後"
    ),
    caveat=(
        "**這是協定層的機制特性，不是使用者行為差異**。"
        "第一個請求快取率低是因為前綴尚未被快取，"
        "與該使用者「用得好不好」無關，不可拿來比較人或族群。"
        "這個指標不依賴樣本量，1.8 天的資料也足以觀察機制。"
        "**兩條排除規則各自成欄**：n_excluded_usage_missing（token 遺失，"
        "比值算不出來）與 n_excluded_zero_prompt_tokens（分母為零）。"
        "兩者相加才是母數與覆蓋數的差額；"
        "合成一欄的話，讀者無從判斷少掉的請求是資料品質問題還是結構性的零分母。"
    ),
    version="1.1",
)
def cache_hit_by_request_position(tables: dict) -> MetricResult:
    request = tables["request"]
    scoped = request[request["turn_id"].notna()].copy()
    usable, n_excluded = _drop_usage_missing(scoped)
    n_zero_prompt = int((usable["prompt_tokens"].fillna(0) <= 0).sum())
    usable = usable[usable["prompt_tokens"].fillna(0) > 0].copy()

    usable = usable.sort_values(["turn_id", "ts_taipei"])
    usable["position"] = usable.groupby("turn_id").cumcount() + 1
    usable["position_bucket"] = usable["position"].where(
        usable["position"] <= 2, 3).map({1: "1st", 2: "2nd", 3: "3rd+"})
    usable["cache_rate"] = (usable["cached_tokens"].astype("Float64")
                            / usable["prompt_tokens"].astype("Float64"))

    rows = []
    for bucket in ("1st", "2nd", "3rd+"):
        part = usable[usable["position_bucket"] == bucket]
        row = _quantile_rows(part["cache_rate"], bucket)[0]
        row["n_requests"] = int(len(part))
        row["n_turns"] = int(part["turn_id"].nunique())
        row["zero_cache_share"] = round(
            float((part["cache_rate"].dropna().astype("float64") == 0).mean()), 4
        ) if len(part) else None
        row["n_excluded_usage_missing"] = n_excluded
        row["n_excluded_zero_prompt_tokens"] = n_zero_prompt
        rows.append(row)

    return MetricResult(
        data=pd.DataFrame(rows),
        n_total=len(scoped),
        n_covered=len(usable),
    )


# ---------------------------------------------------------------------------
# 14 token_inflation_by_client_type
# ---------------------------------------------------------------------------
@metric(
    name="token_inflation_by_client_type",
    question="原始 prompt_tokens 相對於實際未快取部分放大了幾倍？",
    unit="request",
    source="request",
    denominator=(
        "usage_missing=False 且 prompt_tokens - cached_tokens > 0 的請求，"
        "比值 = prompt_tokens /（prompt_tokens - cached_tokens）"
    ),
    caveat=(
        "**用來說明「原始 token 數會高估實際資源消耗」，不是計費依據**。"
        "快取命中的 token 仍會出現在 prompt_tokens 裡，"
        "直接加總 prompt_tokens 會把同一段前綴重複計算數十次。"
        "分母為 0（完全命中快取）的請求無法計算比值，已排除並記在 n_excluded_zero_denominator。"
        "token 加總已排除 usage_missing=True 的請求。"
        "direct 這一組被單一使用者主導，其分位數會被抑制——"
        "那不是計算失敗，是因為該組的中位數其實在描述一個人。"
    ),
    group_by=["client_type"],
    version="1.0",
)
def token_inflation_by_client_type(tables: dict) -> MetricResult:
    request = tables["request"]
    usable, n_excluded = _drop_usage_missing(request)

    prompt = usable["prompt_tokens"].astype("Float64")
    cached = usable["cached_tokens"].astype("Float64").fillna(0)
    uncached = prompt - cached
    usable = usable.assign(_uncached=uncached, _inflation=prompt / uncached)

    zero_denominator = int((usable["_uncached"] <= 0).sum())
    usable = usable[usable["_uncached"] > 0]

    rows = []
    for client_type, part in usable.groupby("client_type", dropna=False):
        row = _quantile_rows(part["_inflation"], str(client_type))[0]
        row["client_type"] = str(client_type)
        row["n_requests"] = int(len(part))
        row["n_users"] = int(part["username"].nunique())
        row["n_excluded_usage_missing"] = n_excluded
        row["n_excluded_zero_denominator"] = zero_denominator
        rows.append(row)

    data = pd.DataFrame(rows)
    ordered = ["client_type", "分組", "n_requests", "n_users",
               "p10", "p25", "p50", "p75", "p90", "p99", "max",
               "n_excluded_usage_missing", "n_excluded_zero_denominator"]
    data = data[[c for c in ordered if c in data.columns]]
    return MetricResult(
        data=data,
        n_total=len(request),
        n_covered=len(usable),
        # 分位數描述的是「這一組的行為」，被單人主導時同樣要抑制。
        ratio_columns=["p10", "p25", "p50", "p75", "p90", "p99", "max"],
    )


# ---------------------------------------------------------------------------
# 15 usage_missing_impact
# ---------------------------------------------------------------------------
@metric(
    name="usage_missing_impact",
    question="有多少請求的 token 用量沒有被記錄？它們是什麼樣的請求？",
    unit="request",
    source="request",
    denominator="全部 9,937 個請求中 usage_missing=True 的 188 筆",
    caveat=(
        "**這 188 筆的 token 是「遺失」不是「0」**。"
        "其中 102 筆是 status=200 的串流請求，total_tokens 全部記成 0——"
        "直接 sum(total_tokens) 會把它們當成零消耗而低估總量。"
        "所有 token 加總都應排除這些列，並在結果中註明排除筆數。"
        "另外 84 筆是 400 錯誤，請求根本沒送到模型，沒有用量是正確的。"
        "兩種成因必須分開理解，不可一律當成資料品質問題。"
    ),
    version="1.0",
)
def usage_missing_impact(tables: dict) -> MetricResult:
    request = tables["request"]
    missing = request[request["usage_missing"].fillna(False).astype(bool)]

    rows = [{
        "類別": "彙總", "值": "usage_missing 總筆數",
        "n_requests": int(len(missing)),
        "佔全體請求": round(len(missing) / len(request), 4),
        "n_users": int(missing["username"].nunique()),
    }, {
        "類別": "彙總", "值": "其中 status=200（token 真正遺失）",
        "n_requests": int((missing["status_code"] == 200).sum()),
        "佔全體請求": round(float((missing["status_code"] == 200).sum()) / len(request), 4),
        "n_users": int(missing.loc[missing["status_code"] == 200,
                                   "username"].nunique()),
    }, {
        "類別": "彙總", "值": "其中 status>=400（沒送到模型，無用量屬正常）",
        "n_requests": int((missing["status_code"] >= 400).sum()),
        "佔全體請求": round(float((missing["status_code"] >= 400).sum()) / len(request), 4),
        "n_users": int(missing.loc[missing["status_code"] >= 400,
                                   "username"].nunique()),
    }]
    for column in ("endpoint", "status_code", "stream", "provider"):
        for value, count in missing[column].astype("string").fillna("<null>") \
                .value_counts().items():
            rows.append({
                "類別": column, "值": str(value), "n_requests": int(count),
                "佔全體請求": round(int(count) / len(request), 4),
                "n_users": None,
            })
    return MetricResult(
        data=pd.DataFrame(rows),
        n_total=len(request),
        n_covered=len(missing),
    )


# ---------------------------------------------------------------------------
# 16 prompt_length_distribution
# ---------------------------------------------------------------------------
@metric(
    name="prompt_length_distribution",
    question="送出去的 prompt 有多長（字元數）？",
    unit="request",
    source="request",
    denominator="prompt_len > 0 的請求（9,450 筆，佔全體 95.1%）",
    caveat=(
        "**只有長度沒有內容**——L1 刻意只保留字元數，原文含個資與本機路徑，不進表。"
        "字元數不等於 token 數，中英文比例不同會讓兩者差異很大，不可互相換算。"
        "**這個欄位只含本次送出的輸入，不含對話歷史**（已驗證，非推論）："
        "1,025 個 turn 中有 997 個（97.3%）的 prompt_len 在 turn 內完全不變，"
        "而同批 turn 的 message_count 全數成長；相鄰請求對有 99.2% 的 prompt_len 不動，"
        "其變動與 user_message_count 變動的一致率 99.8%；"
        "與 message_count 的相關性為負（spearman -0.34），"
        "與 user_message_count 亦為負（spearman -0.41）——"
        "若含歷史，這兩個相關性都該是正的。"
        "反面案例：message_count=560 而 prompt_len=1。"
        "另注意欄內混有客戶端自動產生的輸入（任務說明、附件包裝、建議指令），"
        "不全是使用者鍵入的內容。"
        "最長一筆達 900,179 字元，與 context_length_exceeded 錯誤可能相關"
        "（見 context_length_exceeded_profile）。"
        "direct 這一組被單一使用者主導，分位數會被抑制——"
        "該組的長度分布實際上是一支腳本的行為。"
        "**但這裡的抑制擋不住任何東西**：本表同時輸出未抑制的「全體」列，"
        "拿它與 codex 列對照就能讀出 direct 的分布形狀"
        "（全體 p99 遠高於 codex p99，差額只可能來自 direct）。"
        "留著全體列是因為它本身有用；"
        "suppression_reason 欄會把這件事寫在被抑制的那一列上，不假裝擋住了。"
    ),
    group_by=["client_type"],
    version="1.1",
)
def prompt_length_distribution(tables: dict) -> MetricResult:
    request = tables["request"]
    scoped = request[request["prompt_len"].fillna(0) > 0]

    rows = []
    for client_type, part in scoped.groupby("client_type", dropna=False):
        row = _quantile_rows(part["prompt_len"], str(client_type))[0]
        row["client_type"] = str(client_type)
        row["n_requests"] = int(len(part))
        row["n_users"] = int(part["username"].nunique())
        rows.append(row)
    row = _quantile_rows(scoped["prompt_len"], "全體")[0]
    row["client_type"] = "全體"
    row["n_requests"] = int(len(scoped))
    row["n_users"] = int(scoped["username"].nunique())
    rows.append(row)

    data = pd.DataFrame(rows)
    ordered = ["client_type", "n_requests", "n_users",
               "p10", "p25", "p50", "p75", "p90", "p99", "max"]
    data = data[[c for c in ordered if c in data.columns]]
    return MetricResult(
        data=data,
        n_total=len(request),
        n_covered=len(scoped),
        ratio_columns=["p10", "p25", "p50", "p75", "p90", "p99", "max"],
        has_unsuppressed_total_row=True,
    )


# ---------------------------------------------------------------------------
# 17 context_length_exceeded_profile
# ---------------------------------------------------------------------------
@metric(
    name="context_length_exceeded_profile",
    question="撞到上下文長度上限的請求長什麼樣？",
    unit="request",
    source="request",
    denominator="error_code = context_length_exceeded 的 47 筆請求",
    caveat=(
        "**母數只有 47 筆，只做描述、不出任何比例**。"
        "不可據此估算「多少比例的使用者會撞到上限」，"
        "也不可拿來比較模型或族群——樣本量不支持任何比較。"
        "這些請求被拒絕於模型之前，usage_details 為空，"
        "其 token 欄位不可信（見 n_usage_missing）；prompt_len 字元數才是可用的長度訊號。"
    ),
    version="1.0",
)
def context_length_exceeded_profile(tables: dict) -> MetricResult:
    request = tables["request"]
    hits = request[request["error_code"] == "context_length_exceeded"]

    rows = [{
        "類別": "彙總", "值": "n_requests",
        "n": int(len(hits)), "備註": "",
    }, {
        "類別": "彙總", "值": "n_users",
        "n": int(hits["username"].nunique()), "備註": "",
    }, {
        "類別": "彙總", "值": "n_usage_missing",
        "n": int(hits["usage_missing"].fillna(False).astype(bool).sum()),
        "備註": "usage_details 為空，token 欄位不可信",
    }]
    for column in ("model_family", "error_param", "endpoint", "client_type"):
        for value, count in hits[column].astype("string").fillna("<null>") \
                .value_counts().items():
            rows.append({"類別": column, "值": str(value),
                         "n": int(count), "備註": ""})
    for column, label in (("prompt_len", "prompt_len 字元"),
                          ("prompt_tokens", "prompt_tokens（不可信）")):
        values = hits[column].dropna().astype("float64")
        if not len(values):
            continue
        for stat, value in (("p50", values.quantile(0.5)),
                            ("p90", values.quantile(0.9)),
                            ("min", values.min()), ("max", values.max())):
            rows.append({"類別": label, "值": stat,
                         "n": int(round(float(value))), "備註": ""})
    return MetricResult(
        data=pd.DataFrame(rows),
        n_total=len(request),
        n_covered=len(hits),
    )


# ---------------------------------------------------------------------------
# 19 model_consistency
# ---------------------------------------------------------------------------
@metric(
    name="model_consistency",
    question="gateway 回報的模型，與回應摘要裡記的模型一致嗎？",
    unit="request",
    source="request",
    denominator="model_returned 與 response_model 兩欄都有值的請求",
    caveat=(
        "**這是資料勾稽，不是服務品質指標**。"
        "現有的『模型替換率 0.01%』是拿 gateway 自己的 model_requested 與 "
        "model_returned 相比得出的——兩欄都由同一段程式寫入，"
        "若那段程式有系統性錯誤，比對結果會一致地錯，從數字上看不出來。"
        "response_model 來自回應摘要，是目前唯一能獨立佐證的第三個欄位。"
        "怎麼讀：兩者完全一致，代表 gateway 忠實轉發，"
        "原本的替換率數字可以採信；出現不一致，那個差異才是真正需要追的替換。"
        "**最重要的限制：這個檢查涵蓋不到 Codex 流量**。"
        "response_model 只出現在 1,958 筆請求上（全體的 19.7%），"
        "而且**全部都是 direct 客戶端**——7,115 筆 codex 請求無一有這個欄位。"
        "因此即使一致率 100%，能佐證的也只有直呼那一段；"
        "佔七成的 Codex 流量仍然只能靠 gateway 自己的兩個欄位互證，無法獨立驗證。"
        "要補這個洞只能從上游想辦法，不是這份資料能解決的。"
        "**一致不等於正確**——兩欄仍可能來自同一次上游回應，"
        "這個檢查排除得了轉發過程的竄改，排除不了上游本身回報錯誤。"
        "版本後綴（-YYYY-MM-DD）已在比對前剝除，"
        "否則同一模型的不同快照會被算成不一致。"
    ),
    version="1.0",
)
def model_consistency(tables: dict) -> MetricResult:
    from src.extract import _MODEL_DATE_SUFFIX

    request = tables["request"]

    def family(series: pd.Series) -> pd.Series:
        return series.astype("string").str.replace(
            _MODEL_DATE_SUFFIX, "", regex=True)

    both = request[request["model_returned"].notna()
                   & request["response_model"].notna()].copy()
    both["_returned_family"] = family(both["model_returned"])
    both["_response_family"] = family(both["response_model"])
    raw_match = both["model_returned"].astype("string") == \
        both["response_model"].astype("string")
    family_match = both["_returned_family"] == both["_response_family"]

    total = len(request)
    rows = [
        {"項目": "n_requests（全體）", "n": total, "佔比": 1.0, "備註": ""},
        {"項目": "response_model 有值", "n": int(request["response_model"].notna().sum()),
         "佔比": round(float(request["response_model"].notna().mean()), 4),
         "備註": "缺值代表該請求沒有回應摘要（多為錯誤或非對話端點）"},
        {"項目": "兩欄皆有值（可比對）", "n": len(both),
         "佔比": round(len(both) / total, 4) if total else None, "備註": ""},
        {"項目": "原字串完全一致", "n": int(raw_match.sum()),
         "佔比": round(float(raw_match.mean()), 4) if len(both) else None,
         "備註": "含版本後綴"},
        {"項目": "模型族一致", "n": int(family_match.sum()),
         "佔比": round(float(family_match.mean()), 4) if len(both) else None,
         "備註": "剝除 -YYYY-MM-DD 後綴後比對"},
        {"項目": "模型族不一致", "n": int((~family_match).sum()),
         "佔比": round(float((~family_match).mean()), 4) if len(both) else None,
         "備註": "這才是真正的模型替換"},
    ]
    for (returned, response), part in both[~family_match].groupby(
            ["_returned_family", "_response_family"], dropna=False):
        rows.append({
            "項目": f"不一致明細：{returned} → {response}",
            "n": int(len(part)),
            "佔比": round(len(part) / len(both), 4) if len(both) else None,
            "備註": f"{part['username'].nunique()} 人",
        })

    # 可比對的範圍長什麼樣，決定這個檢查值多少。若它剛好避開主要流量，
    # 一致率再高也佐證不了什麼——所以這段拆解與一致率同等重要，必須並列輸出。
    for column in ("client_type", "endpoint"):
        for value, part in request.groupby(column, dropna=False):
            covered = int(part["response_model"].notna().sum())
            rows.append({
                "項目": f"可比對範圍：{column}={value}",
                "n": covered,
                "佔比": round(covered / len(part), 4) if len(part) else None,
                "備註": f"該組共 {len(part)} 筆"
                        + ("，**完全無法比對**" if covered == 0 else ""),
            })
    return MetricResult(
        data=pd.DataFrame(rows),
        n_total=total,
        n_covered=len(both),
        # 這裡的「佔比」是資料勾稽的覆蓋率，不是人群統計，不需要抑制。
        ratio_columns=[],
    )


# ---------------------------------------------------------------------------
# 18 anomaly_profile
#
# concentration_summary 原本在這個位置，已降級為 run metadata
# （runs/<run_id>/concentration_summary.csv，由 aggregate 產生）。
# 它的每一列都能從 concentration.csv 加總還原，且描述的是抑制規則本身
# 而不是使用行為。
# ---------------------------------------------------------------------------
# 每個異常的判準。寫成常數而不是埋在程式裡，是因為這些門檻決定了
# 「什麼算異常」——那是政策判斷，要看得見才能被質疑。
ANOMALY_ERROR_CODE = "context_length_exceeded"
ANOMALY_LEGACY_FAMILIES = ("gpt-4o-mini",)   # 舊模型族
ANOMALY_OFFPEAK_HOURS = range(0, 8)          # 台北時間 0–7 時
ANOMALY_DOMINANT = 0.80                      # 單人佔該切片流量超過此比例才列入


def _describe_cadence(timestamps: pd.Series) -> str:
    """請求間隔的規律程度。自動化與人為操作的間隔分布形狀差很多。"""
    gaps = timestamps.sort_values().diff().dt.total_seconds().dropna()
    if len(gaps) < 3:
        return "樣本不足以描述節奏"
    median = float(gaps.median())
    tight = float(gaps.between(0.5 * median, 2 * median).mean())
    return (f"相鄰請求間隔中位數 {median:.1f} 秒，"
            f"{100 * tight:.0f}% 的間隔落在中位數的 0.5–2 倍區間")


@metric(
    name="anomaly_profile",
    question="有哪些使用者的行為明顯偏離其他人？各是什麼樣的行為？",
    unit="user",
    source="request",
    denominator=(
        "三個各自定義的切片：context_length_exceeded 錯誤、"
        f"舊模型族 {'/'.join(ANOMALY_LEGACY_FAMILIES)} 的請求、"
        "台北時間 0–7 時的離峰請求。每個切片只在單人佔比超過 "
        f"{100 * ANOMALY_DOMINANT:.0f}% 時列出。"
    ),
    caveat=(
        "**這張表描述行為特徵，不指名個人**。輸出不含 username，"
        "也不含任何能直接還原到帳號的欄位。"
        "要把某一列對應到具體帳號，請查 `ref/user_registry.csv`——"
        "該檔不進版控，且需要另外的授權。"
        "「佔比」是該類異常佔全體請求的比例，分母固定是全部請求。"
        "**三列不必然是三個人**：同一個帳號可能同時觸發多個判準，"
        "n_users 是該列自己的人數，不可跨列相加。"
        "判準是描述性的門檻（見 denominator），不是偵測規則——"
        "沒被列出來不代表沒有異常，只代表沒有超過這三條門檻。"
        "**不做歸因**：規律的請求間隔與自動化一致，但也與任何固定輪詢的"
        "正常用途一致，這張表不判斷那是濫用還是正當使用。"
    ),
    version="1.0",
)
def anomaly_profile(tables: dict) -> MetricResult:
    request = tables["request"]
    total = len(request)
    rows: list[dict] = []
    # 記下每一列的主導帳號，只為了在描述裡指出「這兩列是同一個人」。
    # 帳號本身不進輸出。
    owners: list[str] = []
    # 各列的請求會重疊（同一人同時觸發多條判準），覆蓋率要去重才不會超過 100%。
    flagged: set = set()

    def dominant(slice_: pd.DataFrame) -> str | None:
        """該切片是否由單一使用者主導？是的話回傳那個帳號。"""
        if slice_.empty:
            return None
        shares = slice_["username"].value_counts(normalize=True)
        return shares.index[0] if shares.iloc[0] >= ANOMALY_DOMINANT else None

    def add(kind: str, who: str, part: pd.DataFrame, description: str) -> None:
        """part 是**該使用者的異常請求**，不是整個切片。

        用切片當計數的話，「離峰單人集中」會報成 1,972 筆／17 人——
        那是離峰時段的全部流量，不是異常。異常是其中一個人的那一份。
        """
        overlap = ""
        if who in owners:
            overlap = f"（與第 {owners.index(who) + 1} 列為同一帳號）"
        owners.append(who)
        flagged.update(part["request_id"])
        rows.append({
            "異常類型": kind,
            "n_users": int(part["username"].nunique()),
            "n_requests": int(len(part)),
            "佔比": round(len(part) / total, 4) if total else None,
            "特徵描述": description + overlap,
        })

    # (a) 錯誤集中在單一使用者
    hits = request[request["error_code"] == ANOMALY_ERROR_CODE]
    who = dominant(hits)
    if who is not None:
        mine = hits[hits["username"] == who]
        whole = request[request["username"] == who]
        window = (mine["ts_taipei"].max() - mine["ts_taipei"].min()).total_seconds() / 60
        top_len = int(mine["prompt_len"].mode().iloc[0])
        repeats = int((mine["prompt_len"] == top_len).sum())
        add(
            f"{ANOMALY_ERROR_CODE} 集中", who, mine,
            f"單一 {mine['client_type'].mode().iloc[0]} 使用者，"
            f"佔該錯誤全部 {len(hits)} 筆中的 {len(mine)} 筆；"
            f"失敗全部落在 {window:.0f} 分鐘的同一個時窗內，"
            f"其中 {repeats} 次的 prompt_len 都是 {top_len:,} 字元"
            f"（同一份 payload 反覆送出）。"
            f"{_describe_cadence(mine['ts_taipei'])}。"
            f"該帳號全期共 {len(whole):,} 個請求、"
            f"失敗率 {100 * float((whole['status_code'] >= 400).mean()):.1f}%，"
            f"全部走 {whole['endpoint'].mode().iloc[0]}／"
            f"{whole['model_family'].mode().iloc[0]}，"
            f"user_agent 只有 {whole['user_agent'].nunique()} 種",
        )

    # (b) 舊模型族集中
    legacy = request[request["model_family"].isin(ANOMALY_LEGACY_FAMILIES)]
    who = dominant(legacy)
    if who is not None:
        mine = legacy[legacy["username"] == who]
        others = legacy[legacy["username"] != who]
        add(
            f"舊模型族（{'/'.join(ANOMALY_LEGACY_FAMILIES)}）集中", who, mine,
            f"該模型族共 {len(legacy):,} 筆、{legacy['username'].nunique()} 人，"
            f"單一使用者就佔 {100 * len(mine) / len(legacy):.1f}%；"
            f"其餘 {others['username'].nunique()} 人合計僅 {len(others)} 筆。"
            f"該使用者 100% 直呼、"
            f"{100 * float((mine['endpoint'] == '/v1/chat/completions').mean()):.0f}%"
            f" 走 /v1/chat/completions，"
            f"user_agent 集中於單一 SDK 版本"
            f"（最常見者佔 {100 * float(mine['user_agent'].value_counts(normalize=True).iloc[0]):.0f}%）。"
            f"{_describe_cadence(mine['ts_taipei'])}——節奏規律，與自動化一致",
        )

    # (c) 離峰時段單人集中
    offpeak = request[request["hour_taipei"].isin(list(ANOMALY_OFFPEAK_HOURS))]
    who = dominant(offpeak)
    if who is not None:
        from src import aggregate

        mine = offpeak[offpeak["username"] == who]
        whole = request[request["username"] == who]
        registry_table = aggregate.load_registry()
        kind = registry_table.loc[registry_table["username"] == who,
                                  "account_type"].squeeze()
        n_direct = int((request["client_type"] == "direct").sum())
        add(
            f"離峰時段（{ANOMALY_OFFPEAK_HOURS.start}–"
            f"{ANOMALY_OFFPEAK_HOURS.stop - 1} 時）單人集中", who, mine,
            f"離峰時段共 {len(offpeak):,} 筆、{offpeak['username'].nunique()} 人，"
            f"其中 {100 * len(mine) / len(offpeak):.1f}% 來自單一使用者。"
            f"該帳號 account_type={kind}（**不是** service 服務帳號），"
            f"全期 {len(whole):,} 個請求佔全體 {100 * len(whole) / total:.1f}%、"
            f"佔 direct 流量 {100 * len(whole) / n_direct:.1f}%，"
            f"其中 {100 * float(whole['hour_taipei'].isin(list(ANOMALY_OFFPEAK_HOURS)).mean()):.1f}%"
            f" 落在離峰時段，活躍天數僅 {whole['date_taipei'].nunique()} 天。"
            f"{_describe_cadence(whole['ts_taipei'])}",
        )

    data = pd.DataFrame(rows, columns=["異常類型", "n_users", "n_requests",
                                       "佔比", "特徵描述"])
    return MetricResult(
        data=data,
        n_total=total,
        # 去重後的筆數。直接加總 n_requests 會超過實際涵蓋量，
        # 因為同一個帳號的請求可能同時被兩列算到。
        n_covered=len(flagged),
        # 「佔比」的分母是全體請求，描述的是一類行為佔多少流量，
        # 不是把人分群後的族群比例，因此不套用抑制。
        ratio_columns=[],
    )
