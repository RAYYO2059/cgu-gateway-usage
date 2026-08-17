"""basic 指標：直接數欄位，不需要先算一步。

全部只做描述，不做解釋、不做族群比較、不算成本金額。
caveat 欄位是這些指標的主要產出之一——INDEX.md 由 registry 產生，
caveat 寫得爛文件就爛。
"""

from __future__ import annotations

import pandas as pd

from src.metrics.registry import MetricResult, metric

# usage_details 為空 dict 的請求，其 token 是「遺失」不是 0。
# 任何 token 加總都要排除它們，並註明排除筆數。
#
# 這是給人看的參考字串，不參與任何計算。各指標的 caveat 各自寫自己的版本
# （因為排除筆數與適用範圍不同），這裡放的是共用的說法，寫新指標時可以照抄。
# 不要改成由程式組裝：caveat 的措辭要隨指標調整，統一模板反而會讓人
# 把「已排除」寫在根本沒做排除的指標上。
USAGE_MISSING_NOTE = (
    "token 加總已排除 usage_missing=True 的請求（其 token 為遺失而非 0）"
)


# 0=週一。只用在備註字串上，不當分組鍵。
_WEEKDAY_NAMES = ("週一", "週二", "週三", "週四", "週五", "週六", "週日")


def _shares(frame: pd.DataFrame, count_column: str, total: int, name: str):
    frame[name] = (frame[count_column] / total).round(4) if total else pd.NA
    return frame


def _shares_exact(frame: pd.DataFrame, count_column: str, total: int, name: str):
    """同 _shares，但把四捨五入的殘差補回最大的一組，讓比例加總剛好是 1.0000。

    27 個模型族各自 round(4) 之後加總是 0.9997，不是 1。差額純粹來自捨入，
    但讀者無從分辨「捨入誤差」與「分母用錯」——後者正是這個指標剛修掉的 bug。
    把殘差補在最大組上，那一格最多動 0.0001，相對值 3 個數量級以下。
    """
    frame = _shares(frame, count_column, total, name)
    if not total or frame.empty:
        return frame
    residual = round(1.0 - float(frame[name].sum()), 4)
    if residual:
        largest = frame[count_column].idxmax()
        frame.loc[largest, name] = round(float(frame.loc[largest, name]) + residual, 4)
    return frame


# ---------------------------------------------------------------------------
# 1 dataset_scale
# ---------------------------------------------------------------------------
@metric(
    name="dataset_scale",
    question="這批資料有多大？四層母數各是多少、涵蓋哪幾天？",
    unit="request",
    source="request",
    denominator="無分母。這是規模描述，不是比例。",
    caveat=(
        "只涵蓋 2026-07-21 至 2026-07-23 約 1.8 天，且第一天與最後一天都不完整。"
        "不可據此推估日均量或月量，也不可視為 gateway 的完整歷史。"
        "四層母數逐層下降是 agent 展開造成的，不是資料遺失。"
        "每日一列的備註附上星期與當日使用人數（原 requests_by_weekday 指標已併入此處）——"
        "3 個 weekday 值、其中兩個是不完整的一天，"
        "『平日 vs 週末』之類的比較在這份資料上沒有意義，因此不再單獨出表。"
        "「codex 請求無 turn_id」一列是刻意保留的缺口："
        "那些請求不屬於任何 turn，也沒有被塞進任何 turn，"
        "所以以 turn 為母體的指標（turn_expansion_depth、cache_hit_by_request_position）"
        "的母數會比 client_type=codex 的請求數少這幾筆。"
    ),
    version="1.1",
)
def dataset_scale(tables: dict) -> MetricResult:
    request = tables["request"]
    rows = [
        {"項目": "n_requests", "值": float(len(request))},
        {"項目": "n_turns", "值": float(len(tables["turn"]))},
        {"項目": "n_threads", "值": float(len(tables["thread"]))},
        {"項目": "n_users", "值": float(len(tables["user"]))},
        {"項目": "n_days", "值": float(request["date_taipei"].nunique())},
        {"項目": "n_columns", "值": float(request.shape[1])},
    ]
    for day, part in request.groupby("date_taipei", dropna=False):
        weekday = int(part["weekday_taipei"].iloc[0])
        rows.append({
            "項目": f"requests_on_{day}",
            "值": float(len(part)),
            "備註": f"{_WEEKDAY_NAMES[weekday]}（weekday_taipei={weekday}）、"
                    f"{part['username'].nunique()} 人",
        })

    rows.append(_orphan_row(request))
    rows += [
        {"項目": "ts_first", "值": None},
        {"項目": "ts_last", "值": None},
    ]
    data = pd.DataFrame(rows)
    data.loc[data["項目"] == "ts_first", "備註"] = str(request["ts_taipei"].min())
    data.loc[data["項目"] == "ts_last", "備註"] = str(request["ts_taipei"].max())
    return MetricResult(data=data, n_total=len(request), n_covered=len(request))


