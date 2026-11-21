# -*- coding: utf-8 -*-
"""零写入取证快照（测试工程师交付，纯标准库，对生产库 mode=ro 只读）。

用法：
  python 取证_零写入快照.py <输出.json>            # 拍快照
  python 取证_零写入快照.py 对比 <前.json> <后.json>  # 对比并给出结论

证据三层：
  1) 库文件（含 -wal/-shm）大小 + SHA256；
  2) 关键表行数；
  3) 关键表全量内容哈希（SELECT * ORDER BY rowid，最强证据，覆盖窗口内外）。
口径说明：DSA 调度 18:00/18:05/19:00 会写库，快照必须紧挨着被测命令拍；
若文件哈希不一致但表内容哈希一致，说明是 SQLite 页级/WAL 变动而非业务数据写入，需单独注明。
"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

仓库根 = Path(__file__).resolve().parents[3]
DB = 仓库根 / "data" / "stock_analysis.db"
关键表 = ["portfolio_trades", "decision_signals", "paper_account_runs",
          "portfolio_positions", "stock_daily", "portfolio_accounts",
          "paper_strategy_profiles"]


def 文件指纹(p: Path):
    if not p.exists():
        return {"存在": False}
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"存在": True, "大小": p.stat().st_size, "sha256": h.hexdigest()}


def 拍快照():
    uri = DB.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        行数, 内容哈希 = {}, {}
        for t in 关键表:
            cur = conn.execute(f"SELECT * FROM {t} ORDER BY rowid")
            h = hashlib.sha256()
            n = 0
            for row in cur:
                h.update(repr(row).encode("utf-8"))
                n += 1
            行数[t] = n
            内容哈希[t] = h.hexdigest()
        # 窗口内成交单独留证（截至 2026-11-13）
        窗口 = conn.execute(
            "SELECT COUNT(*) FROM portfolio_trades WHERE substr(trade_date,1,10)<='2026-11-13'"
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "库文件": {p.name: 文件指纹(p) for p in [DB, Path(str(DB) + "-wal"), Path(str(DB) + "-shm")]},
        "表行数": 行数, "表内容哈希": 内容哈希, "窗口内成交笔数": 窗口,
    }


def 对比(前路径, 后路径):
    前 = json.loads(Path(前路径).read_text(encoding="utf-8"))
    后 = json.loads(Path(后路径).read_text(encoding="utf-8"))
    结果 = {"文件一致": 前["库文件"] == 后["库文件"],
            "行数一致": 前["表行数"] == 后["表行数"],
            "内容一致": 前["表内容哈希"] == 后["表内容哈希"],
            "窗口一致": 前["窗口内成交笔数"] == 后["窗口内成交笔数"]}
    差异 = []
    for k in 关键表:
        if 前["表行数"].get(k) != 后["表行数"].get(k):
            差异.append(f"{k} 行数 {前['表行数'].get(k)} -> {后['表行数'].get(k)}")
        if 前["表内容哈希"].get(k) != 后["表内容哈希"].get(k):
            差异.append(f"{k} 内容哈希变化")
    for name, a in 前["库文件"].items():
        b = 后["库文件"].get(name, {})
        if a != b:
            差异.append(f"文件 {name}: {a} -> {b}")
    结果["差异明细"] = 差异
    结果["结论"] = ("零写入 ✅（文件+行数+全表内容三层一致）" if all(结果[k] for k in ("文件一致", "行数一致", "内容一致", "窗口一致"))
                    else ("业务数据零写入 ✅（文件层有差异，见明细，需核对是否调度写入）" if 结果["内容一致"] and 结果["行数一致"]
                          else "检测到数据变化 ❌"))
    print(json.dumps(结果, ensure_ascii=False, indent=2))
    return 0 if 结果["内容一致"] and 结果["行数一致"] else 1


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "对比":
        sys.exit(对比(sys.argv[2], sys.argv[3]))
    out = Path(sys.argv[1])
    s = 拍快照()
    out.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"表行数": s["表行数"], "窗口内成交笔数": s["窗口内成交笔数"],
                      "db_sha256": s["库文件"][DB.name].get("sha256")}, ensure_ascii=False, indent=2))
    print(f"[已写入] {out}")