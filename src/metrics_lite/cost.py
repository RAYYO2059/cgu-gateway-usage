"""lite 的成本指標。

成本先前只是 ``pricing_check.py`` 的一次性輸出——它有數字，但沒有分母宣告、
沒有 caveat、不進 ``runs/`` 快照、不走抑制規則。這個專案的可信度建立在
方法學貼著數字走，所以成本必須跟其他指標一樣被 ``@metric`` 包起來。

五個指標分工：

    cost_by_unit_lite          誰花的
    cost_by_model_family_lite  花在哪個模型、四段各多少
    cost_by_day_lite           逐日的量與錢（時間序列，也是兩張圖的資料來源）
    cost_structure_lite        錢的形狀（四段、可靠度、長上下文）
    cost_coverage_lite         有多少流量根本算不出金額

拆成五張而不是一張大表，理由同 ``unit.py``：最後兩張是診斷表，回答「這個數字
能不能信」，併進主表會讓它們變成主表的一列而被當成一個分組來讀。

``cost_by_day_lite`` 存在的理由不只是「想看逐日」：兩張逐日的圖需要一個資料
來源，而直接在繪圖模組裡讀 parquet 重算會繞過整個指標系統——那樣的數字沒有
分母宣告、沒有 caveat、不進 ``runs/`` 快照，圖上的線就成了唯一的紀錄。


為什麼要用 module 層級的快取
----------------------------
``pricing.estimate_frame`` 是逐列呼叫 ``estimate_cost``（刻意不向量化，見該
函式註解），12 萬列要跑幾秒。四個指標各算一次就是四倍。

快取掛在 ``tables`` dict 上而不是模組全域：``runner.run_all`` 每次執行都會
呼叫 ``load_tables_lite()`` 產生新的 dict，所以快取的生命週期剛好是一次執行。
掛在模組全域的話，同一個行程裡換一批資料重跑會拿到上一批的成本——而且不會
報錯，只會安靜地算錯。
"""

from __future__ import annotations

import pandas as pd

from src import aggregate_lite, college, pricing
from src.metrics.registry import MetricResult, metric

SERVICE_UNIT = aggregate_lite.UNIT_SERVICE
PERSON_TYPES = ("student", "staff")

_CACHE_KEY = "_cost_lite"

# pricing.py docstring 的四條限制。四個指標共用，寫成常數而不是各自抄一份——
# 抄的話遲早漂移，而漂移的症狀是「同一個估算在不同表裡有不同的但書」。
PRICING_CAVEAT = (
    "本估算是**牌價等值成本，不是實際帳單**。四項系統性差距無法從 lite 資料"
    "判斷（不是精度問題，是資料裡沒有那個欄位）：\n"
    "(1) service_tier 一律假設 Standard。官方分四級（Batch 半價、Flex 半價、"
    "Fast 兩倍、Standard 預設），lite schema 沒有這個欄位。若實際大量使用 "
    "Batch，本估算會**高估一倍**；用 Fast 則低估一半。這是四項裡影響最大的。\n"
    "(2) 區域處理加價未計。2026-03-05 之後發布且符合資料落地資格的模型走區域"
    "端點會加收 10%，無法從資料判斷 gateway 走的是全球還是區域端點。"
    "**待向單位確認**；若成立，gpt-5.4 之後的模型金額需上調 10%。\n"
    "(3) 促銷價有時效。gpt-5.6-sol 自 2026-08-22 起的價格是官方標明的促銷價，"
    "至少維持到 2026-11-21；該日之後用新價目表重算同一批資料，數字會變。\n"
    "(4) **本估算的可重現性性質與其他指標不同**。其他指標都是資料的函數："
    "資料不變，重跑必然一樣。成本是「資料 × 外部價目 × 取價時間」的函數，"
    "所以「同一批資料在不同時間重算得到不同數字」在這裡不是 bug，是性質。"
    "要重現一份成本數字必須同時固定資料與 ref/pricing_table.csv；"
    "可重現性綁在該表的 fetched_at 上，不綁在資料上。"
    "引用本估算的文件請一併註明取價日期。"
)

