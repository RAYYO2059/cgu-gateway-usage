"""lite 這條線的指標。

全部宣告 line="lite"，名稱一律加 _lite 後綴——REGISTRY 是全域 dict，
名稱撞到 clean 的指標會直接 raise，後綴讓那件事不可能發生。

import 這個套件就會註冊；clean 的執行路徑不 import 它，
而且 runner 與 render_index 都依 line 篩選，所以兩條線不會互相污染。
"""

from src.metrics_lite import unit  # noqa: F401
