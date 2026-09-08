# 礼物库物品清单（初版）

> 状态：草稿
> 创建日期：2026-08-03
> 来源：[[7-AI学习/11-AI桌宠情侣互联/spec/gift-exchange/spec.md|gift-exchange spec]] §7 开放问题决议
> 说明：初始礼物库 17 款 + 自定义彩蛋类型。id 与 `assets/manifest.json`（脚本生成）一致；美术资源当前为程序生成的 SVG 占位图（见 `tools/gen_placeholders.py`），替换真实素材只覆盖文件、不改代码。

## 字段说明

| 字段 | 说明 |
|------|------|
| id | 全局唯一，命名 `{type}-{序号}` |
| name | 显示名称 |
| type | outfit 装扮 / action 动作 / emoji 表情 / custom_egg 自定义彩蛋 |
| rarity | common 常见 / event 活动 / anniversary 周年专属 |
| expire_days | 有效期天数；0 表示永久（不失效） |

## 清单

| id | 名称 | 类型 | 稀有度 | 有效期 | 说明 |
|----|------|------|:------:|:------:|------|
| outfit-heart-100 | 爱心气泡装扮 | outfit | event | 30 | 纪念日/互赠热门 |
| outfit-paw-scarf | 爪印围巾 | outfit | common | 30 | 日常穿搭 |
| outfit-crown | 迷你皇冠 | outfit | event | 7 | 庆祝场合限时 |
| outfit-glasses | 圆框眼镜 | outfit | common | 30 | 书呆子风 |
| outfit-anniv-set | 周年纪念套装 | outfit | anniversary | 7 | 周年日专属 |
| outfit-cosy-hood | 连帽睡袍 | outfit | common | 30 | 慵懒居家 |
| outfit-halo | 小光环 | outfit | event | 7 | 天使风 |
| action-hug | 贴贴抱抱 | action | common | 0 | 亲密度表现 |
| action-tail-wag | 摇尾巴 | action | event | 30 | 开心表现 |
| action-fly-kiss | 飞吻 | action | event | 7 | 撒娇 |
| action-sleepy-yawn | 打哈欠 | action | common | 0 | 困倦表现 |
| action-paw-tap | 爪爪拍桌 | action | common | 0 | 求关注 |
| emoji-heart-bubble | 爱心气泡 | emoji | common | 0 | 弹窗/聊天 |
| emoji-blush | 脸红 | emoji | event | 7 | 收到礼物时 |
| emoji-cat-dizzy | 晕晕猫 | emoji | common | 30 | 搞笑表情 |
| emoji-stomp | 生气跺脚 | emoji | common | 0 | 傲娇 |
| emoji-heart-eyes | 星星眼 | emoji | event | 7 | 花痴 |

## 自定义彩蛋

| id | 类型 | 说明 |
|----|------|------|
| custom-egg | custom_egg | 用户自定义纯文本 ≤200 字；每端上限 20 个，超出替换旧的；随 gift.send 端到端加密 |

## 备注

- 清单可扩展：新增物品在 `gift_catalog.md` 与 `tools/gen_placeholders.py` 各加一行，重跑脚本生成占位图。
- 礼物接受积分统一规则（普通 +5 / 纪念日特惠 +20），不按单品区分，见 [[7-AI学习/11-AI桌宠情侣互联/spec/pet-growth/spec.md|pet-growth spec 积分来源表]]。
