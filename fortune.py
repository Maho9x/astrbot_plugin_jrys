"""运势抽取逻辑，完全对齐 koishi-plugin-jrys-prpr 的 utils/jrys.ts。

种子 = md5(用户ID + 中国时区当天0点毫秒 + YYYY-MM-DD) 前 8 位十六进制，
再用该种子做一次 weightedPick 选运势档位；文案下标由时间戳与用户ID推导。
因此同一用户在同一天（中国时区）看到的运势与 koishi 完全一致。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

CHINA_TZ = timezone(timedelta(hours=8))
MOD = 1000000001

# koishi 默认的运势概率表：档位值 -> 权重
DEFAULT_WEIGHTS: dict[int, int] = {
    0: 5,
    14: 10,
    28: 12,
    42: 15,
    56: 30,
    70: 35,
    84: 45,
    98: 25,
}

# 档位 -> 背景图文件名（不含扩展名），对齐 koishi utils/background.ts
LUCK_TO_IMAGE: dict[int, str] = {
    98: "大吉",
    84: "中吉",
    70: "吉",
    56: "吉带凶",
    42: "吉凶参半",
    28: "凶带吉",
    14: "小凶",
    0: "凶",
}

# 星级文案（仅用于展示，koishi 的星数来自 jrys.json 的 luckyStar 字段）
LUCK_LABEL: dict[int, str] = {
    98: "★★★★★★★",
    84: "★★★★★★☆",
    70: "★★★★★☆☆",
    56: "★★★★☆☆☆",
    42: "★★★☆☆☆☆",
    28: "★★☆☆☆☆☆",
    14: "★☆☆☆☆☆☆",
    0: "☆☆☆☆☆☆☆",
}


def china_now() -> datetime:
    return datetime.now(CHINA_TZ)


def china_midnight_ms(moment: datetime | None = None) -> int:
    moment = moment or china_now()
    d = moment.astimezone(CHINA_TZ)
    return int(datetime(d.year, d.month, d.day, tzinfo=CHINA_TZ).timestamp() * 1000)


def china_date_string(moment: datetime | None = None) -> str:
    d = (moment or china_now()).astimezone(CHINA_TZ)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def china_formatted_date(moment: datetime | None = None) -> str:
    d = (moment or china_now()).astimezone(CHINA_TZ)
    return f"{d.year:04d}/{d.month:02d}/{d.day:02d}"


def _resolve_user_id(user_id: str, moment: datetime) -> int:
    etime = china_midnight_ms(moment)
    raw = str(user_id or "")
    if raw.isdigit():
        return int(raw)
    digest = hashlib.sha256((raw + str(etime)).encode("utf-8")).hexdigest()
    return int(digest, 16) % MOD


def weighted_pick(weights: dict[int, int], pointer_fraction: float) -> int:
    """等价 inaba Random.weightedPick：按 key 升序遍历（JS 对象整型键的遍历顺序）。"""
    total = sum(weights.values())
    pointer = pointer_fraction * total
    counter = 0
    for key in sorted(weights):
        counter += weights[key]
        if pointer < counter:
            return key
    return sorted(weights)[-1]


def normalize_weights(raw: dict | None) -> dict[int, int]:
    weights: dict[int, int] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                luck = int(k)
                weight = int(v)
            except (TypeError, ValueError):
                continue
            if weight > 0:
                weights[luck] = weight
    return weights or dict(DEFAULT_WEIGHTS)


def draw_luck(weights: dict[int, int], user_id: str, moment: datetime | None = None) -> int:
    moment = moment or china_now()
    etime = china_midnight_ms(moment)
    uid = _resolve_user_id(user_id, moment)
    seed_input = f"{uid}{etime}{china_date_string(moment)}"
    seed = int(hashlib.md5(seed_input.encode("utf-8")).hexdigest()[:8], 16)
    return weighted_pick(weights, seed / 0xFFFFFFFF)


def pick_entry(jrys_data: dict, luck: int, user_id: str, moment: datetime | None = None) -> dict:
    moment = moment or china_now()
    etime = china_midnight_ms(moment)
    uid = _resolve_user_id(user_id, moment)
    bucket = jrys_data.get(str(luck))
    if not bucket:
        raise KeyError(f"jrys 数据中缺少档位 {luck}")
    index = int(((etime / 100000) * uid % 1000001) * 2333 % len(bucket))
    entry = dict(bucket[index])
    entry["luckValue"] = luck
    return entry


def load_jrys_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
