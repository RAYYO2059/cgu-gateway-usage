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
    # caveat 會被渲染進 INDEX.md，所以它是散文——而散文裡的數值會隨每一批
    # 資料變動。這裡只描述機制、不寫具體數值：寫死的話，下一個改動的人要先
    # 想到「還有一段散文藏在裝飾器的參數裡」才可能發現它過期了。實際數值
    # 由表本身提供，那才是唯一會跟著重算的地方。
    caveat=(
        "top1_account_share 是資訊欄位，不是抑制條件。它衡量的是"
        "「該單位內最大的單一遮罩帳號佔多少請求」，而遮罩帳號編碼了"
        "學位＋入學學年＋系所，所以它近似於「同一個班級」。\n"
        "已知管理學院有一個帳號底下掛著 17 個 uid、佔該學院約八成請求。"
        "那 17 個人過得了 n>=10 的門檻，所以不是再識別風險，而是代表性問題"
        "——該學院的數字其實是在描述一個班。\n"
        "**n_users_in_top_account 因此與 top1_account_share 同列，不是"
        "湊欄位。** 那一欄單獨看無法解讀：同一個佔比，底下是一個人代表"
        "再識別風險（要遮蔽），是十七個人代表代表性問題（要標註），"
        "兩者的補救方式相反。解讀它所需的數必須與它同列——放到另一張表，"
        "讀者會拿著一個沒有意義的百分比自行推論。\n"
        "n_accounts 同理與 n_users 相鄰：兩者的差距就是遮罩造成的碰撞量，"
        "也就是有多少人在遮罩之後彼此分不出來。\n"
        "抑制規則處理的是再識別，兩者不該混用同一個機制：把代表性問題塞進"
        "抑制，會讓「這個數字不能公開」與「這個數字不能一般化」變成同一件事，"
        "而它們的補救方式完全相反（前者是遮蔽，後者是標註）。\n"
        "is_single_dept_college 同理：智慧運算學院只有人工智慧學系一系，"
        "該列的「學院」數字等同系所層級，與其他學院粒度不對等。\n"
        "\n"
        "(a) 涵蓋上限。本表最多只能涵蓋 50.15% 的流量。服務憑證佔 49.85%，"
        "那是**非人流量**——不是「不知道是誰」，是「本來就沒有人」。"
        "服務憑證那一列的 request_share 照常公布：它登記為非自然人分組，"
        "單一識別碼佔 68.9% 不觸發抑制（抑制保護的是自然人）。"
        "attributable_share 欄在該列是 NA，理由不同——它不在可歸屬母體內，"
        "不是被擋下。\n"
        "\n"
        "(b) 最大遮罩帳號佔比反映該單位內同系同屆使用者的集中程度。"
        "此欄不受抑制規則約束——抑制處理的是再識別風險，而集中度屬於"
        "代表性問題。人均請求數在集中度高的單位主要由該單一群組構成，"
        "不宜解讀為該單位整體的使用強度。實際數值見表。\n"
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
        account = part["unit_account"].map(_account_prefix)
        per_account = account.value_counts()
        n_requests = len(part)
        is_person = unit != SERVICE_UNIT
        # 最大遮罩帳號底下掛著幾個識別碼。與 top1_account_share 同源：那一欄
        # 取 per_account 第一名佔多少請求，這一欄取同一個帳號名下有多少個
        # uid。兩者一起算而不是分開——分開就會有人只更新一邊。
        top_account = per_account.index[0] if len(per_account) else None
        rows.append({
            "unit": unit,
            "unit_type": unit_type,
            "n_users": int(part["anonymous_user_id"].nunique()),
            # 與 n_users 相鄰：同一群人的兩種計數，差距即遮罩造成的碰撞。
            "n_accounts": int(account.nunique()),
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
            # 緊接 top1_account_share：那一欄單獨看無法解讀，見 caveat。
            "n_users_in_top_account": (
                int(part.loc[account == top_account,
                             "anonymous_user_id"].nunique())
                if top_account is not None else pd.NA),
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
        "與 requests_by_unit_lite 並排看時，兩張表都沒有任何一列被抑制，"
        "但原因不同：那邊的服務憑證單人佔 68.9%，命中集中度規則，因登記為"
        "非自然人分組而依政策豁免；這邊的分母排除了服務憑證，根本沒有那一列。"
        "**兩張表的判定並無不同。** build_concentration 對每個維度"
        "只算一份規則，兩張表查的是同一份 concentration_lite.csv 的同一列。\n"
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
# 欄位要寫死而不是靠 pd.DataFrame(rows) 從第一列推導。這是**監測指標**：
# 它的正常終點就是空表——代碼補齊之後這裡本來就該是零列。而 rows 為空時
# 推導不出任何欄位，sort_values("n_requests") 會 KeyError，指標整個失敗，
# 然後渲染器發現 csv 不存在、默默保留文件裡的舊表。
# 結果是「缺口已補完」這件好事，表現成一份看起來完全正常但已經過期的報表。
UNMAPPED_COLUMNS = [
    "dept_code", "n_users", "n_requests", "degree_mix",
    "entry_year_range", "account_examples", "note",
]


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
        "本表列出無法對應到學院的系所代碼。空表表示全部學生識別碼都已歸屬。"
        "本表為監測用，後續若出現新的系所代碼會重新有值。"
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
            # note 欄留白：本表是監測表，備註要寫的是「這個代碼為什麼對不到」，
            # 而那件事在代碼出現之前無從得知。針對特定代碼預先寫死一句話，
            # 代碼一旦補進對照表就再也不會執行，只剩一段沒人會發現已經過期的斷言。
            "note": "",
        })

    data = (pd.DataFrame(rows, columns=UNMAPPED_COLUMNS)
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