# 每一格獨立四捨五入到分，所以分組加總會與總額差幾分（實測 1–3 分）。
# 刻意不把殘差補進最大的那一組：clean 的 _shares_exact 那樣做是為了讓比例
# 加總剛好 1.0000（比例本來就是導出量），但金額是錢——把 3 分錢塞給某個
# 學院會讓那一列的數字不再是那個學院的實際金額。差幾分是四捨五入的事實，
# 寫進 caveat 讓讀者知道就好，不要為了表面整齊去動數字。
ROUNDING_NOTE = (
    "各列 cost_usd 獨立四捨五入到分，**分組加總會與總額差幾分**"
    "（實測 1–3 分）。殘差刻意不補進任何一組：那會讓該列不再是它的實際金額。"
    "要精確的總額請看 cost_structure_lite 的『總金額』列。"
)

# 未計價的請求在加總時當 0，所以總額是**下界**。這一句必須跟著每一個
# 總額走——它與「cost_usd 欄區分 0 與 NA」是同一件事的兩面：逐列看得出
# 哪些不知道，加總時那些不知道被當成 0，於是總額只可能偏低不可能偏高。
LOWER_BOUND_NOTE = (
    "**總額是下界。** 查不到牌價（unpriced_no_table）與非 token 計價"
    "（unpriced_non_token）的請求在加總時當 0，它們的錢確實花了但本估算"
    "算不出來。逐列的 cost_usd 欄對這兩種填 NA 而不是 0，加總時才當 0"
    "——所以「某一列是 NA」與「總額偏低」是同一件事的兩面。"
    "缺口有多大請看 cost_coverage_lite。"
)

# 兩個母數。成本總額與人均的分母不同，而且不能互推。
DENOMINATOR_TWO_BASES = (
    "兩個母數並列，刻意不合併：\n"
    "**成本總額的分母是全部 120,520 筆請求**（含服務憑證、含未計價的那些）。"
    "服務憑證佔了將近一半流量，它產生的成本是真的花掉的錢，不能排除。\n"
    "**人均的分母是 275 人**（學生 155 + 教職員 120）。服務憑證的 82 個 uid "
    "不是人，把它們算進人均會讓分母混入非人實體——所以服務憑證那一列的 "
    "cost_per_user 是 NA，不是 0。\n"
    "兩個分母不可互推：用 275 去除總額會把非人流量的錢攤到人頭上。"
)


def _account_prefix(account: object) -> str | None:
    """遮罩帳號的 local part，例如 B1228XXX。"""
    if account is None or account != account:
        return None
    return str(account).split("@", 1)[0]


def _cost_frame(tables: dict) -> pd.DataFrame:
    """請求表逐列的成本估算，同一次執行內只算一次。"""
    cached = tables.get(_CACHE_KEY)
    if cached is not None:
        return cached
    table = pricing.PricingTable.from_csv()
    estimate = pricing.estimate_frame(tables["request"], table)
    tables[_CACHE_KEY] = estimate
    return estimate


def _joined(tables: dict) -> pd.DataFrame:
    """請求 × 人員屬性 × 成本，索引與請求表對齊。"""
    request = tables["request"]
    user = tables["user"]
    identity = user[["anonymous_user_id", "unit", "unit_type", "account_type",
                     "user_account"]].rename(columns={"user_account": "unit_account"})
    # 請求表本身也有 user_account，不改名 merge 會撞成 _x/_y（見 unit.py）。
    base = request.drop(columns=["user_account"], errors="ignore").merge(
        identity, on="anonymous_user_id", how="left")
    cost = _cost_frame(tables)
    for column in ("cost", "pricing_status", "confidence", "model_family",
                   "input_cost", "cached_cost", "cache_write_cost", "output_cost",
                   "context_tier", "prompt_tokens", "completion_tokens"):
        base[f"_{column}"] = cost[column].values
    # 加總用的欄位：未知當 0。總額因此是**下界**，這一點寫進每個指標的 caveat。
    #
    # **但顯示用的值不能用它。** fillna(0) 會把三種語意壓成同一個 0.00：
    # 本地模型（真的不花錢）、查不到牌價（不知道）、非 token 計價（不適用）。
    # 讀者看到的不是「資料可疑」，而是一個明確但錯誤的結論——這些模型沒花錢。
    # 顯示值一律走 group_cost()。
    base["_cost_filled"] = base["_cost"].fillna(0.0)
    return base