# 判定「同形狀」用的欄位。這幾欄決定了一個請求是哪一種呼叫：
# 訊息結構（三個 count）加上輸出格式，足以認出「Codex 的結構化輔助呼叫」
# 這一類，又不會細到把每筆請求都變成獨一無二。
_SHAPE_COLUMNS = ("text_format_name", "message_count",
                  "user_message_count", "assistant_message_count")


def _orphan_row(request: pd.DataFrame) -> dict:
    """client_type=codex 但沒有 turn_id 的請求。

    刻意不把它們補進任何 turn。turn 是「一次使用者動作」，
    硬塞進去只會讓展開深度多出一個不存在的動作——
    為了讓兩個母數對齊而製造假資料，比留著缺口糟得多。
    """
    orphans = request[(request["client_type"] == "codex")
                      & request["turn_id"].isna()]
    if orphans.empty:
        return {"項目": "codex 請求無 turn_id", "值": 0.0, "備註": ""}

    def shape_key(frame: pd.DataFrame) -> pd.Series:
        return frame[list(_SHAPE_COLUMNS)].astype("string").fillna("<null>") \
            .agg("|".join, axis=1)

    same_shape = request[shape_key(request).isin(set(shape_key(orphans)))]
    with_turn = int(same_shape["turn_id"].notna().sum())

    def joined(column: str) -> str:
        return ",".join(sorted(str(v) for v in orphans[column].dropna().unique()))

    return {
        "項目": "codex 請求無 turn_id",
        "值": float(len(orphans)),
        "備註": (
            f"{orphans['thread_id'].nunique()} 個 thread、"
            f"{orphans['username'].nunique()} 人；"
            f"全部 endpoint={joined('endpoint')}、status={joined('status_code')}、"
            f"text_format_name={joined('text_format_name')}；"
            f"同形狀的請求另有 {with_turn} 筆帶著 turn_id，"
            "故『缺 turn_id』本身無規律，未併入任何 turn"
        ),
    }


# ---------------------------------------------------------------------------
# 2 requests_by_account_type
# ---------------------------------------------------------------------------
@metric(
    name="requests_by_account_type",
    question="各身分別（學生／教職員／服務帳號）各有多少人、發了多少請求？",
    unit="request",
    source="user",
    denominator="全部 9,937 個請求，依 username 對應的 account_type 分組",
    caveat=(
        "account_type 來自被遮罩的 user_account（末三碼為 XXX），同前綴的不同人會合併，"
        "但身分別的判定本身可信。不可回推到個別帳號。"
        "service 是 link-*/open* 服務憑證，不是人，做人均統計時必須先剔除。"
        "佔比欄位會依集中度規則自動抑制；被抑制的組仍保留計數。"
    ),
    group_by=["account_type"],
    version="1.1",
)
def requests_by_account_type(tables: dict) -> MetricResult:
    user = tables["user"]
    grouped = user.groupby("account_type", dropna=False).agg(
        n_users=("username", "nunique"),
        n_requests=("n_requests", "sum"),
        n_threads=("n_threads", "sum"),
        n_turns=("n_turns", "sum"),
    ).reset_index()
    grouped = _shares(grouped, "n_requests", int(grouped["n_requests"].sum()),
                      "request_share")
    grouped = _shares(grouped, "n_users", int(grouped["n_users"].sum()), "user_share")
    return MetricResult(
        data=grouped.sort_values("n_requests", ascending=False).reset_index(drop=True),
        n_total=len(user),
        n_covered=int(user["account_type"].notna().sum()),
        ratio_columns=["request_share", "user_share"],
    )


