"""批量生成礼物占位图标（SVG）与 manifest.json。

用法:
    python tools/gen_placeholders.py

输出:
    assets/gifts/<gift_id>.svg   64x64 矢量占位图
    assets/manifest.json         礼物库机读清单

约定:
    - 礼物清单与本脚本内 GIFTS 保持同步（新增物品两处各加一行）
    - 稀有度决定底色: common 灰蓝 / event 金 / anniversary 粉
    - 类型决定中央形状: outfit 爱心 / action 圆环 / emoji 五角星 / custom_egg 蛋形
    - 替换真实素材: 直接覆盖 assets/gifts/ 下对应文件，程序按 manifest 加载，不改代码
"""
from __future__ import annotations

import json
import pathlib

BASE = pathlib.Path(__file__).resolve().parent.parent
OUT = BASE / "assets" / "gifts"
MANIFEST = BASE / "assets" / "manifest.json"

# (id, name, type, rarity, expire_days)  —— expire_days 0 表示永久
GIFTS = [
    ("outfit-heart-100", "爱心气泡装扮", "outfit", "event", 30),
    ("outfit-paw-scarf", "爪印围巾", "outfit", "common", 30),
    ("outfit-crown", "迷你皇冠", "outfit", "event", 7),
    ("outfit-glasses", "圆框眼镜", "outfit", "common", 30),
    ("outfit-anniv-set", "周年纪念套装", "outfit", "anniversary", 7),
    ("outfit-cosy-hood", "连帽睡袍", "outfit", "common", 30),
    ("outfit-halo", "小光环", "outfit", "event", 7),
    ("action-hug", "贴贴抱抱", "action", "common", 0),
    ("action-tail-wag", "摇尾巴", "action", "event", 30),
    ("action-fly-kiss", "飞吻", "action", "event", 7),
    ("action-sleepy-yawn", "打哈欠", "action", "common", 0),
    ("action-paw-tap", "爪爪拍桌", "action", "common", 0),
    ("emoji-heart-bubble", "爱心气泡", "emoji", "common", 0),
    ("emoji-blush", "脸红", "emoji", "event", 7),
    ("emoji-cat-dizzy", "晕晕猫", "emoji", "common", 30),
    ("emoji-stomp", "生气跺脚", "emoji", "common", 0),
    ("emoji-heart-eyes", "星星眼", "emoji", "event", 7),
]

RARITY_COLORS = {
    "common": "#aab8c8",
    "event": "#f5c97b",
    "anniversary": "#ff8fab",
}

SHAPES = {
    "outfit": '<path d="M32 46 C20 35 12 28 16 21 C18 16 25 15 29 19 C31 21 31 22 32 23 C33 22 33 21 35 19 C39 15 46 16 48 21 C52 28 44 35 32 46 Z" fill="#ffffff" opacity="0.92"/>',
    "action": '<circle cx="32" cy="32" r="17" fill="none" stroke="#ffffff" stroke-width="4" opacity="0.95"/><circle cx="32" cy="32" r="6" fill="#ffffff" opacity="0.95"/>',
    "emoji": '<polygon points="32,14 36.4,25.9 49.1,26.4 39.1,34.3 42.6,46.6 32,39.5 21.4,46.6 24.9,34.3 14.9,26.4 27.6,25.9" fill="#ffffff" opacity="0.92"/>',
    "custom_egg": '<ellipse cx="32" cy="33" rx="14" ry="19" fill="#ffffff" opacity="0.92"/><ellipse cx="27" cy="26" rx="4" ry="6" fill="#000000" opacity="0.08"/>',
}


def svg_for(gid: str, gtype: str, rarity: str) -> str:
    bg = RARITY_COLORS[rarity]
    shape = SHAPES[gtype]
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="{bg}"/>
  {shape}
</svg>
'''


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    entries = []
    for gid, name, gtype, rarity, expire_days in GIFTS:
        (OUT / f"{gid}.svg").write_text(svg_for(gid, gtype, rarity), encoding="utf-8")
        entries.append({
            "id": gid,
            "name": name,
            "type": gtype,
            "rarity": rarity,
            "expire_days": expire_days,
            "asset": f"gifts/{gid}.svg",
        })
    manifest = {"version": 1, "gifts": entries}
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"生成 {len(entries)} 个占位图 -> {OUT}")
    print(f"生成 manifest.json -> {MANIFEST}")


if __name__ == "__main__":
    main()
