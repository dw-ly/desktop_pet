# Desktop Pet · 情侣互联

情侣双端桌宠：两人各养一只团子，本地推理，通过端到端加密通道同步带话、情绪、养成、纪念日等轻量事件。定位是关系连接器，不是 AI 恋人。聊天、语音和模型文件不上传。

仓库对应本地项目「AI桌宠情侣互联」。

## 目录

```text
code/          源码、测试、联调 demo、宠物素材
  src/core/    带话 / 情绪 / 养成 / 纪念日 / 一致性
  src/sync/    配对、加密、传输、队列
  src/ui/      面板与设置
  tests/       pytest
  tools/       双端联调脚本
spec/          需求、实施计划、实现规格
assets/        礼物等美术规划
环境准备.md     完整开发环境与模型下载说明
```

## 环境部署

Windows 优先，Python 3.11（64 位）。详细步骤见 [环境准备.md](环境准备.md)。

```powershell
git clone https://github.com/dw-ly/desktop_pet.git
cd desktop_pet

python -m venv code\.venv
code\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

可选：接入本地 LLM / TTS / ASR / UI 外壳时再装：

```powershell
pip install -r requirements-ai.txt
```

模型文件放到 `models/`（该目录不入库，需按 `环境准备.md` 自行下载）。

## 运行测试

```powershell
cd code
.\.venv\Scripts\python.exe -m pytest -q
```

当前同步层、带话、情绪、养成、纪念日与数据一致性模块均有单测覆盖。

## 联调 Demo

在已激活的 venv 中：

```powershell
cd code
python tools\sync_demo.py
python tools\carry_demo.py
python tools\mood_demo.py
python tools\pet_growth_demo.py
python tools\anniv_demo.py
python tools\consistency_demo.py
```

局域网双端直连即可，中继服务按规划放到后续里程碑。
