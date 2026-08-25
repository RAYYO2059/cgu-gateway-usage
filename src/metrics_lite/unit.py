"""lite 的單位（學院／教職員／服務憑證）相關指標。

四個指標回答的是不同層次的問題，刻意分開而不是併成一張大表：

    requests_by_unit_lite    各單位用了多少
    users_by_unit_lite       各單位的人均與分布
    unmapped_dept_codes_lite 歸不到學院的是誰
    unit_coverage_lite       這個維度總共能覆蓋多少

後兩個是診斷表：它們回答的是「這張表能不能信」，而不是「數字是多少」。
併進主表的話，覆蓋缺口會變成主表的一列，讀者會把它當成一個單位來讀。
"""

from __future__ import annotations

import pandas as pd

from src import aggregate_lite, college
from src.metrics.registry import MetricResult, metric

# 服務憑證不是人。它有 uid、有請求，但背後是自動化流程而不是使用者，
# 所以任何「人均」或「這個單位佔多少」的分母都不該包含它。
SERVICE_UNIT = aggregate_lite.UNIT_SERVICE
PERSON_TYPES = ("student", "staff")


def _account_prefix(account: object) -> str | None:
    """遮罩帳號的 local part，例如 B1228XXX。"""
    if account is None or account != account:
        return None
    return str(account).split("@", 1)[0]


# ---------------------------------------------------------------------------
@metric(
    name="requests_by_unit_lite",
    line="lite",
    question="各單位（學院／教職員／服務憑證）各用了多少請求？",
    unit="request",
    source="request",
    denominator=(
        "兩個分母並列，刻意不合併：\n"
        "request_share 的分母是全部 120,520 筆請求（含服務憑證）；\n"
        "attributable_share 的分母只有可歸屬到個人的請求"
        "（學生 + 教職員），服務憑證那一列在這一欄是 NA。\n"
        "服務憑證在第二個分母裡不存在，不是因為被排除，而是因為它不是人"
        "——它沒有可以當作人均分母的東西。兩欄相加沒有意義。"
    ),
    caveat=(
        "top1_account_share 是資訊欄位，不是抑制條件。它衡量的是"
        "「該單位內最大的單一遮罩帳號佔多少請求」，而遮罩帳號編碼了"
        "學位＋入學學年＋系所，所以它近似於「同一個班級」。\n"
        "已知管理學院有一個帳號底下掛著 17 個 uid、佔該學院約八成請求。"
        "那 17 個人過得了 n>=10 的門檻，所以不是再識別風險，而是代表性問題"
        "——該學院的數字其實是在描述一個班。\n"
        "抑制規則處理的是再識別，兩者不該混用同一個機制：把代表性問題塞進"
        "抑制，會讓「這個數字不能公開」與「這個數字不能一般化」變成同一件事，"
        "而它們的補救方式完全相反（前者是遮蔽，後者是標註）。\n"
        "is_single_dept_college 同理：智慧運算學院只有人工智慧學系一系，"
        "該列的「學院」數字等同系所層級，與其他學院粒度不對等。\n"
        "\n"
        "(a) 涵蓋上限。本表最多只能涵蓋 50.15% 的流量。服務憑證佔 49.85%，"
        "那是**非人流量**——不是「不知道是誰」，是「本來就沒有人」。"
        "request_share 欄不會加總到 100%，因為服務憑證那一列在該欄是 NA。\n"
        "\n"
        "(b) 高 top1_account_share 的單位，人均與總量實質上由單一班級決定。"
        "管理學院 78.7%、智慧運算學院 77.1%——這兩個學院的人均請求數"
        "（366.2 與 355.5）約為工學院（203.8）與醫學院（171.3）的兩倍，"
        "但那個數字幾乎完全由一個班貢獻。準確的說法是「有兩個班在密集使用」，"
        "不是「該學院使用強度高」。\n"
        "現有抑制規則對這兩列不會有反應：兩者都過 n>=10，單人佔比分別是"
        "23.85% 與 29.46%，都在 30% 門檻內。**這個訊息只能由本欄取得**，"
        "看抑制狀態是看不出來的。\n"
        "\n"
        "(c) 教職員 120 人沒有單位資訊，這是**資料層的缺口**——上游帳號格式"
        "本身不編碼單位（identity.classify_account 對教職員一律回 None），"
        "補系代號對照表也解決不了。"
    ),
    group_by=["unit"],
)
def requests_by_unit_lite(tables: dict) -> MetricResult:
    request = tables["request"]
    user = tables["user"]

    # 請求表本身也有 user_account，merge 會撞成 _x/_y。用人層級表的那一份
    # （每個 uid 單值，見 identity_lite），先改名再 merge。
    identity = user[["anonymous_user_id", "unit", "unit_type", "account_type",
                     "user_account"]].rename(
        columns={"user_account": "unit_account"})
    joined = request.drop(columns=["user_account"], errors="ignore").merge(
        identity, on="anonymous_user_id", how="left")

    total = len(request)
    attributable = int(joined["account_type"].isin(PERSON_TYPES).sum())

    rows = []
    for (unit, unit_type), part in joined.groupby(["unit", "unit_type"],
                                                  dropna=False):
        per_user = part["anonymous_user_id"].value_counts()
        per_account = part["unit_account"].map(_account_prefix).value_counts()
        n_requests = len(part)
        is_person = unit != SERVICE_UNIT
        rows.append({
            "unit": unit,
            "unit_type": unit_type,
            "n_users": int(part["anonymous_user_id"].nunique()),
            "n_requests": n_requests,
            "request_share": round(n_requests / total, 4) if total else None,
            # 服務憑證不是人 → 這一欄對它沒有意義，填 NA 而不是 0。
            # 填 0 會讓它看起來「佔可歸屬流量的 0%」，那是個假陳述。
            "attributable_share": (
                round(n_requests / attributable, 4)
                if is_person and attributable else pd.NA),
            "top1_user_share": (round(float(per_user.iloc[0]) / n_requests, 4)
                                if n_requests else None),
            "top1_account_share": (
                round(float(per_account.iloc[0]) / n_requests, 4)
                if n_requests and len(per_account) else pd.NA),
            # 不論抑制與否都要有值：讓它在不同版本間忽隱忽現，讀者會以為
            # 「這次沒有標」代表「這次不是單系學院」。
            "is_single_dept_college": college.is_single_dept_college(unit),
        })

    data = (pd.DataFrame(rows)
            .sort_values("n_requests", ascending=False)
            .reset_index(drop=True))
    return MetricResult(
        data=data, n_total=total, n_covered=total,
        # top1_* 是集中度的描述，抑制掉它們等於把「這組被一個人主導」這個
        # 事實也遮掉——而那正是讀者最需要知道的。只抑制兩個佔比欄。
        ratio_columns=["request_share", "attributable_share"],
    )


