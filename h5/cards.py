"""
卡密管理模块（SQLite 存储）

卡密模型：
  - code:        卡密字符串，格式 XXXX-XXXX-XXXX（去歧义字符，大小写不敏感）
  - quota:       总可用次数
  - used:        已用次数
  - expires_at:  过期时间（unix 秒）；None 表示不过期
  - note:        备注（可选）
  - created_at:  创建时间
  - disabled:    是否停用（1=停用）

业务逻辑（与用户拍板一致）：
  - 额度池：卡密 = 若干次建会额度，后端从 MEETING_ACCOUNTS 多账号里自动挑空闲账号建会
  - 限次 + 有效期：quota 控制次数，expires_at 控制有效期
"""

import os
import time
import random
import sqlite3

DB_PATH = os.getenv("CARDS_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cards.db"))

# 去掉容易看错的字符：0/O、1/I/L
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cards (
            code        TEXT PRIMARY KEY,
            quota       INTEGER NOT NULL,
            used        INTEGER NOT NULL DEFAULT 0,
            expires_at  INTEGER,
            note        TEXT,
            created_at  INTEGER NOT NULL,
            disabled    INTEGER NOT NULL DEFAULT 0,
            max_duration_min INTEGER NOT NULL DEFAULT 180
        )"""
    )
    # 兼容老库：缺列则补（按次卡默认单次最长 3 小时）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cards)").fetchall()]
    if "max_duration_min" not in cols:
        conn.execute("ALTER TABLE cards ADD COLUMN max_duration_min INTEGER NOT NULL DEFAULT 180")
    conn.commit()
    conn.close()


def gen_code(length=12):
    """生成 XXXX-XXXX-XXXX 格式卡密（去歧义字符）。"""
    raw = "".join(random.choice(_ALPHABET) for _ in range(length))
    return "-".join(raw[i:i + 4] for i in range(0, length, 4))


def create_cards(count, quota, days_valid=None, note="", max_duration_min=180):
    """批量生成卡密。max_duration_min=按次卡单次最长分钟数（默认 180=3 小时）。返回生成的卡密列表。"""
    init_db()
    conn = _conn()
    created = []
    for _ in range(int(count)):
        while True:
            code = gen_code()
            if not conn.execute("SELECT 1 FROM cards WHERE code=?", (code,)).fetchone():
                break
        expires = None
        if days_valid:
            expires = int(time.time()) + int(days_valid) * 86400
        conn.execute(
            "INSERT INTO cards(code, quota, used, expires_at, note, created_at, disabled, max_duration_min) "
            "VALUES(?,?,?,?,?,?,0,?)",
            (code, int(quota), 0, expires, note, int(time.time()), int(max_duration_min)),
        )
        created.append(code)
    conn.commit()
    conn.close()
    return created


def _norm(code):
    return (code or "").strip().upper()


def exists(code):
    init_db()
    conn = _conn()
    row = conn.execute("SELECT 1 FROM cards WHERE code=?", (_norm(code),)).fetchone()
    conn.close()
    return row is not None


def verify(code):
    """校验卡密是否可用于「创建会议」。

    返回 dict：
      ok=False 时带 reason: not_found / disabled / expired / no_quota
      ok=True  时带 remaining / quota / expires_at
    """
    init_db()
    conn = _conn()
    row = conn.execute("SELECT * FROM cards WHERE code=?", (_norm(code),)).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "reason": "not_found"}
    if row["disabled"]:
        return {"ok": False, "reason": "disabled"}
    if row["expires_at"] and row["expires_at"] < int(time.time()):
        return {"ok": False, "reason": "expired"}
    remaining = row["quota"] - row["used"]
    if remaining <= 0:
        return {"ok": False, "reason": "no_quota"}
    return {
        "ok": True,
        "remaining": remaining,
        "quota": row["quota"],
        "expires_at": row["expires_at"],
        "max_duration_min": row["max_duration_min"],
    }


def consume(code):
    """创建成功后扣减一次额度。"""
    conn = _conn()
    conn.execute("UPDATE cards SET used=used+1 WHERE code=?", (_norm(code),))
    conn.commit()
    conn.close()


def list_cards():
    init_db()
    conn = _conn()
    rows = conn.execute("SELECT * FROM cards ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    # 简易 CLI：python cards.py <数量> <额度> [有效期天数]
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    q = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    d = int(sys.argv[3]) if len(sys.argv) > 3 else None
    for c in create_cards(n, q, d):
        print(c)