def group_cost(part: pd.DataFrame, column: str = "_cost_filled"):
    """一組請求的金額，**區分「不花錢」與「不知道」**。

        有任何 priced 的請求      → 實際金額（若同組另有未計價的，那是下界）
        全部是本地模型            → 0.0，那是事實
        其餘（無牌價／非 token）  → NA，我們不知道

    這是 ENGINEERING_NOTES〈事後補寫的紀錄不是紀錄〉那條「未定價不填 0」在指標層的實作。原本這裡直接
    ``sum(fillna(0))``，把 pricing.py 特地設計的四值 pricing_status 抹平成
    一個數字——上游分得很細，下游一行 fillna 就還原回去了。
    """
    status = part["_pricing_status"]
    if (status == pricing.STATUS_PRICED).any():
        return round(float(part[column].fillna(0).sum()), 2)
    if (status == pricing.STATUS_UNPRICED_LOCAL).all():
        return 0.0
    return pd.NA


# ---------------------------------------------------------------------------
@metric(
    name="cost_by_unit_lite",
    line="lite",
    question="各單位（學院／教職員／服務憑證）各花了多少錢？",
    unit="request",
    source="request",
    denominator=DENOMINATOR_TWO_BASES,
    caveat=(
        "**cost_per_user 只對可歸屬到個人的單位有意義**，服務憑證該欄為 NA。"
        "分母是該單位的 uid 數，而 uid 不等於人：同一組遮罩帳號底下可能是"
        "同一個人的多個工作階段，也可能是一整個班。所以人均是**下界**"
        "——實際人數只會更少，人均只會更高。\n"
        "\n"
        "top1_account_share 是資訊欄位不是抑制條件，語意同 requests_by_unit_lite："
        "遮罩帳號編碼了學位＋入學學年＋系所，近似於「同一個班級」。"
        "高 top1_account_share 的單位，其成本實質上由單一班級決定——那是"
        "**代表性問題不是再識別風險**，兩者的補救方式相反（標註 vs 遮蔽），"
        "所以不共用抑制機制。現有抑制規則對這些列不會有反應，"
        "這個訊息只能由本欄取得。\n"
        "\n"
        "cost_per_request 未列入受抑制欄位：它是每筆請求的平均金額，描述的是"
        "流量的形狀不是某個人的行為。cost_share 與 cost_per_user 則會被抑制"
        "——後者在單人主導的組裡就是在描述那個人的花費。\n"
        "\n"
        "教職員 120 人沒有單位資訊（上游帳號格式不編碼單位），"
        "所以「教職員」是一整列而不是分散到各學院，這是資料層缺口。\n"
        "\n" + LOWER_BOUND_NOTE + "\n"
        "\n" + ROUNDING_NOTE + "\n"
        "\n" + PRICING_CAVEAT
    ),
    group_by=["unit"],
    version="1.0",
)
def cost_by_unit_lite(tables: dict) -> MetricResult:
    joined = _joined(tables)
    total_requests = len(joined)
    total_cost = float(joined["_cost_filled"].sum())

    rows = []
    for (unit, unit_type), part in joined.groupby(["unit", "unit_type"],
                                                  dropna=False):
        n_requests = len(part)
        n_users = int(part["anonymous_user_id"].nunique())
        cost = float(part["_cost_filled"].sum())
        shown_cost = group_cost(part)
        per_user = part["anonymous_user_id"].value_counts()
        per_account = part["unit_account"].map(_account_prefix).value_counts()
        is_person = unit != SERVICE_UNIT
        rows.append({
            "unit": unit,
            "unit_type": unit_type,
            "n_users": n_users,
            "n_requests": n_requests,
            "cost_usd": shown_cost,
            "cost_share": round(cost / total_cost, 4) if total_cost else None,
            "cost_per_request": round(cost / n_requests, 6) if n_requests else None,
            # 服務憑證不是人 → 沒有可以當人均分母的東西。填 NA 而不是 0：
            # 填 0 會讓它看起來「人均花費為零」，那是個假陳述。
            "cost_per_user": (round(cost / n_users, 2)
                              if is_person and n_users else pd.NA),
            "top1_user_share": (round(float(per_user.iloc[0]) / n_requests, 4)
                                if n_requests else None),
            "top1_account_share": (
                round(float(per_account.iloc[0]) / n_requests, 4)
                if n_requests and len(per_account) else pd.NA),
            "is_single_dept_college": college.is_single_dept_college(unit),
        })

    data = (pd.DataFrame(rows)
            .sort_values("cost_usd", ascending=False)
            .reset_index(drop=True))
    return MetricResult(
        data=data, n_total=total_requests, n_covered=total_requests,
        ratio_columns=["cost_share", "cost_per_user"],
    )


