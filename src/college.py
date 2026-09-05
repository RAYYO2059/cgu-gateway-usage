"""系代號 → 學院對照表的載入、檢查與查詢。

對照表在 ``ref/college_mapping.csv``，人工維護，來源是獎學金公告整理出的
系代號清單。一列描述「某個系代號在某段學年區間屬於哪個學院」：

    dept_code, dept_name, college, degrees, dept_key,
    effective_year_start, effective_year_end

其中 ``degrees``（該代碼適用的學制，BMD／B／MD）與 ``dept_key``（系所身分）
是後來補的，理由見底下「同系所在不同學制使用不同代碼」一節。
**``degrees`` 是文件不是查詢鍵**：沒有任何查詢會讀它，它存在的目的是讓下次
看這張表的人知道某個代碼為什麼只涵蓋一部分的人。``dept_key`` 相反，它是真的
會被拿去算的——「單系學院」那類以系所為單位的判定必須數它而不是數代碼。

兩個正規化決定，都是為了避免安靜的錯誤：

- **dept_code 一律補成 2 碼字串。** 學號裡的系代號是 "05"，但這張表是人工
  維護的，用 Excel 打開再存檔會把 "05" 變成 5。字串直接比對的話，那一列
  就從此查不到——不會報錯，只會讓該系所整組落進「(未對應)」。
- **學年一律正規化成 3 碼民國學年（108、115）。** user_registry 的
  entry_year 是學號裡的 2 碼（"12" = 112 學年），對照表寫 3 碼給人看比較
  不會誤讀，所以查詢時兩種寫法都收。

effective_year_start/end 兩端皆含，留空代表該端不設限；兩端都留空就是全期間
適用，絕大多數系代號都屬於這種。只有系所改隸學院時才需要拆成多列。

本模組只負責對照表本身，不碰任何指標定義。

以下四點是建表過程中查證出來的限制，決定了這張表能回答什麼、不能回答什麼。


遮罩導致系所層級不可分
-----------------------
學號末三碼在進入本專案之前就已被遮罩成 XXX，而有兩個系代號底下不只一個系
所，區分的鍵**正好就是被遮罩掉的序號首碼**：

    03：序號 1xx = 醫技系，2xx = 醫放系
    04：序號 1xx/2xx = 護理系，3xx = 學士後護理

這兩對在學院層級同屬醫學院，所以**學院層級不受影響**，03 和 04 都能安全地
回答「醫學院」。但系所層級無法還原——資訊是在資料到達本專案之前就失去的，
不是解析邏輯的問題，再怎麼改程式也拿不回來。

結論：**本專案只報學院層級，不報系所層級。** 任何看起來像系所層級的輸出，
在 03 和 04 這兩格都是錯的。


dept_code 是入學系所，不是現在的系所
-------------------------------------
轉系的學生保留原學號，所以 dept_code 反映的是**入學時**的系所，不是目前的。

以 308 筆大學部獎學金名單交叉驗證，5 筆（1.6%）的名單系別與學號代碼不符，
且不符的方向全部落在相鄰或相關的系所：

    醫技 ↔ 護理    醫技 ↔ 生醫    醫放 ↔ 生醫
    職治 ↔ 護理    機械 ↔ 電子

符合轉系的特徵，不是編碼錯誤。

結論：**不能拿 dept_code 回答「這個人現在讀哪個系」，只能回答「入學時屬於
哪個系」。** 學院層級的誤差上限約 1.6%，且這 5 筆**全部發生在同一學院內**
（前四組同屬醫學院，機械與電子同屬工學院），實際跨學院誤差在這批樣本裡是
0/308。上限之所以還留著 1.6%，是因為跨學院轉系本來就可能發生，只是這批沒抽到。

（原始學號未寫在這裡：這 5 筆的判讀只需要「名單系別 vs 學號代碼」這一組對
應，完整學號是可以還原到個人的資訊，而 src/ 是進版控的。對應關係由
tests/test_college.py 的 test_轉系的五筆都在同一學院內 固定下來。）


智慧運算學院只有一個系
-----------------------
該學院底下僅設人工智慧學系一系，所以「學院」這一層對它沒有任何聚合效果：
該列的數字等同系所層級，甚至等同單一班級。與其他學院並排比較時粒度不對等，
別的學院是數個系加總、它不是。

報表中出現這個學院時**必須加註記**，見 SINGLE_DEPT_COLLEGES 與
is_single_dept_college()。

注意它在對照表裡佔**兩列**（28 大學部、61 碩博士），但仍然只是一個系——
判斷單系與否要數 dept_key 而不是數 dept_code，見下一節。


代碼 28 的沿革
---------------
人工智慧學士學程（111 學年起招生，學號 B1128xxx）自 112 學年起改為人工智慧
學系（B1228 起）。這是**改名，不是代碼重用**：同一個實體、同一個學院，只是
名稱與學制層級變了。

因此對照表只列一列、effective_year 兩端留空。若當成代碼重用而拆成兩列，
111 學年那批人會被切到另一組去，而他們其實一直是同一個單位的學生。


同系所在不同學制使用不同代碼
-----------------------------
三個系的大學部與碩博士班用**不同的系代號**：

    生醫       大學部 09    碩博士 01
    醫工       大學部 26    碩博士 31
    人工智慧   大學部 28    碩博士 61

這解釋了先前那個看起來很奇怪的觀察——對不到學院的 24 個人**全部是碩博士
生、一個大學部都沒有**。當時的解釋是「研究所有獨立的編碼區間」，那只說對了
一半：真正的成因是這三個系換了代碼，再加上五個研究所專屬的所／學程
（00 臨醫、17 免疫、40 管研、15 生物科技產業、32 奈米工程設計）。

含意在於**對照表的鍵不等於系所身分**。dept_code 仍然是唯一鍵（沒有任何代碼
在不同學制下指向不同系所，所以 dept_to_college() 不需要知道學制），但「這個
學院底下有幾個系」這種以系所為單位的問題不能用代碼去數：智慧運算學院有 28
與 61 兩個代碼，卻只有一個系。dept_key 就是為此存在的——成對的代碼共用同一
個值，取其大學部代碼。

（``degrees`` 欄記的是「這個代碼給誰用」，來源是系所代碼表本身，不是從使用
者資料反推的。09/26/28 標成 B、八個研究所代碼標成 MD，這兩組有來源可依。
其餘 17 列沒有來源，分兩種處理：11 列能在 lite registry 裡看到實際的 M 或 D
使用者，標 BMD；另外 6 列（02、06、08、41、42、43）在這批資料裡只看得到大
學部、或根本沒有人用過，標 ``(未查證)``。

**沒查過就不要填一個看起來合理的值。** 六列全填 BMD 的話，讀表的人分不出
「查過，確實三個學制共用」與「沒查過，填了個最常見的值」——前者是資訊，
後者是雜訊，而它們長得一模一樣。這一欄不參與任何查詢，填錯不會讓任何數字
變錯，但會讓下一個人以為這件事已經有人確認過了。判準與 UNMAPPED 不回 None、
與未定價的金額不填 0 相同。）
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path

from src import config

logger = logging.getLogger(__name__)

MAPPING_PATH = config.REF_DIR / "college_mapping.csv"
REGISTRY_PATH = config.REF_DIR / "user_registry.csv"

MAPPING_COLUMNS = (
    "dept_code", "dept_name", "college", "degrees", "dept_key",
    "effective_year_start", "effective_year_end",
)

# degrees 欄的合法值。B = 只給大學部，MD = 只給碩博士，BMD = 三者共用一個代碼。
# 這一欄不參與查詢（見模組 docstring），列舉出來只是為了讓打錯字被驗出來。
DEGREES_SOURCED = frozenset({"BMD", "B", "MD"})

# 「還沒查證過這個代碼給誰用」。與 UNMAPPED、與未定價不填 0 同一個判斷：
# 不知道要看得出來是不知道。
#
# 具體要擋的是這件事：把沒查過的代碼一律填 BMD，讀表的人沒有任何線索可以
# 分辨「查過，確實三個學制共用」與「沒查過，填了個看起來最常見的值」。
# 前者是資訊，後者是雜訊，而它們長得一模一樣。
DEGREES_UNVERIFIED = "(未查證)"

DEGREES_VALUES = DEGREES_SOURCED | {DEGREES_UNVERIFIED}

# 查不到時的回傳值。刻意不是 None：下游多半直接拿去 groupby，
# None 會被 pandas 當成 NA 丟掉，該組資料就這樣無聲消失。
UNMAPPED = "(未對應)"

# 底下只有一個系的學院。這種學院的「學院層級」數字等同系所層級，
# 與其他學院並排時粒度不對等，報表必須加註記（見模組 docstring）。
#
# 「只有一個系」數的是**相異 dept_key 而不是 dept_code**：智慧運算學院有 28
# 與 61 兩個代碼，但那是同一個系的兩個學制。用代碼去數會判定它不再是單系學院，
# 於是註記默默消失——而該學院的粒度問題一點都沒有改變。
SINGLE_DEPT_COLLEGES = frozenset({"智慧運算學院"})


# ---------------------------------------------------------------------------
# 正規化
# ---------------------------------------------------------------------------
def normalize_dept_code(value: object) -> str | None:
    """把系代號正規化成 2 碼字串；空值回 None。

    接受 "05"、"5"、5 三種寫法（Excel 存檔會把前兩種都變成第三種）。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return text.zfill(2)
    # 非純數字的代號原樣保留，交給 check_mapping() 去報。
    return text