# ---------------------------------------------------------------------------
# 3 requests_by_client_type
# ---------------------------------------------------------------------------
@metric(
    name="requests_by_client_type",
    question="Codex 客戶端與直呼（SDK/curl/自寫程式）各佔多少請求、多少人？",
    unit="request",
    source="request",
    denominator="全部 9,937 個請求，依 thread_id 是否存在二分",
    caveat=(
        "direct 這一組不能當成「25 個人的行為」：單一使用者佔該組 token 的 83.6%、"
        "請求數的 61.0%，其行為特徵基本上是一個人的腳本。"
        "direct 的所有比例欄位因此會被抑制，計數保留。"
        "另外 client_type 是用 thread_id 是否為 null 判定的，"
        "有 9 筆 direct 請求帶著 store、6 筆帶著 prompt_cache_key，"
        "代表少數直呼客戶端會手動帶 Responses API 參數。"
    ),
    group_by=["client_type"],
    version="1.0",
)
def requests_by_client_type(tables: dict) -> MetricResult:
    request = tables["request"]
    grouped = request.groupby("client_type", dropna=False).agg(
        n_requests=("request_id", "size"),
        n_users=("username", "nunique"),
        n_threads=("thread_id", "nunique"),
        n_turns=("turn_id", "nunique"),
    ).reset_index()
    grouped = _shares(grouped, "n_requests", len(request), "request_share")
    return MetricResult(
        data=grouped.sort_values("n_requests", ascending=False).reset_index(drop=True),
        n_total=len(request),
        n_covered=int(request["client_type"].notna().sum()),
        ratio_columns=["request_share"],
    )


# ---------------------------------------------------------------------------
# 4 requests_by_endpoint
# ---------------------------------------------------------------------------
@metric(
    name="requests_by_endpoint",
    question="使用者拿 gateway 做什麼？各 API 端點的請求數與使用人數。",
    unit="request",
    source="request",
    denominator="全部 9,937 個請求",
    caveat=(
        "端點是「拿來做什麼」最可靠的結構訊號（對話／語音／轉錄／生圖），"
        "因為它不依賴任何內容判讀。但它只說明呼叫了什麼 API，"
        "不說明使用者的任務目的——同一個 /v1/responses 可能是寫程式也可能是翻譯。"
        "endpoint 分的是請求不是人，因此**不套用抑制**：低請求量的端點"
        "（如 /v1/images/edits 僅 1 筆、1 人）比例照樣輸出，"
        "請自行對照 n_users 判斷母數——一個人的 1 筆請求佔 0.01%，"
        "這個 0.01% 是事實，但它描述的是一個人。"
    ),
    group_by=["endpoint"],
    version="1.0",
)
def requests_by_endpoint(tables: dict) -> MetricResult:
    request = tables["request"]
    grouped = request.groupby("endpoint", dropna=False).agg(
        n_requests=("request_id", "size"),
        n_users=("username", "nunique"),
        n_threads=("thread_id", "nunique"),
    ).reset_index()
    grouped = _shares(grouped, "n_requests", len(request), "request_share")
    return MetricResult(
        data=grouped.sort_values("n_requests", ascending=False).reset_index(drop=True),
        n_total=len(request),
        n_covered=int(request["endpoint"].notna().sum()),
        ratio_columns=["request_share"],
    )


