"""src/college.py 的測試。

每一個測試對應模組 docstring 裡的一條陳述——docstring 講的是這張表能回答
什麼、不能回答什麼，而那些話會隨著對照表被編輯而悄悄變成假的。測試就是
用來讓它們一旦變假就吵出來。

跑法：pytest tests/ 或 python tests/test_college.py（不需要 pytest）。

用的是 ref/college_mapping.csv 本尊，不是 fixture：要驗的正是那張人工維護的
表本身，換成假資料就只剩下在測 csv reader。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import college  # noqa: E402

ENTRIES = college.load_mapping()


def _lookup(dept_code, entry_year=None):
    return college.dept_to_college(dept_code, entry_year, ENTRIES)


# --- 遮罩導致系所層級不可分 -------------------------------------------------
# 03（醫技系／醫放系）與 04（護理系／學士後護理）底下各有多個系所，
# 區分的鍵是被遮罩掉的序號首碼。學院層級不受影響，兩者都必須答得出醫學院。
def test_遮罩的兩個代碼在學院層級仍可解析():
    assert _lookup("03") == "醫學院"
    assert _lookup("04") == "醫學院"


def test_遮罩的兩個代碼在表上記著兩個系名():
    # docstring 說 03 與 04 底下各有多個系所。系代碼表的來源只寫一個系名，
    # 照抄就會把「這裡有兩個系」這件事洗掉——而洗掉之後看起來完全正常，
    # 只是從此沒有人知道 03 不只是醫技系。
    names = {e.dept_code: e.dept_name for e in ENTRIES}
    assert names["03"] == "醫技系/醫放系"
    assert names["04"] == "護理系/學士後護理"


def test_遮罩的兩個代碼給了學年也一樣():
    # 全期間適用的列，不該因為有沒有給學年而改變答案。
    for year in (None, "11", 111, "114"):
        assert _lookup("03", year) == "醫學院"
        assert _lookup("04", year) == "醫學院"


# --- dept_code 是入學系所，不是現在系所 -------------------------------------
# 308 筆獎學金名單裡有 5 筆的名單系別與學號代碼不符（轉系）。
# docstring 主張這 5 筆全部落在同一學院內，所以學院層級不受影響。
# 對照表若把 22 或 27 改隸到別的學院，這個主張就不再成立，要吵出來。
#
# 每組是（名單上的系別代碼, 學號裡的 dept_code）。
TRANSFER_CASES = (
    ("04", "03"),  # 名單護理系，學號代碼 03（醫技／醫放）
    ("06", "04"),  # 名單職治系，學號代碼 04（護理／學士後護理）
    ("03", "09"),  # 名單醫技系，學號代碼 09（生醫）
    ("03", "09"),  # 名單醫放系，學號代碼 09（生醫）
    ("27", "22"),  # 名單電子系，學號代碼 22（機械）
)


def test_轉系的五筆都在同一學院內():
    for listed_code, id_code in TRANSFER_CASES:
        listed = _lookup(listed_code)
        from_id = _lookup(id_code)
        assert listed == from_id, (
            f"名單系別代碼 {listed_code} 屬 {listed}，"
            f"學號代碼 {id_code} 屬 {from_id}：跨學院了，"
            f"docstring 的「學院層級不受影響」不再成立")


def test_轉系的五筆在系所層級確實不同():
    # 反面：如果代碼相同，那就不是轉系案例，樣本挑錯了。
    for listed_code, id_code in TRANSFER_CASES:
        assert listed_code != id_code


# --- 代碼 28 的沿革 ---------------------------------------------------------
# 學士學程（111 學年 B1128xxx）改名為學系（112 學年起 B1228），
# 屬改名而非代碼重用，所以改名前後必須落在同一個學院。
def test_代碼28一律是智慧運算學院():
    assert _lookup("28") == "智慧運算學院"


def test_代碼28改名前後同屬一個學院():
    assert _lookup("28", 111) == _lookup("28", 112) == "智慧運算學院"


# --- 智慧運算學院只有一個系 -------------------------------------------------
def test_智慧運算學院被標為單系學院():
    assert college.is_single_dept_college("智慧運算學院") is True


def test_其他學院不是單系學院():
    for name in ("醫學院", "工學院", "管理學院"):
        assert college.is_single_dept_college(name) is False


def test_未對應與空值不算單系學院():
    # 這兩個不是學院，談不上單不單系；回 True 會讓報表對它們加錯註記。
    assert college.is_single_dept_college(college.UNMAPPED) is False
    assert college.is_single_dept_college(None) is False
    assert college.is_single_dept_college("") is False


# --- 同系所在不同學制使用不同代碼 -------------------------------------------
# 三個系的大學部與碩博士班分開編碼。兩個代碼必須落在同一個學院——
# 若不是，那就不是「同一個系的兩個學制」而是兩個系，dept_key 就配錯了。
DEGREE_CODE_PAIRS = (
    ("09", "01", "醫學院"),        # 生醫系 / 生醫所
    ("26", "31", "工學院"),        # 醫工系 / 醫工所
    ("28", "61", "智慧運算學院"),  # 人工智慧學系 / 人工智慧系
)


def test_成對的學制代碼指向同一個學院():
    for undergrad, graduate, expected in DEGREE_CODE_PAIRS:
        assert _lookup(undergrad) == expected
        assert _lookup(graduate) == expected


def test_成對的學制代碼共用同一個系所身分():
    keys = {e.dept_code: e.dept_key for e in ENTRIES}
    for undergrad, graduate, _ in DEGREE_CODE_PAIRS:
        assert keys[graduate] == keys[undergrad] == undergrad, (
            f"{graduate} 的 dept_key 應為其大學部代碼 {undergrad}，"
            f"實際是 {keys[graduate]!r}")


def test_沒有成對關係的代碼指向自己():
    paired = {g for _, g, _ in DEGREE_CODE_PAIRS}
    for entry in ENTRIES:
        if entry.dept_code not in paired:
            assert entry.dept_key == entry.dept_code


def test_研究所專屬代碼各自成一個系所身分():
    # 這五個不是誰的第二個代碼，它們就是自己。錯把它們併到別的 dept_key，
    # 該學院的系所數就會少一個。
    keys = {e.dept_code: e.dept_key for e in ENTRIES}
    for code in ("00", "15", "17", "32", "40"):
        assert keys[code] == code


def test_degrees欄的值都在允許集合裡():
    for entry in ENTRIES:
        assert entry.degrees in college.DEGREES_VALUES, (
            f"第 {entry.line_no} 行 {entry.dept_code} 的 degrees "
            f"是 {entry.degrees!r}")


def test_未查證與有來源的學制值分得開():
    # 兩者必須是不同的值域。若 (未查證) 混進 DEGREES_SOURCED，
    # 「這個代碼有人查過」就再也問不出來了——而那正是這一欄存在的理由。
    assert college.DEGREES_UNVERIFIED not in college.DEGREES_SOURCED
    assert college.DEGREES_UNVERIFIED in college.DEGREES_VALUES
    assert college.DEGREES_VALUES == college.DEGREES_SOURCED | {
        college.DEGREES_UNVERIFIED}


# docstring 點名這六個代碼沒有來源可依。清單寫在這裡是為了擋反方向的改動：
# 把 (未查證) 換成 BMD 不會有任何錯誤訊號，表看起來反而更完整了。
UNVERIFIED_CODES = ("02", "06", "08", "41", "42", "43")


def test_沒有來源的六個代碼標成未查證():
    degrees = {e.dept_code: e.degrees for e in ENTRIES}
    for code in UNVERIFIED_CODES:
        assert degrees[code] == college.DEGREES_UNVERIFIED, (
            f"{code} 在 docstring 裡列為沒有來源可依，卻標成 "
            f"{degrees[code]!r}。真的查證過的話，docstring 的那份清單"
            f"要一起改——否則下一個人會以為這六列從來沒被查過")


def test_未查證的代碼不多不少就是那六個():
    # 只驗「那六個是未查證」擋不住有人再把第七列降級。兩個方向都要鎖。
    actual = {e.dept_code for e in ENTRIES
              if e.degrees == college.DEGREES_UNVERIFIED}
    assert actual == set(UNVERIFIED_CODES), (
        f"對照表裡未查證的是 {sorted(actual)}，"
        f"docstring 說的是 {sorted(UNVERIFIED_CODES)}")


def test_有來源的代碼不得標成未查證():
    # 三對學制代碼與五個研究所專屬代碼都有代碼表可依。把它們降級成
    # (未查證) 不會有任何錯誤訊號，只會讓已經查到的事實安靜地消失。
    sourced = {u for u, _, _ in DEGREE_CODE_PAIRS}
    sourced |= {g for _, g, _ in DEGREE_CODE_PAIRS}
    sourced |= {"00", "15", "17", "32", "40"}
    degrees = {e.dept_code: e.degrees for e in ENTRIES}
    for code in sorted(sourced):
        assert degrees[code] in college.DEGREES_SOURCED, (
            f"{code} 有來源可依，不該標成 {degrees[code]!r}")


def test_成對代碼的學制標註互補():
    # 大學部那個代碼標 B、碩博士那個標 MD。若有一邊標成 BMD，
    # 就表示有人把「這個代碼涵蓋全部學制」寫進了一張明明是分開編碼的表。
    degrees = {e.dept_code: e.degrees for e in ENTRIES}
    for undergrad, graduate, _ in DEGREE_CODE_PAIRS:
        assert degrees[undergrad] == "B"
        assert degrees[graduate] == "MD"


# --- 查不到的代碼 -----------------------------------------------------------
# 先前 01、15、61 對不到，現在都對得到了。這條測試改用一個確定不存在於
# 對照表的代碼——查不到要回 UNMAPPED 而不是 None，None 會被 pandas 當 NA
# 在 groupby 時丟掉，那一組資料就這樣無聲消失。
UNKNOWN_CODE = "99"


def test_查不到的代碼回未對應():
    assert UNKNOWN_CODE not in {e.dept_code for e in ENTRIES}
    assert _lookup(UNKNOWN_CODE) == college.UNMAPPED


def test_未對應不是None():
    assert _lookup(UNKNOWN_CODE) is not None
    assert college.UNMAPPED == "(未對應)"


# --- 學年寫法正規化 ---------------------------------------------------------
# registry 的 entry_year 是學號裡的 2 碼，對照表寫 3 碼，兩種都要收。
def test_學年2碼3碼int結果一致():
    assert _lookup("28", "11") == _lookup("28", "111") == _lookup("28", 111)
    assert _lookup("28", "11") == "智慧運算學院"


def test_學年正規化本身():
    assert college.normalize_year("11") == 111
    assert college.normalize_year(111) == 111
    assert college.normalize_year("111") == 111
    assert college.normalize_year("08") == 108
    assert college.normalize_year("") is None
    assert college.normalize_year(None) is None


# --- dept_code 正規化 -------------------------------------------------------
# Excel 打開 csv 再存檔會把 "05" 變成 5，補零是為了讓那種檔案還查得到。
def test_前導零被壓掉仍查得到():
    assert _lookup("5") == _lookup("05") == "醫學院"
    assert _lookup(5) == _lookup("05")


def test_代碼正規化本身():
    assert college.normalize_dept_code("5") == "05"
    assert college.normalize_dept_code(5) == "05"
    assert college.normalize_dept_code(" 05 ") == "05"
    assert college.normalize_dept_code("") is None
    assert college.normalize_dept_code(None) is None


# --- 對照表本身 -------------------------------------------------------------
# 用 validate_mapping() 而不是 check_mapping()：後者還會去讀 ref/user_registry.csv
# 算覆蓋率，而那個檔含遮罩帳號、被 gitignore，clone 下來不會有。對照表對不對是
# 它自己的事，不該因為本機剛好沒有使用者資料就驗不了。
#
# 「registry 有哪些代碼查不到」已經不在測試裡——它驗的是本機資料的狀態而不是
# 對照表的正確性，同一張正確的表換一批資料就會有不同答案。那件事由
# `python -m src.college` 的覆蓋率報告負責，並在拿不到 registry 時說「無法比對」。
def test_對照表沒有衝突或格式問題():
    problems = college.validate_mapping(ENTRIES)
    assert problems.conflicts == []
    assert problems.redundant == []
    assert problems.bad_rows == []
    assert problems.ok


def test_覆蓋率比對在沒有registry時不拋例外():
    # 這是「clone 下來就能跑」的關鍵：拿不到資料要能安靜降級。
    comparison = college.compare_with_registry(ENTRIES, None)
    assert comparison.available is False
    assert comparison.reason
    assert comparison.missing_codes == []


def test_覆蓋率比對能算出未對應的代碼():
    # 不碰檔案系統，直接餵一個假的 registry，驗比對邏輯本身。
    fake = {"29": 5, "99": 1}
    comparison = college.compare_with_registry(ENTRIES, fake)
    assert comparison.available is True
    assert comparison.missing_codes == ["99"]   # 對照表沒有 99
    assert "29" not in comparison.missing_codes  # 29 有對到


def test_單系學院常數與對照表一致():
    # SINGLE_DEPT_COLLEGES 是手寫的，對照表改了它不會自動跟上。
    #
    # 數的是**相異 dept_key 而不是 dept_code**：智慧運算學院有 28 與 61 兩個
    # 代碼，但那是同一個系的兩個學制。用代碼去數會判定它有兩個系、於是不再是
    # 單系學院，註記就這樣消失了——而該學院的粒度問題一點都沒有改變。
    #
    # 注意這條驗的是「常數與對照表一致」，**不是**「對照表與學校現實一致」。
    # 若某學院實際增設了系所、而對照表還沒補上那一列，兩邊仍然一致，這條測試
    # 照樣會過——它擋得住的是有人改了 csv 卻忘了改常數，擋不住 csv 本身過期。
    # 對照表與現實的落差只能靠人去核，沒有測試代得掉。
    keys = college.dept_keys_by_college(ENTRIES)
    actual = {c for c, group in keys.items() if len(group) == 1}
    assert actual == set(college.SINGLE_DEPT_COLLEGES), (
        f"對照表算出來的單系學院是 {sorted(actual)}，"
        f"但 SINGLE_DEPT_COLLEGES 寫的是 {sorted(college.SINGLE_DEPT_COLLEGES)}")


def test_智慧運算學院有兩個代碼但只有一個系():
    # 這條把「代碼數 ≠ 系所數」直接釘住。上一條測試只看最後的集合相不相等，
    # 兩邊同時算錯時它會過；這條看的是中間的數字。
    keys = college.dept_keys_by_college(ENTRIES)
    codes = {e.dept_code for e in ENTRIES if e.college == "智慧運算學院"}
    assert codes == {"28", "61"}
    assert keys["智慧運算學院"] == {"28"}
    assert college.is_single_dept_college("智慧運算學院") is True


def _main() -> int:
    """不裝 pytest 也能跑。"""
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, func in tests:
        try:
            func()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通過")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