# ---------------------------------------------------------------------------
@metric(
    name="cost_by_model_family_lite",
    line="lite",
    question="錢花在哪些模型族？四段（未快取 prompt／快取讀／快取寫／輸出）各多少？",
    unit="request",
    source="request",
    denominator=(
        "全部 120,520 筆請求，依 model_family（已剝除 -YYYY-MM-DD 版本後綴）分組。"
        "cost_share 的分母是總金額，request_share 的分母是總請求數——"
        "兩者的排序不一樣，那個差異本身就是要看的東西。\n"
        "未計價的請求（本地模型、非 token 端點、無價目）金額算 0 但**仍在分母內**，"
        "所以它們會拉低自己那一列的 cost_share 而不是被排除。"
    ),
    caveat=(
        "model_family 是**豁免抑制**的維度：它分的是請求不是人"
        "（見 aggregate_lite.EXEMPT_DIMENSIONS_LITE）。代價是低量模型族的比例"
        "可能出自一兩個人，因此本表一律附 n_users，請對照著讀。\n"
        "\n"
        "四段金額相加等於 cost_usd。四段的意義不同：\n"
        "  input_cost        未命中快取的 prompt token\n"
        "  cached_cost       命中快取的 prompt token（費率遠低於未快取）\n"
        "  cache_write_cost  寫入快取的 token（部分模型族才有這一段）\n"
        "  output_cost       completion token\n"
        "**快取讀寫佔比高不是浪費，是 agent 迴圈的必然結果**——每一輪都要重送"
        "整段對話歷史，第二輪之後幾乎全部命中快取。解釋這個機制的指標在 clean "
        "那條線（cache_hit_by_request_position、token_inflation_by_client_type），"
        "lite 因為缺 turn_id 做不出來。\n"
        "\n" + LOWER_BOUND_NOTE + "\n"
        "\n" + ROUNDING_NOTE + "\n"
        "\n" + PRICING_CAVEAT
    ),
    group_by=["model_family"],
    version="1.0",
)
def cost_by_model_family_lite(tables: dict) -> MetricResult:
    joined = _joined(tables)
    total_requests = len(joined)
    total_cost = float(joined["_cost_filled"].sum())

    rows = []
    for family, part in joined.groupby("_model_family", dropna=False):
        n_requests = len(part)
        cost = float(part["_cost_filled"].sum())
        # 四段與總額都走 group_cost：一個模型族若一筆都沒有計價成功，
        # 它的四段也是「不知道」而不是四個 0。
        shown_cost = group_cost(part)
        segments = {key: group_cost(part, f"_{key}")
                    for key in ("input_cost", "cached_cost",
                                "cache_write_cost", "output_cost")}
        rows.append({
            "model_family": family if isinstance(family, str) else "（未記錄）",
            "n_users": int(part["anonymous_user_id"].nunique()),
            "n_requests": n_requests,
            "request_share": round(n_requests / total_requests, 4)
            if total_requests else None,
            "cost_usd": shown_cost,
            "cost_share": round(cost / total_cost, 4) if total_cost else None,
            "input_cost": segments["input_cost"],
            "cached_cost": segments["cached_cost"],
            "cache_write_cost": segments["cache_write_cost"],
            "output_cost": segments["output_cost"],
            "pricing_status": "、".join(
                f"{k}×{v}" for k, v in
                sorted(part["_pricing_status"].value_counts().items())),
        })

    # cost_usd 現在可能是 NA（不知道），排序要明講擺哪裡——預設會把 NA
    # 放最後，但寫出來才不會在 pandas 改預設時靜默換位。
    data = (pd.DataFrame(rows)
            .sort_values("cost_usd", ascending=False, na_position="last")
            .reset_index(drop=True))
    return MetricResult(
        data=data, n_total=total_requests, n_covered=total_requests,
        ratio_columns=["request_share", "cost_share"],
    )