# ---------------------------------------------------------------------------
# 5 requests_by_model_family
# ---------------------------------------------------------------------------
@metric(
    name="requests_by_model_family",
    question="實際服務請求的是哪些模型族？",
    unit="request",
    source="request",
    denominator="有 model_family 的請求（9,935 筆）。缺值的 2 筆單列一行，不進分母。",
    caveat=(
        "使用 model_family（已剝除 -YYYY-MM-DD 版本後綴）而非 model_returned。"
        "不做這步的話，同一個模型的不同快照會被當成不同模型："
        "『請求的模型 ≠ 回傳的模型』的比率會顯示為 57.96%，"
        "剝除版本後實際只有 0.01%（9,935 筆中僅 1 筆真正被替換）。"
        "這個欄位回答『誰在服務』，不回答『使用者選了什麼』——後者要看 model_requested。"
        "**request_share 的分母是 9,935（有 model_family 的請求）不是 9,937。**"
        "表尾的 `(未記錄)` 一列是 model_family 為 null 的 2 筆，"
        "計數照列但不出比例——它不屬於任何模型族，放進分母會讓其他列的比例失真。"
        "因此比例欄加總為 1.0000，計數欄加總為 9,937。"
        "捨入殘差補在請求數最大的那一組（見 _shares_exact），該格最多偏離 0.0001。"
        "model_family 分的是請求不是人，**不套用抑制**；"
        "多數模型族只有個位數使用者，請對照 n_users 再引用比例。"
    ),
    group_by=["model_family"],
    version="1.1",
)
def requests_by_model_family(tables: dict) -> MetricResult:
    request = tables["request"]
    grouped = request.groupby("model_family", dropna=True).agg(
        n_requests=("request_id", "size"),
        n_users=("username", "nunique"),
    ).reset_index()
    covered = int(request["model_family"].notna().sum())
    grouped = _shares_exact(grouped, "n_requests", covered, "request_share")
    grouped = grouped.sort_values("n_requests", ascending=False).reset_index(drop=True)

    # 缺值單列一行：不列的話「表只涵蓋 9,935 筆」這件事只能靠 coverage 反推，
    # 而 coverage 不在 csv 裡。列了才看得見 9,937 與 9,935 的差在哪。
    unknown = request[request["model_family"].isna()]
    if len(unknown):
        # 只指定有值的欄位，request_share 交給 pandas 補 NA。
        # 明寫 pd.NA 會讓 pandas 對整欄重新推斷 dtype 並發 FutureWarning。
        grouped.loc[len(grouped), ["model_family", "n_requests", "n_users"]] = [
            "(未記錄)", int(len(unknown)), int(unknown["username"].nunique()),
        ]
        # 以 loc 加列會把整數欄 upcast 成 float（3236 變成 3236.0）。
        # 計數就該長得像計數，這裡轉回可空整數。
        grouped = grouped.astype({"n_requests": "Int64", "n_users": "Int64"})
    return MetricResult(
        data=grouped,
        n_total=len(request),
        n_covered=covered,
        ratio_columns=["request_share"],
    )


# ---------------------------------------------------------------------------
# 6 requests_by_hour
#
# requests_by_weekday 已移除：它的三個請求數與 dataset_scale 的 requests_on_*
# 逐格相同，只是把索引從日期換成星期碼，而 1.8 天的資料談星期分布沒有意義。
# 它唯一的獨有欄位 n_users 已併入 dataset_scale 的每日備註。
# ---------------------------------------------------------------------------
_TIME_CAVEAT = (
    "資料只涵蓋約 1.8 天（2026-07-21 13:20 至 07-23 07:56，台北時間），"
    "且頭尾兩天都不完整。**完全不足以談週期性、尖峰時段或作息模式**。"
    "這張表只能當成「這 1.8 天內請求落在哪些時段」的事實描述，"
    "不可外推、不可與其他期間比較、不可用來排程或估算容量。"
    "時段分的是請求不是人，**不套用抑制**——「凌晨 3 點只有 2 個人在用」"
    "本身就是要報的事實，把它清成 NA 只會讓讀者以為資料缺漏。"
    "代價是低量時段的比例可能出自一兩個人，請一律對照 n_users 讀。"
)


