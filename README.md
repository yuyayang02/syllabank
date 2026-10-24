# Syllabank

Syllabank 是一个非官方的个人用普通话音节库语音生成器，用本地音节录音生成带有动森式角色说话风格的 WAV 音频。

本项目不是 Coopanion、Microsoft 或任天堂的官方项目，也不代表与这些项目或公司存在关联、合作或背书。Python 代码根据 Coopanion 的公开语音库流程改写；正式音库的来源、版本、哈希和许可信息记录在 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 中。

当前正式流程只使用 `assets/speech/` 中的 Mandarin 音节库生成 WAV。仓库不包含候选音色或生成音频。

本项目代码使用 `AGPL-3.0-or-later`，完整许可见 [LICENSE](LICENSE)，第三方来源和音频授权说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 快速使用

```bash
./make-animalese "你好，今天过得怎么样？"
```

默认生成 `animalese.wav`。

```bash
./make-animalese "你好" --out hello.wav
./make-animalese --out reply.wav < reply.txt
```

运行时使用系统 `ffmpeg` 解码本地 OGG，不联网，也不调用 TTS。

## 仓库文件

| 路径 | 作用 |
|---|---|
| `make-animalese` | 正式 WAV 生成入口 |
| `animalese_bank.py` | 汉字解析、音节调度、OGG 解码、变调与混音 |
| `assets/speech/` | 正式音库及其索引、汉字读音表 |
| `build-bank`、`build_bank.py` | 从 Edge 音色构建候选音库 |
| `LICENSE`、`THIRD_PARTY_NOTICES.md` | 项目许可与第三方来源 |

正式音库：

```text
assets/speech/zh.ogg
assets/speech/bank.json
assets/speech/characters.json
```

## 参数

| 参数 | 默认值 | 作用 |
|---|---:|---|
| `text` | 无 | 要朗读的中文；省略时从标准输入读取 |
| `--pitch` | `365` | 基础音高，单位 Hz |
| `--speed` | `9` | 每秒最多启动的音节数 |
| `--bubble-speed` | `1` | 文字推进速度 |
| `--sample-rate` | `48000` | 输出采样率 |
| `--out` | `animalese.wav` | 输出 WAV 路径 |
| `--bank-dir` | `assets/speech` | 读取哪一组候选或正式音库 |
| `--self-test` | 无 | 运行内置检查 |

## 候选音库

以下候选只在本地测试中保留，`.gitignore` 会排除整个 `build/`，因此它们不在 GitHub 仓库中：

| 路径 | 音色 | 音节数 | 状态 |
|---|---|---:|---|
| `build/xiaoxiao/` | `zh-CN-XiaoxiaoNeural` | 407 | 完整候选 |
| `build/yunxi/` | `zh-CN-YunxiNeural` | 407 | 完整候选 |
| `build/xiaoxiao-smoke/` | Xiaoxiao | 20 | 冒烟测试 |
| `build/yunxi-smoke/` | Yunxi | 20 | 冒烟测试 |

每个完整候选目录包含：

```text
zh.ogg
bank.json
characters.json
test.wav
report.json
```

候选目录中的 `zh.ogg` 不会被正式脚本自动使用。手动指定候选：

```bash
./make-animalese "你好，今天过得怎么样？" \
  --bank-dir build/xiaoxiao \
  --out build/xiaoxiao/phrase-test.wav
```

## 构建新音库

```bash
./build-bank \
  --voice zh-CN-XiaoxiaoNeural \
  --out-dir build/xiaoxiao-new
```

首次构建需要联网安装 uv 依赖并调用 Edge TTS。构建器只允许写入 `build/`，不能直接覆盖 `assets/speech/`。

小规模验证：

```bash
./build-bank \
  --voice zh-CN-XiaoxiaoNeural \
  --limit 20 \
  --out-dir build/xiaoxiao-smoke-new
```

网络或 Edge 限流中断后，重复同一命令会复用缓存并续跑。

## 缓存

- `build/cache/`：Edge TTS 返回的原始音轨，用于断点续跑。
- `.uv-cache/`：构建依赖和 uv 运行缓存，保证文件副作用留在项目内。

这些缓存不是正式音库，也不在 GitHub 仓库中；删除后不影响 `make-animalese` 使用 `assets/speech/`。

## 来源与发布

参考项目：

- Coopanion v0.2.8
- https://github.com/Pal-AI-Lab/Coopanion
- commit `3dd0892c2d70a026253ab980af392302b88ff8b1`

本项目使用 Python 根据 Coopanion 的相关语音生成流程重写实现，不复制或发布 Coopanion 的完整项目。本项目不是 Coopanion 官方发布，也没有获得 Coopanion、Microsoft 或任天堂的背书。

正式音库：

- `zh.ogg` 和 `bank.json` 复制自上述 Coopanion 版本。
- 原音色为 Microsoft Edge `zh-CN-XiaoyiNeural`。
- 具体处理方式和哈希记录在 `THIRD_PARTY_NOTICES.md`。
- Coopanion 将仓库内容置于 AGPL-3.0-or-later，但 Edge 服务输出是否可再分发仍需遵守相关服务条款。

汉字读音表：

- 由 Coopanion 使用 pypinyin 0.55.0 生成。
- 上游读音数据为 pinyin-data，采用 MIT。
- 数据源自 Unicode Unihan，采用 Unicode License。

`.gitignore` 排除了参考源码 `Coopanion/`、本地依赖缓存、候选音库和生成的 WAV。`build/xiaoxiao/`、`build/yunxi/` 仍保留在维护者本机，但不会进入 Git 历史。

商业分发或重新分发正式 OGG 前，应再次确认 Coopanion 的音频授权和 Microsoft Edge 服务条款；如有疑问，应替换为自行录制或明确允许再分发的音源。

## 侵权处理

本项目用于个人学习和实验，无意图侵犯任何作品、商标、声音、音频或其他权利。如果任何权利人认为本项目中的代码、文字、音库或生成内容侵犯了其权利，请通过 [GitHub Issues](https://github.com/yuyayang02/syllabank/issues) 联系维护者，并说明涉及的文件和权利依据。核实后，维护者会立即删除或替换相关内容，并停止进一步分发。