# ---------------------------------------------------------------------------
@metric(
    name="users_by_unit_lite",
    line="lite",
    question="各單位的人均請求數與分布如何？",
    unit="user",
    source="user",
    denominator=(
        "275 人（學生 155 + 教職員 120）。服務憑證的 82 個 uid 排除："
        "它們不是人，把它們算進人均會讓分母混入非人實體。\n"
        "**因此本表不含服務憑證那一列。**"
    ),
    caveat=(
        "分位數欄位（p25/p50/p75/p90）與比例欄位一起列入受抑制欄位。"
        "當一組被單人主導時，它的中位數就是在描述那個人——"
        "這是沿用 clean 的 prompt_length_distribution 的判斷。\n"
        "\n"
        "與 requests_by_unit_lite 並排看時會發現抑制狀態不一致："
        "那邊的服務憑證被抑制（單人佔 68.9%），這邊沒有任何一列被抑制。"
        "**那不是抑制規則對兩張表判得不同。** build_concentration 對每個維度"
        "只算一份規則，兩張表查的是同一份 concentration_lite.csv 的同一列；"
        "差別純粹在於本表的分母排除了服務憑證，所以根本沒有那一列可以被抑制。\n"
        "抑制規則沒有狀態相依性——同一個 (維度, 分組值) 在任何一張表裡"
        "都會得到相同的判定。"
    ),
    group_by=["unit"],
)
def users_by_unit_lite(tables: dict) -> MetricResult:
    user = tables["user"]
    people = user[user["account_type"].isin(PERSON_TYPES)].copy()
    total_people = len(people)

    rows = []
    for (unit, unit_type), part in people.groupby(["unit", "unit_type"],
                                                  dropna=False):
        requests = part["n_requests"].fillna(0).astype("int64")
        rows.append({
            "unit": unit,
            "unit_type": unit_type,
            "n_users": len(part),
            "user_share": round(len(part) / total_people, 4) if total_people else None,
            "n_requests": int(requests.sum()),
            "mean_requests": round(float(requests.mean()), 1),
            "p25": int(requests.quantile(0.25)),
            "p50": int(requests.quantile(0.50)),
            "p75": int(requests.quantile(0.75)),
            "p90": int(requests.quantile(0.90)),
            "max": int(requests.max()),
            "is_single_dept_college": college.is_single_dept_college(unit),
        })

    data = (pd.DataFrame(rows)
            .sort_values("n_users", ascending=False)
            .reset_index(drop=True))
    return MetricResult(
        data=data, n_total=len(user), n_covered=total_people,
        ratio_columns=["user_share", "mean_requests",
                       "p25", "p50", "p75", "p90", "max"],
    )