# ---------------------------------------------------------------------------
@metric(
    name="cost_structure_lite",
    line="lite",
    question="這筆錢的形狀是什麼？四段各佔多少、價目有多可靠、長上下文吃掉多少？",
    unit="request",
    source="request",
    denominator=(
        "全部 120,520 筆請求。這是**結構描述不是分組統計**，"
        "每一區塊的分母寫在該列的『佔比基準』欄。\n"
        "price_confidence 區塊的分母是總金額，且**「涉及筆數」跨列相加會超過"
        "總筆數**——一筆請求的四段可以來自不同可靠度的價目，它的錢確實一部分"
        "來自明列價、一部分來自推估價，所以同一筆會同時出現在多個桶裡。"
        "金額欄則是可加總的，四個桶相加等於總額。"
    ),
    caveat=(
        "**price_confidence 與 pricing_status 是兩件事**：後者回答「這筆能不能"
        "計價」，前者回答「用來計價的那個數字有多硬」。混成一欄之後，"
        "「有多少筆算不出來」與「有多少筆算得出來但價格存疑」就再也分不開。\n"
        "\n"
        "可靠度必須按**段金額**加權，不能用「筆數 × 該筆的 confidence」。"
        "8/22 前的 gpt-5.6-sol 只有快取寫入是推估價、其餘三段官方明列——"
        "整筆算成 inferred 會得出「一半的金額建立在推估價上」，"
        "而按段加權的實際值是 13.6%。前者會讓讀者質疑整份估算，"
        "而那個質疑是我們自己製造的。\n"
        "\n"
        "confidence=low 是**資料品質**標記不是價目問題：2026-08-18 與 08-19 "
        "兩天有 1,596 筆 2xx 請求的 token 記成 0，那兩天的金額因此偏低。"
        "與 price_confidence 是獨立的兩個軸，不要混讀。\n"
        "\n"
        "\n" + LOWER_BOUND_NOTE + "\n"
        "\n"
        "長上下文（prompt_tokens > 272,000）適用另一組約兩倍的價格。"
        "它的筆數佔比極低但金額佔比不成比例，這正是要單獨列出來的理由。\n"
        "\n" + PRICING_CAVEAT
    ),
    version="1.0",
)
def cost_structure_lite(tables: dict) -> MetricResult:
    joined = _joined(tables)
    cost = _cost_frame(tables)
    total_requests = len(joined)
    total_cost = float(joined["_cost_filled"].sum())

    def row(section, label, n, amount, base_label, base, note=""):
        return {
            "區塊": section,
            "項目": label,
            "n_requests": n,
            "cost_usd": round(amount, 2) if amount is not None else pd.NA,
            "佔比基準": base_label,
            "佔比": round(amount / base, 4) if (amount is not None and base) else None,
            "備註": note,
        }

    rows = [row("彙總", "總金額", total_requests, total_cost, "總金額", total_cost)]

    # --- 四段 ---
    for key, label, note in (
        ("input_cost", "未快取 prompt", ""),
        ("cached_cost", "快取讀", "費率遠低於未快取"),
        ("cache_write_cost", "快取寫", "部分模型族才有這一段"),
        ("output_cost", "輸出", ""),
    ):
        amount = float(joined[f"_{key}"].fillna(0).sum())
        n = int((joined[f"_{key}"].fillna(0) > 0).sum())
        rows.append(row("四段拆分", label, n, amount, "總金額", total_cost, note))

    # --- price_confidence（按段金額加權）---
    breakdown = pricing.confidence_breakdown(cost)
    for record in breakdown.to_dict("records"):
        rows.append(row(
            "price_confidence", str(record["price_confidence"]),
            int(record["涉及筆數"]), float(record["金額"]),
            "總金額", total_cost,
            "涉及筆數跨列相加會超過總筆數（一筆的四段可分屬不同桶）"))

    # --- 長上下文 ---
    long_mask = joined["_context_tier"] == pricing.TIER_LONG
    rows.append(row(
        "長上下文", f"prompt_tokens > {pricing.LONG_CONTEXT_THRESHOLD:,}",
        int(long_mask.sum()), float(joined.loc[long_mask, "_cost_filled"].sum()),
        "總金額", total_cost, "適用另一組約兩倍的價格"))

    # --- 資料品質標記 ---
    low_mask = joined["_confidence"] == pricing.CONFIDENCE_LOW
    rows.append(row(
        "資料品質", "confidence=low（08-18／08-19）",
        int(low_mask.sum()), float(joined.loc[low_mask, "_cost_filled"].sum()),
        "總金額", total_cost,
        "那兩天有 1,596 筆 2xx 請求 token 記成 0，金額偏低"))

    data = pd.DataFrame(rows)
    return MetricResult(data=data, n_total=total_requests,
                        n_covered=total_requests, ratio_columns=[])