@metric(
    name="requests_by_hour",
    question="這 1.8 天內，請求落在台北時間的哪些小時？",
    unit="request",
    source="request",
    denominator="全部 9,937 個請求，依 hour_taipei 分組",
    caveat=_TIME_CAVEAT,
    group_by=["hour_taipei"],
    version="1.0",
)
def requests_by_hour(tables: dict) -> MetricResult:
    request = tables["request"]
    grouped = request.groupby("hour_taipei", dropna=False).agg(
        n_requests=("request_id", "size"),
        n_users=("username", "nunique"),
    ).reset_index()
    grouped = _shares(grouped, "n_requests", len(request), "request_share")
    return MetricResult(
        data=grouped.sort_values("hour_taipei").reset_index(drop=True),
        n_total=len(request),
        n_covered=int(request["hour_taipei"].notna().sum()),
        ratio_columns=["request_share"],
    )


# ---------------------------------------------------------------------------
# 7 status_and_errors
# ---------------------------------------------------------------------------
# 這三個狀態碼是「有沒有被技術性阻擋」的關鍵訊號，零筆本身就是結論，
# 所以即使沒出現也要明列，否則讀者只會看到「表裡沒有」而無法區分
# 「沒發生」與「沒統計」。
_WATCHED_STATUS = (429, 402, 403)


@metric(
    name="status_and_errors",
    question="請求的成功率如何？失敗的是哪些錯誤？",
    unit="request",
    source="request",
    denominator="全部 9,937 個請求",
    caveat=(
        "429（額度限制）、402（付款要求）、403（禁止）**皆為零筆**，"
        "代表這段期間沒有任何技術性額度阻擋——"
        "因此『使用量低』不能被解釋成『被擋住了』。"
        "唯一的 520 是上游異常，唯一的 404 是端點不存在。"
        "400 全部是 invalid_request_error，屬於客戶端送錯參數，不是服務故障。"
        "錯誤筆數極少（86/9937），任何依錯誤分組的比例都不可用。"
        "**error_code 區塊含一列 `(無)`**：部分 4xx/5xx 請求的 error 物件沒有 code 欄位"
        "（只有 type 與 param，520 則連 error 物件都沒有）。"
        "列出來是為了讓 error_code 區塊加總等於 error_4xx_5xx；"
        "少了它，兩個數字差幾筆而讀者查不出差在哪。"
    ),
    version="1.1",
)
def status_and_errors(tables: dict) -> MetricResult:
    request = tables["request"]
    rows = []
    for value, count in request["status_code"].value_counts().sort_index().items():
        rows.append({
            "類別": "status_code", "值": str(value), "n_requests": int(count),
            "n_users": int(request.loc[request["status_code"] == value,
                                       "username"].nunique()),
        })
    seen = {int(r["值"]) for r in rows}
    for code in _WATCHED_STATUS:
        if code not in seen:
            rows.append({"類別": "status_code", "值": str(code),
                         "n_requests": 0, "n_users": 0})

    failed = request[request["status_code"] >= 400]
    errors = failed[failed["error_code"].notna()]
    for value, count in errors["error_code"].value_counts().items():
        rows.append({
            "類別": "error_code", "值": str(value), "n_requests": int(count),
            "n_users": int(errors.loc[errors["error_code"] == value,
                                      "username"].nunique()),
        })
    # 沒有 error_code 的失敗請求。不列的話 error_code 區塊會比 error_4xx_5xx 少，
    # 而少掉的那幾筆從表上完全看不出去了哪裡。
    codeless = failed[failed["error_code"].isna()]
    if len(codeless):
        rows.append({
            "類別": "error_code", "值": "(無)",
            "n_requests": int(len(codeless)),
            "n_users": int(codeless["username"].nunique()),
        })
    rows.append({
        "類別": "彙總", "值": "success_2xx",
        "n_requests": int((request["status_code"] < 400).sum()),
        "n_users": int(request.loc[request["status_code"] < 400,
                                   "username"].nunique()),
    })
    rows.append({
        "類別": "彙總", "值": "error_4xx_5xx",
        "n_requests": int((request["status_code"] >= 400).sum()),
        "n_users": int(request.loc[request["status_code"] >= 400,
                                   "username"].nunique()),
    })
    data = pd.DataFrame(rows)
    return MetricResult(
        data=data,
        n_total=len(request),
        n_covered=int(request["status_code"].notna().sum()),
    )