# ---------------------------------------------------------------------------
@metric(
    name="unmapped_dept_codes_lite",
    line="lite",
    question="哪些系代號對不到學院？各自涉及多少人與請求？",
    unit="user",
    source="user",
    denominator="155 個學生 uid。教職員與服務憑證沒有系代號，不在分母內。",
    caveat=(
        "這是診斷表，不是分組統計：它回答的是「學院這個維度的覆蓋缺口在哪」，"
        "所以不宣告 group_by——把它當成分組指標會讓覆蓋缺口變成主表的一列，"
        "讀者會誤以為那是一個單位。\n"
        "實測這六個代碼底下的 24 個人**全部是碩士或博士（M 14、D 10），"
        "一個大學部（B）都沒有**；相對地，對得到學院的 131 個學生裡有 84 個是 B。"
        "對照表的來源是大學部獎學金公告，這解釋了為什麼它涵蓋不到這些代碼"
        "——它們落在研究所專屬的編碼區間。\n"
        "代碼 00 另外標記：目前看到的三個人全是博士生，而 00 這個值本身比較"
        "像哨兵值或佔位值而不是真實系所代碼。本專案已經被哨兵值咬過一次"
        "（空字串 turn_id 被 groupby 併成同一個假群組），所以在查證之前"
        "不猜它對應哪個系。\n"
        "\n"
        "要再取一份研究所系代碼名單的話，優先涵蓋 01 與 61："
        "兩者合計 15 人（佔未對應**人數** 62.5%）、2,118 筆"
        "（佔未對應**請求** 56.5%）。01 跨 09–15 學年、9 人，"
        "是跨度最大也是人數最多的一組。\n"
        "人數與請求兩個口徑要分開講：若目標是補齊請求覆蓋率，優先序會變成"
        "61（44.7%）與 31（35.2%），兩者合計 79.9%，而 01 只佔 11.8%。"
        "補齊哪一個取決於你要修的是「多少人歸不了院」還是「多少流量歸不了院」。"
    ),
)
def unmapped_dept_codes_lite(tables: dict) -> MetricResult:
    user = tables["user"]
    students = user[user["account_type"] == "student"]
    unmapped = students[students["unit"] == college.UNMAPPED]

    rows = []
    for code, part in unmapped.groupby("dept_code", dropna=False):
        degrees = part["degree"].value_counts().to_dict()
        years = sorted(y for y in part["entry_year"].dropna().unique())
        accounts = sorted({_account_prefix(a) for a in part["user_account"]
                           if _account_prefix(a)})
        rows.append({
            "dept_code": code,
            "n_users": len(part),
            "n_requests": int(part["n_requests"].fillna(0).sum()),
            "degree_mix": "、".join(f"{k}×{v}" for k, v in sorted(degrees.items())),
            "entry_year_range": (f"{years[0]}–{years[-1]}" if len(years) > 1
                                 else (years[0] if years else "")),
            "account_examples": "、".join(accounts[:4]),
            "note": ("★ 待查證：值本身像哨兵／佔位值，且目前只見博士生"
                     if code == "00" else ""),
        })

    data = (pd.DataFrame(rows)
            .sort_values("n_requests", ascending=False)
            .reset_index(drop=True))
    return MetricResult(data=data, n_total=len(students),
                        n_covered=len(unmapped), ratio_columns=[])


# ---------------------------------------------------------------------------
@metric(
    name="unit_coverage_lite",
    line="lite",
    question="unit 這個維度能覆蓋多少請求？缺口在哪一段？",
    unit="request",
    source="request",
    denominator="全部 120,520 筆請求。每一列的分母寫在該列的『佔比基準』欄。",
    caveat=(
        "這張表的用途是先回答「這個維度能不能用」，再談數字。"
        "服務憑證佔了將近一半的流量卻不對應任何人，所以任何以全部流量為分母的"
        "單位佔比，天花板都只有五成左右。\n"
        "教職員那一段是**資料層的缺口不是分析的限制**：教職員編號沒有系所語意"
        "（identity.classify_account 一律回 None），所以就算補齊系代號對照表，"
        "教職員也永遠歸不到學院。"
    ),
)
def unit_coverage_lite(tables: dict) -> MetricResult:
    request = tables["request"]
    user = tables["user"]

    joined = request.merge(
        user[["anonymous_user_id", "unit", "unit_type", "account_type"]],
        on="anonymous_user_id", how="left")

    total = len(joined)
    is_person = joined["account_type"].isin(PERSON_TYPES)
    attributable = int(is_person.sum())
    is_student = joined["account_type"] == "student"
    mapped = int((is_student & (joined["unit"] != college.UNMAPPED)).sum())
    unmapped = int((is_student & (joined["unit"] == college.UNMAPPED)).sum())
    staff = int((joined["account_type"] == "staff").sum())
    service = total - attributable

    def row(label, n, base_label, base, note=""):
        return {
            "層次": label,
            "n_requests": n,
            "佔比基準": base_label,
            "佔比": round(n / base, 4) if base else None,
            "備註": note,
        }

    rows = [
        row("全部請求", total, "全部請求", total),
        row("可歸屬個人（學生＋教職員）", attributable, "全部請求", total,
            "其餘為服務憑證，不是人"),
        row("　└ 學生且能歸到學院", mapped, "可歸屬個人", attributable),
        row("　└ 學生但對不到學院", unmapped, "可歸屬個人", attributable,
            "見 unmapped_dept_codes_lite"),
        row("　└ 教職員（無單位資訊）", staff, "可歸屬個人", attributable,
            "教職員編號沒有系所語意，屬資料層缺口而非分析限制"),
        row("服務憑證（非人）", service, "全部請求", total,
            "自動化流程，無法也不應歸屬到單位"),
    ]
    data = pd.DataFrame(rows)
    return MetricResult(data=data, n_total=total, n_covered=attributable,
                        ratio_columns=[])