def normalize_year(value: object) -> int | None:
    """把學年正規化成 3 碼民國學年整數；空值或無法解析回 None。

    "12"、12、"112"、112 都會得到 112。小於 100 的一律當成學號裡的 2 碼寫法。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        year = int(text)
    except ValueError:
        return None
    if year < 0:
        return None
    return year + 100 if year < 100 else year


# ---------------------------------------------------------------------------
# 資料結構
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MappingEntry:
    dept_code: str
    dept_name: str
    college: str
    year_start: int | None  # None = 不設下限
    year_end: int | None    # None = 不設上限
    line_no: int            # csv 的實際行號，報問題時給人對照用
    raw_dept_code: str = "" # 正規化前的原字串，用來提醒前導零被壓掉了
    degrees: str = ""       # BMD / B / MD，是文件不是查詢鍵
    # 系所身分。成對的代碼（09 與 01、26 與 31、28 與 61）共用同一個值。
    # 留空時 load_mapping() 會填成 dept_code 自己。
    dept_key: str = ""

    def covers(self, year: int | None) -> bool:
        """該列是否適用於某個入學學年。

        year 為 None（不知道學年）時只有「全期間」的列算適用；有年限的列不敢
        猜，留給 dept_to_college() 去判斷要不要收斂。
        """
        if year is None:
            return self.year_start is None and self.year_end is None
        if self.year_start is not None and year < self.year_start:
            return False
        if self.year_end is not None and year > self.year_end:
            return False
        return True

    def overlaps(self, other: "MappingEntry") -> bool:
        """兩列的學年區間是否重疊（留空視為無限）。"""
        start = max(_or(self.year_start, -10 ** 6), _or(other.year_start, -10 ** 6))
        end = min(_or(self.year_end, 10 ** 6), _or(other.year_end, 10 ** 6))
        return start <= end


def _or(value: int | None, fallback: int) -> int:
    return fallback if value is None else value


@dataclass
class MappingProblems:
    """validate_mapping() 的結果：**對照表自身**的正確性，與任何資料無關。

    這些是「不管有沒有使用者資料都成立」的判準，所以任何人 clone 下來都驗得動。
    覆蓋率那類「這批資料裡有哪些代碼查不到」不放在這裡——見 RegistryComparison。
    """

    n_entries: int = 0
    conflicts: list[str] = field(default_factory=list)   # 同代碼、區間重疊、學院不同
    redundant: list[str] = field(default_factory=list)   # 同代碼、區間重疊、學院相同
    bad_rows: list[str] = field(default_factory=list)    # 欄位空白或格式不對
    notes: list[str] = field(default_factory=list)       # 已自動修好，但該讓人知道

    @property
    def ok(self) -> bool:
        # redundant 與 notes 不列入：前者是「多寫了一列但不矛盾」，
        # 後者是「已經自動修好」，兩者都不足以判定對照表是錯的。
        return not (self.conflicts or self.bad_rows)


@dataclass
class RegistryComparison:
    """compare_with_registry() 的結果：對照表與**某一批資料**的覆蓋關係。

    這是操作資訊而不是正確性判準：同一張正確的對照表，換一批資料就會有不同的
    未對應清單。所以它依賴本機資料，而且拿不到資料時要能說「無法比對」，
    不能拋例外——沒有資料是常態（repo 不含原始資料），不是錯誤。
    """

    available: bool = False
    reason: str = ""                                        # available=False 時的說明
    seen: dict[str, int] = field(default_factory=dict)      # 代碼 → 人數
    missing_codes: list[str] = field(default_factory=list)  # registry 有、對照表查不到
    unused_codes: list[str] = field(default_factory=list)   # 對照表有、registry 沒出現過


@dataclass
class MappingReport:
    """check_mapping() 的結果：把上面兩者合起來，給 CLI 用。"""

    problems: MappingProblems = field(default_factory=MappingProblems)
    comparison: RegistryComparison = field(default_factory=RegistryComparison)

    # 讓既有呼叫端不必改寫的轉接屬性。
    @property
    def n_entries(self) -> int:
        return self.problems.n_entries

    @property
    def conflicts(self) -> list[str]:
        return self.problems.conflicts

    @property
    def redundant(self) -> list[str]:
        return self.problems.redundant

    @property
    def bad_rows(self) -> list[str]:
        return self.problems.bad_rows

    @property
    def notes(self) -> list[str]:
        return self.problems.notes

    @property
    def missing_codes(self) -> list[str]:
        return self.comparison.missing_codes

    @property
    def unused_codes(self) -> list[str]:
        return self.comparison.unused_codes

    @property
    def ok(self) -> bool:
        # 與拆分前的判準完全相同：對照表有問題、或這批資料有代碼查不到，
        # 都讓 CLI 回非零。比不了的時候只看對照表自身。
        return self.problems.ok and not self.missing_codes


# ---------------------------------------------------------------------------
# 載入
# ---------------------------------------------------------------------------
def load_mapping(path: Path | None = None) -> list[MappingEntry]:
    """讀入對照表。檔案不存在時給明確訊息，不讓它變成空表安靜跑過去。"""
    path = path or MAPPING_PATH
    if not path.is_file():
        raise FileNotFoundError(
            f"找不到系代號對照表 {path}。\n"
            f"這張表是人工維護的，請先建立，欄位為 {', '.join(MAPPING_COLUMNS)}。"
        )

    # 用 csv 而不是 pandas：pandas 會把 "05" 讀成整數 5、把空白的學年讀成
    # NaN，兩個都要再轉回來，不如一開始就全部當字串處理。
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        missing = [c for c in MAPPING_COLUMNS if c not in header]
        if missing:
            raise ValueError(
                f"{path} 缺少欄位 {missing}；"
                f"應為 {', '.join(MAPPING_COLUMNS)}，實際為 {header}"
            )

        entries: list[MappingEntry] = []
        for line_no, row in enumerate(reader, start=2):
            code = normalize_dept_code(row.get("dept_code"))
            if code is None:
                continue  # 整列空白（例如檔尾多按的 enter），不算問題
            # dept_key 走同一套正規化：它也是系代號，同樣會被 Excel 壓掉前導零。
            # 留空的列預設指向自己——絕大多數系所只有一個代碼，
            # 逼每一列都手寫一次只會多一個打錯字的機會。
            key = normalize_dept_code(row.get("dept_key")) or code
            entries.append(MappingEntry(
                dept_code=code,
                dept_name=(row.get("dept_name") or "").strip(),
                college=(row.get("college") or "").strip(),
                year_start=normalize_year(row.get("effective_year_start")),
                year_end=normalize_year(row.get("effective_year_end")),
                line_no=line_no,
                raw_dept_code=str(row.get("dept_code") or "").strip(),
                degrees=(row.get("degrees") or "").strip().upper(),
                dept_key=key,
            ))
    return entries


def registry_dept_codes(path: Path | None = None) -> dict[str, int]:
    """user_registry 裡實際出現過的系代號 → 該代號的 username 數。"""
    path = path or REGISTRY_PATH
    if not path.is_file():
        raise FileNotFoundError(
            f"找不到 {path}。請先跑過 pipeline 產生 user_registry.csv。"
        )

    counts: dict[str, int] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if "dept_code" not in (reader.fieldnames or []):
            raise ValueError(f"{path} 沒有 dept_code 欄位")
        for row in reader:
            code = normalize_dept_code(row.get("dept_code"))
            if code is not None:  # staff/service 沒有系代號，本來就是空的
                counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------
# 查詢
# ---------------------------------------------------------------------------
def dept_to_college(
    dept_code: object,
    entry_year: object = None,
    entries: list[MappingEntry] | None = None,
) -> str:
    """查系代號屬於哪個學院；查不到一律回 UNMAPPED，不回 None。

    entry_year 可以是學號裡的 2 碼（"12"）或完整民國學年（112），也可以省略。
    省略（或該人根本沒有學年，例如教職員）時，只有候選列的學院一致才回答：
    同一代碼在不同年份分屬不同學院、而又不知道年份，回答哪一個都是猜的。

    entries 是給批次呼叫用的——一次 load_mapping() 之後傳進來，
    否則每查一列就重讀一次 csv。
    """
    code = normalize_dept_code(dept_code)
    if code is None:
        return UNMAPPED

    if entries is None:
        entries = load_mapping()
    year = normalize_year(entry_year)

    candidates = [e for e in entries if e.dept_code == code and e.college]
    if not candidates:
        return UNMAPPED

    if year is not None:
        matched = [e for e in candidates if e.covers(year)]
    else:
        # 學年不明：先看有沒有「全期間」的列，沒有就退回全部候選列，
        # 由下面的一致性判斷決定要不要回答。
        matched = [e for e in candidates if e.covers(None)] or candidates

    colleges = {e.college for e in matched}
    if len(colleges) == 1:
        return colleges.pop()
    return UNMAPPED


def dept_keys_by_college(
    entries: list[MappingEntry] | None = None,
) -> dict[str, set[str]]:
    """學院 → 該學院底下的相異**系所身分**（dept_key）集合。

    刻意回 dept_key 而不是 dept_code：一個系可能因為大學部與碩博士班分開編碼
    而佔兩個代碼（09/01、26/31、28/61），數代碼會把它算成兩個系。
    「這個學院底下有幾個系」只有數 dept_key 才答得對。

    沒有 college 的列（欄位空白）直接跳過——它連屬於哪個學院都不知道，
    放進來只會多一個叫做「」的學院。
    """
    entries = load_mapping() if entries is None else entries
    out: dict[str, set[str]] = {}
    for entry in entries:
        if entry.college:
            out.setdefault(entry.college, set()).add(entry.dept_key)
    return out


def is_single_dept_college(college: object) -> bool:
    """該學院底下是否只有一個系。

    給報表用：True 代表那一列的「學院」其實就是一個系，跟其他學院並排比較時
    要加註記。UNMAPPED 與空值一律回 False——它們不是學院，談不上單不單系。
    """
    if college is None or college != college:
        return False
    return str(college).strip() in SINGLE_DEPT_COLLEGES


# ---------------------------------------------------------------------------
# 檢查
# ---------------------------------------------------------------------------
def _group_by_code(entries: list[MappingEntry]) -> dict[str, list[MappingEntry]]:
    by_code: dict[str, list[MappingEntry]] = {}
    for entry in entries:
        by_code.setdefault(entry.dept_code, []).append(entry)
    return by_code


def validate_mapping(mapping: list[MappingEntry]) -> MappingProblems:
    """只驗對照表自身：欄位格式、學年區間、同代碼衝突與重複。

    **不讀任何檔案、不需要任何使用者資料**，所以在只有 repo 的環境裡也跑得動。
    這是刻意的：對照表對不對是它自己的事，不該因為本機剛好沒有 user_registry
    就驗不了——那會讓「對照表被改壞」這種真正的錯誤在 CI 上完全不被發現。
    """
    problems = MappingProblems(n_entries=len(mapping))

    # --- 欄位本身 ---
    for entry in mapping:
        found = []
        if not entry.dept_code.isdigit() or len(entry.dept_code) != 2:
            found.append(f"dept_code {entry.dept_code!r} 不是 2 碼數字")
        if not entry.college:
            found.append("college 空白")
        if not entry.dept_name:
            found.append("dept_name 空白")
        if entry.degrees not in DEGREES_VALUES:
            found.append(
                f"degrees {entry.degrees!r} 不在 {sorted(DEGREES_VALUES)} 之中")
        if not entry.dept_key.isdigit() or len(entry.dept_key) != 2:
            found.append(f"dept_key {entry.dept_key!r} 不是 2 碼數字")
        if (entry.year_start is not None and entry.year_end is not None
                and entry.year_start > entry.year_end):
            found.append(f"學年區間顛倒（{entry.year_start} > {entry.year_end}）")
        if found:
            problems.bad_rows.append(
                f"第 {entry.line_no} 行 {entry.dept_code}：" + "；".join(found))
        if entry.raw_dept_code and entry.raw_dept_code != entry.dept_code:
            # 查詢時會自動補零，所以不算錯；但 Excel 每存一次就壓掉一次，
            # 不講的話這張表會慢慢爛掉。
            problems.notes.append(
                f"第 {entry.line_no} 行 dept_code 寫成 {entry.raw_dept_code!r}，"
                f"已當作 {entry.dept_code!r} 處理（前導零被 Excel 壓掉了）")

    # --- dept_key 的兩條不變量 ---
    #
    # 這兩條都不是形式檢查而是語意檢查：dept_key 一旦不一致，
    # 「單系學院」這類以系所為單位的判定就會靜默地算錯——不會有例外，
    # 只會有一個少了註記的報表。
    known_codes = {e.dept_code for e in mapping}
    key_of_code = {e.dept_code: e.dept_key for e in mapping}
    by_key: dict[str, list[MappingEntry]] = {}
    for entry in mapping:
        by_key.setdefault(entry.dept_key, []).append(entry)

    for code, group in sorted(_group_by_code(mapping).items()):
        keys = {e.dept_key for e in group}
        if len(keys) > 1:
            problems.conflicts.append(
                f"{code}：同一個代碼被指到多個 dept_key {sorted(keys)}，"
                f"代碼與系所身分必須是多對一")

    for key, group in sorted(by_key.items()):
        if key not in known_codes:
            problems.conflicts.append(
                f"dept_key {key} 沒有對應的 dept_code 列"
                f"（由第 {'、'.join(str(e.line_no) for e in group)} 行指向）；"
                f"系所身分要取其大學部代碼，那一列必須存在")
        elif key_of_code[key] != key:
            # 被指向的那一列必須指向自己。否則 09→01、01→09 這種環會通過上面
            # 每一條檢查，卻讓兩個代碼算成兩個系所身分——「這個學院有幾個系」
            # 就多算了一個，而且完全不會有任何錯誤訊息。
            problems.conflicts.append(
                f"dept_key {key} 那一列自己的 dept_key 是 "
                f"{key_of_code[key]!r}：系所身分必須指向自己，不能再轉一手")
        colleges = {e.college for e in group if e.college}
        if len(colleges) > 1:
            problems.conflicts.append(
                f"dept_key {key} 底下的列分屬不同學院 {sorted(colleges)}："
                f"同一個系不可能同時屬於兩個學院")

    # --- 同代碼重複 ---
    for code, group in sorted(_group_by_code(mapping).items()):
        for i, first in enumerate(group):
            for second in group[i + 1:]:
                if not first.overlaps(second):
                    continue  # 區間不重疊 = 系所改隸，這是合法的
                where = (
                    f"{code}：第 {first.line_no} 行"
                    f"（{_span(first)} → {first.college or '空白'}）"
                    f"與第 {second.line_no} 行"
                    f"（{_span(second)} → {second.college or '空白'}）區間重疊"
                )
                if first.college != second.college:
                    problems.conflicts.append(where + "，且學院不同")
                else:
                    problems.redundant.append(where)

    return problems


def compare_with_registry(
    mapping: list[MappingEntry],
    registry: dict[str, int] | None,
) -> RegistryComparison:
    """對照表對某一批資料的覆蓋率。registry 為 None 時回「無法比對」，不拋例外。

    沒有 registry 是常態而不是錯誤：repo 不含原始資料，剛 clone 下來本來就沒有。
    這種情況要能安靜地說「比不了」，讓呼叫端自己決定要不要在意。
    """
    if registry is None:
        return RegistryComparison(
            available=False,
            reason=f"沒有可比對的 registry（預期位置 {REGISTRY_PATH}）",
        )

    mapped = {c for c, group in _group_by_code(mapping).items()
              if any(e.college for e in group)}
    return RegistryComparison(
        available=True,
        seen=dict(registry),
        missing_codes=[c for c in registry if c not in mapped],
        unused_codes=[c for c in sorted(mapped) if c not in registry],
    )


def check_mapping(
    mapping_path: Path | None = None,
    registry_path: Path | None = None,
) -> MappingReport:
    """CLI 用：驗對照表自身，並在拿得到 registry 時一併比對覆蓋率。

    registry 讀不到就降級成「無法比對」而不是中止——對照表自身的問題比
    覆蓋率重要，不該因為缺一份本機資料就連前者都看不到。
    """
    mapping = load_mapping(mapping_path)
    try:
        registry = registry_dept_codes(registry_path)
    except FileNotFoundError as exc:
        logger.info("跳過覆蓋率比對：%s", str(exc).splitlines()[0])
        registry = None

    return MappingReport(
        problems=validate_mapping(mapping),
        comparison=compare_with_registry(mapping, registry),
    )


def _span(entry: MappingEntry) -> str:
    if entry.year_start is None and entry.year_end is None:
        return "全期間"
    start = entry.year_start if entry.year_start is not None else "…"
    end = entry.year_end if entry.year_end is not None else "…"
    return f"{start}–{end}"


def format_report(report: MappingReport) -> str:
    comparison = report.comparison
    if comparison.available:
        head = (f"對照表 {report.n_entries} 列；"
                f"user_registry 出現過 {len(comparison.seen)} 個系代號。")
    else:
        head = f"對照表 {report.n_entries} 列；{comparison.reason}。"
    lines = [head]

    def section(title: str, items: list[str], unavailable: bool = False) -> None:
        lines.append("")
        if unavailable:
            lines.append(f"{title}：無法比對")
        elif items:
            lines.append(f"{title}（{len(items)}）：")
            lines.extend(f"  - {item}" for item in items)
        else:
            lines.append(f"{title}：無")

    section("衝突：同代碼、區間重疊、學院不同", report.conflicts)
    section("重複：同代碼、區間重疊、學院相同", report.redundant)
    section("欄位有問題的列", report.bad_rows)
    section(
        "registry 有但對照表查不到",
        [f"{code}（{comparison.seen[code]} 人）" for code in report.missing_codes],
        unavailable=not comparison.available,
    )
    section("對照表有但 registry 沒出現過", report.unused_codes,
            unavailable=not comparison.available)
    if report.notes:
        section("已自動處理，但建議回頭修檔案", report.notes)
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    report = check_mapping()
    print(format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