# ---------------------------------------------------------------------------
@metric(
    name="cost_coverage_lite",
    line="lite",
    question="有多少流量算得出金額？算不出來的分別是為什麼？",
    unit="request",
    source="request",
    denominator=(
        "全部 120,520 筆請求，依 pricing_status 四分且互斥。"
        "token 欄是 prompt_tokens + completion_tokens 的合計。"
    ),
    caveat=(
        "**這張表要先讀，再讀金額。** 它回答的是「成本數字涵蓋多少流量」，"
        "沒有它，總額看起來像全部流量的成本，實際上有一段根本沒被計價。\n"
        "\n"
        "四種算不出來的原因，性質完全不同，不可合併成一個「其他」：\n"
        "  priced              有價目、算得出來\n"
        "  unpriced_local      本地模型（ollama），**本來就不花錢**——"
        "這一段的 0 是事實不是缺口\n"
        "  unpriced_non_token  語音、圖片等非 token 計價端點。"
        "**這些端點還是會回報 token 數**，不擋掉的話會被文字費率算進去，"
        "錯得很像對的\n"
        "  unpriced_no_table   有 token、該計價，但價目表沒有這個 model_family"
        "——**這一段才是真正的缺口**，補價目表就能消掉\n"
        "\n"
        "\n" + LOWER_BOUND_NOTE + "\n"
        "\n"
        "只有最後一種需要行動。把四種混成「未計價 X%」會讓「本來就免費」"
        "與「我們還沒查到價格」變成同一個數字，而它們的處置相反。\n"
        "\n" + PRICING_CAVEAT
    ),
    version="1.0",
)
def cost_coverage_lite(tables: dict) -> MetricResult:
    joined = _joined(tables)
    total_requests = len(joined)
    tokens = (joined["_prompt_tokens"].fillna(0)
              + joined["_completion_tokens"].fillna(0))
    total_tokens = float(tokens.sum())

    notes = {
        pricing.STATUS_PRICED: "有價目，金額計入總額",
        pricing.STATUS_UNPRICED_LOCAL: "本地模型，本來就不花錢——0 是事實不是缺口",
        pricing.STATUS_UNPRICED_NON_TOKEN:
            "非 token 計價端點；它們仍會回報 token 數，不擋掉會被文字費率算進去。"
            "金額為 NA 不是 0：它按張／按秒計價，本表算不出來",
        pricing.STATUS_UNPRICED_NO_TABLE:
            "★ 真正的缺口：有 token、該計價，但價目表沒有這個 model_family。"
            "金額為 NA 不是 0——這一段的錢確實花了，只是我們算不出來",
    }

    rows = []
    for status in (pricing.STATUS_PRICED, pricing.STATUS_UNPRICED_LOCAL,
                   pricing.STATUS_UNPRICED_NON_TOKEN,
                   pricing.STATUS_UNPRICED_NO_TABLE):
        mask = joined["_pricing_status"] == status
        n = int(mask.sum())
        tok = float(tokens[mask].sum())
        rows.append({
            "pricing_status": status,
            "n_users": int(joined.loc[mask, "anonymous_user_id"].nunique()),
            "n_requests": n,
            "request_share": round(n / total_requests, 4) if total_requests else None,
            "tokens": int(tok),
            "token_share": round(tok / total_tokens, 4) if total_tokens else None,
            # 每一列就是一種 pricing_status，所以 group_cost 在這裡等於：
            # priced 給實際金額、local 給 0、其餘給 NA。**不可一律 round(sum)**
            # ——那會讓「查不到牌價」印成 0.00，讀成「這些沒花錢」。
            "cost_usd": group_cost(joined[mask]),
            "備註": notes[status],
        })

    data = pd.DataFrame(rows)
    priced = int((joined["_pricing_status"] == pricing.STATUS_PRICED).sum())
    return MetricResult(data=data, n_total=total_requests, n_covered=priced,
                        ratio_columns=[])