# ---------------------------------------------------------------------------
# 8 users_by_degree_and_entry_year
# ---------------------------------------------------------------------------
@metric(
    name="users_by_degree_and_entry_year",
    question="有哪些學位別與入學學年的學生在使用？",
    unit="user",
    source="user",
    denominator="43 個 account_type=student 的 username",
    caveat=(
        "**只回答「有誰在用」，不回答「誰用得多」。**"
        "幾乎所有分組都會觸發抑制（母數 < 10 或單人佔比 > 30%），"
        "比例欄位大量為 NA，這是預期行為而非計算失敗。"
        "degree/entry_year 來自 user_account 的可見前綴，"
        "遮罩會把同前綴的不同人合併，人數是下界不是精確值。"
        "staff 與 service 帳號沒有這兩個屬性，不在此表內。"
    ),
    group_by=["degree", "entry_year"],
    version="1.0",
)
def users_by_degree_and_entry_year(tables: dict) -> MetricResult:
    user = tables["user"]
    students = user[user["account_type"] == "student"]
    grouped = students.groupby(["degree", "entry_year"], dropna=True).agg(
        n_users=("username", "nunique"),
        n_requests=("n_requests", "sum"),
        n_threads=("n_threads", "sum"),
    ).reset_index()
    grouped = _shares(grouped, "n_users", int(grouped["n_users"].sum()), "user_share")
    grouped = _shares(grouped, "n_requests", int(grouped["n_requests"].sum()),
                      "request_share")
    return MetricResult(
        data=grouped.sort_values(["degree", "entry_year"]).reset_index(drop=True),
        n_total=len(students),
        n_covered=int(students["degree"].notna().sum()),
        ratio_columns=["user_share", "request_share"],
    )


# ---------------------------------------------------------------------------
# 9 reasoning_effort_distribution
# ---------------------------------------------------------------------------
@metric(
    name="reasoning_effort_distribution",
    question="Codex 請求用了哪些推理強度設定？",
    unit="request",
    source="request",
    denominator="7,115 個 client_type=codex 的請求（direct 客戶端不帶這個參數）",
    caveat=(
        "**這是客戶端預設值的分布，不是使用者偏好的分布。**"
        "同一批請求的 include_options 唯一值只有一個"
        "（reasoning.encrypted_content，7,115 筆全同、覆蓋率 71.6% 與 codex 請求數完全吻合），"
        "顯示沒有任何使用者調整過推理相關設定。"
        "因此 effort 的高低只反映 Codex 各版本／各操作路徑的內建預設，"
        "不可解讀成「使用者想要更深的推理」。"
        "reasoning_effort 分的是請求不是人，**不套用抑制**，請對照 n_users 讀。"
    ),
    group_by=["reasoning_effort"],
    version="1.0",
)
def reasoning_effort_distribution(tables: dict) -> MetricResult:
    request = tables["request"]
    codex = request[request["client_type"] == "codex"]
    grouped = codex.groupby("reasoning_effort", dropna=True).agg(
        n_requests=("request_id", "size"),
        n_users=("username", "nunique"),
        n_turns=("turn_id", "nunique"),
    ).reset_index()
    covered = int(codex["reasoning_effort"].notna().sum())
    grouped = _shares(grouped, "n_requests", covered, "request_share")
    return MetricResult(
        data=grouped.sort_values("n_requests", ascending=False).reset_index(drop=True),
        n_total=len(codex),
        n_covered=covered,
        ratio_columns=["request_share"],
    )