# ---------------------------------------------------------------------------
@metric(
    name="cost_by_day_lite",
    line="lite",
    question="逐日的請求數、使用人數與成本各是多少？",
    unit="request",
    source="request",
    denominator=(
        "全部 120,520 筆請求，依 date_taipei 分組。"
        "**每一列的分母是那一天自己**，不是全期——cost_per_request 是當日的"
        "平均，不可跨日相加。全期的平均請自行用總額除以總筆數，"
        "不要對本表的 cost_per_request 取平均（那會給每一天相同的權重，"
        "而各日的請求數相差近百倍）。"
    ),
    caveat=(
        "**這張表有三個不能當成使用行為的日子。**\n"
        "(a) 資料期間內有兩個曆日完全沒有請求，它們不會出現在表裡。"
        "那是**服務中斷不是資料遺失**（已查證：中斷 73.12 小時，兩側邊界都在"
        "日中，兩份匯出報告的 skipped_records 都是 0）。畫圖時要留空缺，"
        "把它們連起來會讓那段看起來像使用量平滑下降再回升。\n"
        "(b) **首末兩天都被匯出視窗截斷**，不是使用量的變化。匯出以 UTC 整日為"
        "單位，而本表分的是台北日，兩者差 8 小時：\n"
        "  2026-07-30 首筆 08:00:19，缺台北時間的前 8 小時（6,205 筆／15.8 小時）\n"
        "  2026-08-24 末筆 01:48:58，只有 1.8 小時（232 筆、9 個人）\n"
        "首日的截斷是純時區（UTC 00:00 = 台北 08:00）；末日是時區再加上交付在 "
        "UTC 日中途停止，兩層。其餘 22 日的首末筆都落在 00:0x–23:5x，涵蓋完整。\n"
        "**(a) 與 (b) 要分開講**：匯出視窗截斷是我們拿到的資料有缺，服務中斷"
        "則是那段時間真的沒有請求——後者不是資料品質問題，它本身就是要報的觀察。\n"
        "\n"
        "date_taipei 是**豁免抑制**的維度：它分的是請求不是人"
        "（見 aggregate_lite.EXEMPT_DIMENSIONS_LITE）。所以低量日的比例照樣"
        "輸出，請對照 n_users 讀——最後一天的 9 個人就是這條的用途。\n"
        "\n"
        "cost_per_request 的日間變異遠大於請求數的變異（實測 16.8 倍 vs "
        "近百倍的請求數變異，但兩者不同步）。它反映的是當日的模型組合與快取"
        "狀態，不是使用強度——請求數最高的兩天，每筆成本反而是全期最低。\n"
        "\n" + LOWER_BOUND_NOTE + "\n"
        "\n" + ROUNDING_NOTE + "\n"
        "\n" + PRICING_CAVEAT
    ),
    group_by=["date_taipei"],
    version="1.0",
)
def cost_by_day_lite(tables: dict) -> MetricResult:
    joined = _joined(tables)
    total_requests = len(joined)
    total_cost = float(joined["_cost_filled"].sum())
    # 分區鍵回讀是 category dtype，groupby 會補出所有類別（含空的）。
    # 轉成字串再分組，表裡才不會出現筆數為 0 的幽靈日期。
    day = joined["date_taipei"].astype(str)

    rows = []
    for date, part in joined.groupby(day, dropna=False):
        n_requests = len(part)
        cost = float(part["_cost_filled"].sum())
        tokens = float((part["_prompt_tokens"].fillna(0)
                        + part["_completion_tokens"].fillna(0)).sum())
        rows.append({
            "date_taipei": date,
            "n_users": int(part["anonymous_user_id"].nunique()),
            "n_requests": n_requests,
            "request_share": round(n_requests / total_requests, 4)
            if total_requests else None,
            "tokens": int(tokens),
            "cost_usd": round(cost, 2),
            "cost_share": round(cost / total_cost, 4) if total_cost else None,
            "cost_per_request": round(cost / n_requests, 6) if n_requests else None,
        })

    data = (pd.DataFrame(rows)
            .sort_values("date_taipei")
            .reset_index(drop=True))
    return MetricResult(
        data=data, n_total=total_requests, n_covered=total_requests,
        ratio_columns=["request_share", "cost_share"],
    )