# ---------------------------------------------------------------------------
# 10 tool_types_distribution
# ---------------------------------------------------------------------------
@metric(
    name="tool_types_distribution",
    question="thread 裡掛載了哪些類別的工具？",
    unit="thread",
    source="thread",
    denominator="有 tool_types_union 的 thread（其餘 thread 從未回報工具設定）",
    caveat=(
        "（a）這是**宣告掛載**不是**實際呼叫**。"
        "一個 thread 掛了 web_search 不代表它真的搜尋過，"
        "資料裡沒有任何欄位能區分掛載與呼叫。"
        "（b）只有工具**類別**（function/custom/web_search…），沒有工具名稱，"
        "因此無法知道使用者實際用了哪些具體功能。"
        "（c）缺失非隨機：沒有 tool_types 的請求集中在 subagent 與"
        "標題生成之類的輔助呼叫，這些本來就不掛工具，"
        "所以「沒有工具的 thread 比例」不能解讀成「使用者不用工具」。"
        "（d）**比例的分母是有工具宣告的 thread，不是全部 thread。**"
        "欄名為此改成 declared_thread_share。"
        "function 的 share = 1.0 意思是「所有**有宣告**的 thread 都掛了 function」，"
        "不是「所有 thread 都掛了 function」——後者的分母大了 2.35 倍。"
        "分母本身列在表尾兩列（`（分母）有工具宣告的 thread`／"
        "`（分母）無 tool_types 欄位的 thread`），不必回頭查 coverage 才知道。"
        "維度不在 concentration.csv 內，無法自動抑制，請自行對照 n_users。"
    ),
    version="1.1",
)
def tool_types_distribution(tables: dict) -> MetricResult:
    thread = tables["thread"]
    covered = thread[thread["tool_types_union"].notna()]
    absent = thread[thread["tool_types_union"].isna()]

    rows = []
    for tool in sorted({t for value in covered["tool_types_union"]
                        for t in str(value).split(",") if t}):
        mask = covered["tool_types_union"].str.split(",").apply(lambda s: tool in s)
        subset = covered[mask]
        rows.append({
            "tool_type": tool,
            "n_threads": int(len(subset)),
            "n_users": int(subset["username"].nunique()),
            "declared_thread_share":
                round(len(subset) / len(covered), 4) if len(covered) else None,
        })
    data = pd.DataFrame(rows).sort_values("n_threads", ascending=False)

    # 分母進表。只寫在 caveat 裡沒用——沒有人是先讀 caveat 再讀 csv 的，
    # 而 function=1.0 這個數字單看極容易被讀成「所有 thread 都掛了 function」。
    for label, part in (("（分母）有工具宣告的 thread", covered),
                        ("（分母）無 tool_types 欄位的 thread", absent)):
        # 同 requests_by_model_family：不明寫 NA，讓 pandas 自己補。
        data.loc[len(data), ["tool_type", "n_threads", "n_users"]] = [
            label, int(len(part)), int(part["username"].nunique()),
        ]
    data = data.astype({"n_threads": "Int64", "n_users": "Int64"})
    return MetricResult(
        data=data.reset_index(drop=True),
        n_total=len(thread),
        n_covered=len(covered),
    )
