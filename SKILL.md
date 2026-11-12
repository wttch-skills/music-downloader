---
name: music-downloader
description: 搜索并下载歌曲、整理进 Apple Music。当用户说「帮我下某首歌/某个专辑」「把这首歌加到音乐 App」「要无损 FLAC/ALAC 版本」「要最高品质 MP3」「从 B 站下歌并修正歌曲信息」时使用。优先在 hifini.com.cn（音乐磁场）找音源，找不到自动兜底 B 站；用 Downie 4 下载（内置后处理直接输出 320kbps MP3），hifini 的 FLAC 转 Apple Lossless，最后写入正确的歌名/歌手/专辑/专辑作者/作曲/封面并导入「音乐」。
---

# 音乐下载归档

把「想听的一首歌」变成「Apple Music 里信息完整、带封面的本地曲目」。

## 前置检查（每次任务先跑一次）

```bash
python3 <skill>/scripts/doctor.py
```

看 `ready` 字段。自检分两级：

- **阻塞项**（`blocking: true`，必须解决，否则流程跑不通）
- **降级项**（`blocking: false`，缺了会自动走替代路径，不挡流程）

| 失败项 | 级别 | 处理 |
| --- | --- | --- |
| 浏览器 CDP | 阻塞 | `node "/Users/wttch/.workbuddy/skills/web-access/scripts/check-deps.mjs"`，并在 Chrome 打开 `chrome://inspect/#remote-debugging` 勾选 Allow remote debugging。hifini 必须登录态，所以这条路绕不开 |
| Python 依赖 | 阻塞 | `python3 -m pip install mutagen requests beautifulsoup4 lxml` |
| 脚本控制 · 音乐 | 自动修 | 系统 `osascript` 会报 -10004（WorkBuddy 是 Electron，派生的 osascript 在 TCC 里是独立主体），`_common.osascript()` 会自动改由 `DownieDestPicker.app` 转发，首次会弹一次「自动化」授权，点允许后长期有效。不需要手动去系统设置 |
| 辅助功能 · Downie 目录选择器 | 降级 | 系统设置 → 隐私与安全性 → 辅助功能 → 把 `bin/DownieDestPicker.app` 拖进列表并勾选。**不需要重启 WorkBuddy**。缺了则回退到容器软链接方案 |
| 辅助功能 · WorkBuddy | 降级 | 同样位置授权 WorkBuddy 并**重启**。缺了两件事降级：Music Converter 界面转换（改走 `afconvert`，同一个 ALAC 编码器）、网盘页面点击 |
| Downie 自动开始下载 | 自动修 | 不需要处理，`downie.py` 首次运行会自己打开这个偏好并重启 Downie |
| Downie 后处理 = 仅提取音频 | 自动修 | 不需要处理，`downie.py` 会把 `XUPostprocessing` 设为 2 并重启 Downie。设好后**下载即产出 320kbps MP3**，不必再单独转码 |
| Downie 下载前确认目录 | 自动修 | 不需要处理，`downie.py` 会关掉它并重启 Downie。开着的话每次下载都会弹保存面板，无人值守会卡死 |
| Downie 下载目录 | 自动修 | 容器 Downloads 软链到 `<工作目录>/tmp/music`，并关掉「确认每次下载的目的地」；两者都由 `downie.py dest` 幂等维护 |
| MP3 编码器 (ffmpeg/LAME) | 降级 | `brew install ffmpeg`。只在 Downie 后处理失效、需要本地补转 MP3 时用到 |

### 关于「让 Downie 把文件放到临时目录」

Downie 是**沙盒应用**，下载目录存的是「用户亲自用文件选择器选过」的安全书签（`CMDestinationSelectionFolderURL`）。
外部进程写路径、写普通书签都拿不到沙盒写权限，Downie 会静默退回 `~/Downloads`（已实测两种写法都不行）。

**设置界面上那个「下载文件保存至文件夹」改不了**，这点实测清楚了：它是个自定义 `AXMenuButton`，
一被点开，Downie 主线程就进入 `NSMenu` 模态跟踪循环，**所有辅助功能查询立刻卡死**
（`entire contents` 连续三次全超时）。菜单项文案读不到，盲按方向键又有选错位置、把文件撒到别处的风险。
所以别在这上面耗时间。

改用两条稳定机制，都实测通过：

1. **容器软链**：`~/Library/Containers/com.charliemonroe.Downie-4/Data/Downloads` →
   `<工作目录>/tmp/music`。Downie 以为自己在下到「下载」，文件实际直接落在工作目录，
   完全不需要沙盒授权。
2. **关掉确认面板**：偏好键 `XUAskForDestination`，即设置里的「确认每次下载的目的地」。
   开着的话每次下载都会弹保存面板，无人值守必卡死。脚本按需「退出 Downie → 改键 → 重新拉起」。

```bash
python3 <skill>/scripts/downie.py dest                 # 幂等：软链 + 关确认（默认目标 tmp/music）
python3 <skill>/scripts/downie.py dest --probe         # 用 UI 方式核对真实状态（只读，会弹设置窗口）
python3 <skill>/scripts/downie.py dest --restore-dest  # 容器 Downloads 还原成真实目录
```

`downie.py download` 每次下载前会跑一遍 `ensure_dest()`（已就绪时只读偏好，不抢焦点）。
不想让它动任何设置就加 `--keep-dest`。

### 辅助功能：DownieDestPicker 执行器

需要授权的场景：`dest --probe`（读设置界面）、`cleanup.py`（点「文件 > 清空已完成的下载」）。

为什么必须绕一层独立 app：WorkBuddy 是 Electron 应用，它派生的 `osascript` 在 TCC 里被记成
「osascript」而不是 WorkBuddy，所以给 WorkBuddy 授权辅助功能后脚本仍报 `-10004`；
改用 `do shell script` 转发又会报「osascript 不允许发送按键 (1002)」。独立 app 的 TCC 身份明确，
**授权一次长期有效，且不需要重启 WorkBuddy**。

首次使用一次性授权：

> 系统设置 → 隐私与安全性 → 辅助功能 → 点 `+` 或把 `DownieDestPicker.app` 拖进列表 → 勾选

脚本检测到无权限会自动把设置面板翻到该页，并在 Finder 里选中这个 app，方便直接拖。

**这个 app 只是个壳**：它 `load script` 外部 `.scpt` 然后调用 `main()`，业务逻辑全部在
`bin/picker.applescript`。改逻辑**不需要重新编译 app**——实测替换 `main.scpt` 后辅助功能授权依然有效
（TCC 认的是 app 的可执行文件，不是脚本资源）。改完 `.applescript` 后 `_common._ensure_logic_scpt()`
会自动 `osacompile` 成 `/tmp/downie_picker_logic.scpt`。

通信协议（文件，避免 `open --args` 的兼容问题）：

| 文件 | 内容 |
|---|---|
| `/tmp/downie_pick_request.json` | 第 1 行 `check` 或 `script`；`script` 模式第 2 行是 `.scpt` 路径，第 3 行起是动作与参数 |
| `/tmp/downie_pick_result.json` | `OK\|\|<细节>` / `ERROR\|\|<原因>\|<调试>` / `NEED_PERMISSION\|\|<原因>` |

可用动作：`check` `dump`（dump 全部窗口+菜单）`dumpdest`（切到下载目录页）`openmenu` `fixdest`
`clearhistory` `pick` `esc` `runscript`（执行外部 `.scpt`，多行结果原样返回）。

### 借 applet 身份控制「音乐」

`_common.osascript()` 先直接调系统 `osascript`；一旦命中 `-10004 / 权限违例`，
自动改用 `osascript_via_picker()`：把脚本 `osacompile` 成 `/tmp/wb_applescript_bridge.scpt`，
再让 applet 用 `runscript` 动作执行。发包主体变成 `DownieDestPicker.app`，TCC 身份明确，
**首次控制「音乐」会弹一次「自动化」授权，点允许即可，之后全自动**。

这条路绕开了「给 WorkBuddy 授权也没用」的死结，`music_lib.py` 因此能正常用
`--replace` 删旧条目、回读核对元数据。

> **封面：`artworks` 计数会骗人。** 一段实测经过，值得记住：
> 刚导入时 `count of artworks` 全是 0，我一度判定「Music 的 artwork 属性坏了」；
> 但**重启「音乐」之后同样的查询变成 1**。真相是两层：
>
> 1. **Music 导入时会重写文件标签，把 APIC 抹掉**（实测导入后库内文件的 APIC 变空）。
>    所以「源文件带图」不等于「库里带图」。
> 2. **Music 不会立刻索引封面**，`artworks` 在索引完成前一路返回 0；
>    `make new artwork at t` 又总是报「不能生成类 artwork」，所以靠脚本写 artwork 也走不通。
>
> 可靠流程只有一条，三步都要做：
>
> | 步骤 | 命令 |
> | --- | --- |
> | 1. 导入 | `music_lib.py import --file ... --meta ... --cover ...` |
> | 2. 给**库内文件**补 APIC | `meta.py tag --file "<媒体文件夹>/xx.mp3" --meta ... --cover ...` |
> | 3. 重启音乐强制重读 | `music_lib.py refresh --title 歌名` |
>
> `import --refresh` 会在检测到 `artworks=0` 时自动做第 3 步。**别用 `artworks=0` 断定没封面，
> 也别用「重启前读到 0」证明脚本写错** —— 先重启再下结论。

> 另一个坑：从 `music.126.net` 下封面时带 `?param=1200y1200` 拿到的可能是 **PNG 数据**，
> 直接存成 `.jpg` 会让 `meta.py` 写成 `image/png` 的 APIC，Music 更容易忽略。
> 用 `file cover.jpg` 核实，必要时 `sips -s format jpeg -Z 1400 in.jpg --out out.jpg` 转一次。

> 只有真要重编 app 时（几乎不需要）才走：`osacompile -o DownieDestPicker.app ...` →
> `plutil -replace CFBundleIdentifier` → `codesign --force --deep --sign -`。
> **重编后辅助功能要重新拖一次**。

## 音频格式约定

| 音源 | 输出 | 理由 |
| --- | --- | --- |
| B 站等有损源（Downie 下载） | **MP3 320kbps CBR** | 由 Downie 内置后处理直接产出，44.1kHz/立体声；这是 MP3 的最高品质 |
| hifini 的 FLAC / 母带（真无损） | **Apple Lossless (ALAC)** | 真无损值得保留，转 MP3 反而降级 |
| 其它有损源 | MP3 320kbps | 转 ALAC 只是换封装、白白变大 |

`downie.py` 默认 `--format mp3`：先用 Downie 自带后处理产出 MP3；万一没产出，会自动用本地 ffmpeg 补转 320kbps，返回里的 `postprocessing` 字段标明走了哪条路（`downie` / `local-ffmpeg`）。

**遇到声称 Hi-Res / 无损的源，先用 `--format original` 拿原始音轨再判断。**

```bash
python3 <skill>/scripts/downie.py download --url "<url>" --slug "<slug>" --format original
```

这个模式会**临时关掉 Downie 的「仅提取音频」后处理**（`XUPostprocessing=0`），下载完再自动恢复，
产出 `.mp4` 原始文件而不是被压成 320kbps MP3。拿到后用 `ffprobe` 核对真实音轨：

```bash
ffprobe -v error -select_streams a:0 \
  -show_entries stream=codec_name,sample_rate,channels,bit_rate -of default=nw=1 "<file>.mp4"
```

**B 站没有真正的无损。** 实测多个标「Hi-Res / 无损 / 百万级录音棚」的投稿，音轨仍是
AAC-LC 48kHz、码率 **130kbps 左右**——比默认产出的 320kbps MP3 还低一半多。
标题里的「百万级录音棚」讲的是录音与监听设备（例如真力 1234 音箱、纽曼 149 话筒），
不是数字规格。这种情况应当**如实告诉用户，别默认「Hi-Res 一定更好」**。

## 版本命名约定

同一首歌往往有多个演绎（原曲、翻唱、剧情、纯歌、伴奏、现场、翻奏…）。**只要它们进同一张专辑，
title 就必须带版本后缀**，否则「音乐」里会出现两三条一模一样的条目，用户分不清谁是谁。

格式固定为全角括号：`歌名（版本）`。

| 场景 | title / 文件名 |
| --- | --- |
| 原曲（唯一或无歧义） | `载梦行舟` |
| 翻唱 | `载梦行舟（翻唱版）` |
| 带剧情念白 / 剧情 MV 拆出的音轨 | `载梦行舟（剧情版）` |
| 剔掉剧情段的干净音轨 | `载梦行舟（纯享版）`（写「纯歌版」也会归一成这个） |
| 官方伴奏 | `载梦行舟（伴奏版）` |
| 纯享版但保留环境音效 | `万重山（纯享版·有音效）` |
| 纯享版但保留角色念白/配音 | `万重山（纯享版·有配音）` |
| 现场 | `载梦行舟（现场版）` |
| 器乐翻奏（笛/筝/琴） | `载梦行舟（翻奏版）` |

**只改 title 和文件名，不动 album 与 album_artist。** 归组靠 `album_artist + album`，
区分曲目靠 `title`，这是「音乐」自己的逻辑，改别的只会把专辑拆开。

用法（两种等价，`--version` 优先）：

```bash
# 方式一：写在 meta.json 里
#   { "title": "载梦行舟", "version": "翻唱版", ... }
python3 <skill>/scripts/meta.py tag --file "<S>/载梦行舟（翻唱版）.mp3" --meta "<S>/meta.json"

# 方式二：命令行临时指定
python3 <skill>/scripts/meta.py tag --file "<S>/x.mp3" --meta "<S>/meta.json" --version 翻唱版
python3 <skill>/scripts/music_lib.py import --file "<S>/x.mp3" --meta "<S>/meta.json" --version 翻唱版
```

脚本会自动拼成 `载梦行舟（翻唱版）`，且**幂等**：title 里已经有该后缀时不会重复叠加。
简写会归一化（`cover`→`翻唱版`、`live`→`现场版`、`剧情`→`剧情版`、`伴奏`→`伴奏版`）。

补曲目到已有专辑时，还要一起改这几项：

| 字段 | 做法 |
| --- | --- |
| `track_number` | 接在现有曲目后面递增（第 2 首就是 2） |
| `track_count` | 改成专辑实际曲目总数 |
| `album_artist` / `album` | **必须和专辑现有曲目完全一致**，否则会被拆成两张专辑 |

> **`album_artist` 要单独查，别拿 `find` 的 `artist` 顶替。** `find` 返回的 `artist` 是**曲目艺人**，
> 和 `album artist` 经常不是一个值——剑网3 这类合唱歌的典型情况是
> `artist = "剑网3 & 王大力 & NL不分 & 裂天"` 而 `album artist = "剑网3"`。
> 照 `artist` 去写 `album_artist`，音乐里立刻拆成两张专辑。先查真值：
>
> ```bash
> # 查现有曲目的 album artist / track count（find 看不到这两项）
> osascript -e 'tell application "Music" to get {name, album artist, track count} of (every track of library playlist 1 whose name contains "歌名")'
> ```
>
> 改完 `album_artist` 必须重导才生效（Music 重启会从文件标签读回），
> 库里 `set album artist of t` 只能骗过当前会话。
>
> `track count` 相反，可以在库里直接改（`set track count of t to N`），重启后一般能保持，
> 补曲目后用这个把老曲目的总数补齐，不用重导。
| 封面 | 整张专辑统一用同一个封面，别让翻唱版换自己的图，专辑视图会翻来翻去 |

去重（`--replace`）只删同名同歌手的条目：带版本后缀时按 title 精确匹配，
所以重导原曲 `载梦行舟` **不会**误删 `载梦行舟（翻唱版）`。

## 工作流

记 `S` 为本次任务的临时目录：`<工作目录>/tmp/music/<slug>/`，slug 用「歌手-歌名」的拼音或英文短标识。**所有中间文件都放在这里**，不要散落到下载目录。

### 1. 确认目标

从用户话里提取 `歌名` + `歌手`（有专辑也记下）。信息不全就先问，不要靠猜——猜错歌手后面全错。

### 2. 在 hifini 找音源

```bash
python3 <skill>/scripts/hifini.py search --query "歌名 歌手"
```

返回 `results[]`（`title` / `url` / `context`）。把前 4 条整理成「标题 + 格式/音质线索」让用户挑：

用 AskUserQuestion 提问，每个选项的 label 写帖子标题，description 写 context 里的音质/格式信息。
**必须让用户选**，不要自己替他决定——同一首歌常有 FLAC/MP3/不同版本多个帖子。

> 搜索无结果、或 CDP 不可用时，直接进入第 4 步（B 站兜底），并告知用户 hifini 没找到。

### 3. 进帖子取下载地址

```bash
python3 <skill>/scripts/hifini.py resolve --url "<用户选的帖子地址>"
```

脚本会自动处理隐藏内容：检测到「回复后查看」就用自己的登录态回复（默认文案「谢谢分享，感谢楼主！」，可用 `--text` 改），然后刷新页面提取地址。

返回字段：
- `downloads[]` — 每项含 `type`（`baidu` / `quark` / `direct` …）、`url`、`code`（提取码）
- `composer_from_post` — 正文里抓到的作曲信息，后面写标签时优先用
- `hidden` / `replied` / `reply_note` — 隐藏内容的处理情况

按 `type` 分流：

| type | 怎么做 |
| --- | --- |
| `direct` | 直接 `curl -L -o` 下载到 `S`，**跳到第 5 步** |
| 网盘（baidu/quark/aliyun…） | 走浏览器自动化，见下 |

**网盘链接**（hifini 上大多数是这个）。本地装了百度网盘客户端，所以整条链路可以自动走完：

第一步，在浏览器里打开分享页、填提取码、转存到用户自己的网盘：

```bash
python3 <skill>/scripts/pan.py open --url "<网盘链接>" --code "<提取码>" --save --slug "<slug>"
```

返回里关注 `state`（`file_list` 表示已进到文件列表）、`files` / `audio_files`（识别到的文件）、
`saved_to_pan`（是否转存成功）。

第二步，让客户端下载并监控下载目录回收文件：

```bash
python3 <skill>/scripts/pan.py wait --slug "<slug>" --timeout 1800
```

网盘客户端会在网页点击下载后自动开始，`wait` 检测到体积稳定即认为完成并挪进 `S`。

> 前置：Chrome 里已登录该网盘，且本地网盘客户端已登录同一账号。
> 若 `saved_to_pan: false`，看 `state` 字段——`need_login` 是登录态问题，`err_code` 是提取码错，
> 其它情况多半是网页结构变了，用 `pan.py open` 不带 `--save` 先看看 `files` 是否识别正常。
>
> 兜底：实在走不通时，把链接和提取码给用户，请他存到 `S` 后再继续。不要卡住空转。

### 4. B 站兜底

```bash
python3 <skill>/scripts/bilibili.py search --query "歌名 歌手"
```

结果已按贴合度排序（`score` 高者更可能是歌曲本体，带「无损/原曲/纯音频」加权，「教学/鼓谱/翻唱」降权）。同样用 AskUserQuestion 让用户选前 4 条。

选定后下载：

```bash
python3 <skill>/scripts/downie.py download --url "https://www.bilibili.com/video/BVxxxx" --slug "<slug>"
```

脚本用 Downie 的 `downie://XUOpenURL` scheme 投递（不经过 AppleScript，因此不需要 TCC 授权），
Downie 的内置后处理（仅提取音频）会在下载完成后直接产出 **MP3 320kbps**，
脚本再轮询下载目录、等文件体积稳定、清理 Downie 生成的长文件名与分片临时文件，最后移入 `S`。
返回 `file` / `ext` / `size` / `postprocessing`。

> 若用户明确要原始视频，加 `--format original`。

> ⚠️ **B 站音质有上限，务必向用户说明。**
> 标题写「无损」「Hi-Res」的多是标题党，实测音轨仍是 AAC（常见 78–192 kbps）。
> 转成 MP3 320 只是把有损音频换了个封装，**音质不会提升**（还会多一次有损转码）。
> 真正的无损只有 hifini 的 FLAC 能给。报告里要如实写清实际 codec 和码率。

> **搜索接口已改为 API 优先。** 早期版本抓 `search.bilibili.com` 的 SSR 页面，现在那页面对脚本
> 直接返回「验证码_哔哩哔哩」，解析必然是 0 条。`bilibili.py` 现在先走
> `api.bilibili.com/x/web-interface/search/type`（匿名可用），页面只作兜底；
> 返回里的 `source` 字段会标明走了哪条（`api` / `page`）。

### 4.1 多 P 视频：直接取音轨，别指望 Downie

官方 MV 常把同一首歌拆成好几个 P，例如剑网3《万重山》的 `BV1BBnb6aEww`：

| P | 内容 |
|---|---|
| P1 | 剧情版 |
| P2 | 纯享版 |
| P3 | 天策来援挽危局（另一段） |
| P4 | 纯享版（带音效） |
| P5 | 纯享版（带念白） |

**Downie 处理 `?p=N` 不可靠**：实测投递后只解析到 P1 或干脆停在解析阶段，
下载目录一直空、`.DownieTemporaryFolder` 也不增长。这种情况直接用音频直取：

```bash
# 先看有哪些 P（title / part / duration 都会返回）
python3 <skill>/scripts/bilibili.py audio --bvid BV1BBnb6aEww --p all --out-dir "<S>"
# 只要某几个 P
python3 <skill>/scripts/bilibili.py audio --bvid BV1BBnb6aEww --p 4,5 --out-dir "<S>"
```

产出 `BVxxx_P4.m4a`，匿名最高 **30280 ≈184kbps AAC / 48kHz**（B 站不给更高的匿名档）。
再转 320k MP3：

```bash
python3 <skill>/scripts/convert.py run --file "<S>/BVxxx_P4.m4a" --format mp3 --bitrate 320 \
  --out "万重山（纯享版·有音效）.mp3"
```

> 匿名拿不到 192kbps 以上的音频档（30280 就是上限），别在 `--format original` 上纠缠。
> 各 P 时长通常一样（同一个 MV 的不同混音），用 `part` 区分版本，别靠时长猜。

### 5. 确认音频产物

正常情况下 Downie 已经直接给出 MP3，跳过这一步，直接进入第 7 步查元数据。

需要核对时：

```bash
python3 <skill>/scripts/meta.py read --file "<S>/xxx.mp3"
```

`.mp3` 且 `bitrate ≈ 320000` 即为达标。若 `ext` 是 `.mp4`（后处理没生效），补一次：

```bash
python3 <skill>/scripts/convert.py run --file "<S>/xxx.mp4" --format mp3 --bitrate 320
```

### 6. FLAC 转 Apple Lossless（仅 hifini 真无损源）

只有源文件是 **FLAC** 时才做：

```bash
python3 <skill>/scripts/convert.py run --file "<S>/xxx.flac" --engine auto
```

`--engine auto` 先用 Music Converter.app 界面转换，失败自动降级到系统 `afconvert`（两者都是 CoreAudio 的同一个 ALAC 编码器，产物一致）。返回 `engine` 字段说明实际走了哪条，`codec` 应为 `alac`。

`--format auto`（默认）会按源格式自己选：无损源 → ALAC，有损源 → MP3 320。
所以不确定时直接 `convert.py run --file "<S>/xxx"` 即可。

### 7. 查元数据 + 封面

```bash
python3 <skill>/scripts/meta.py search --title "歌名" --artist "歌手"
```

返回 `candidates[]`，每项含 `title` / `artist` / `album` / `album_artist` / `year` / `track_number` / `cover_url` / `score`。

- `score` 最高且歌手对得上的那条通常就是要的版本；同名不同版本（Live/Remix）看 `album` 和 `year` 区分
- 用户指定了专辑或版本时以此为准
- **`composer` 在 iTunes 数据里经常为空**：优先用第 3 步的 `composer_from_post`，其次从 B 站视频简介/标题找「作曲：xxx」，都没有就留空并在最后报告里说明

写元数据 JSON 到 `S/meta.json`（字段见下），再下封面：

```bash
python3 <skill>/scripts/meta.py cover --url "<cover_url>" --out "<S>/cover.jpg"
```

`meta.json` 字段：`title` / `version` / `artist` / `album` / `album_artist` / `composer` / `genre` / `year` / `track_number` / `track_count` / `disc_number` / `disc_count`。

> **`version`** 是版本后缀（翻唱版 / 剧情版 / 纯歌版 / 伴奏版…），只在同一首歌有多个演绎时才填，
> 写法与注意事项见上文「版本命名约定」。**补曲目到已有专辑时必填**，否则会和原曲撞名。

> **专辑作者（album_artist）**取 iTunes 的 `albumArtist`，不是歌手名——合辑、feat. 曲目、古典唱片里两者不同，写错会让专辑在「音乐」里被拆成多张。

### 8. 写标签

```bash
python3 <skill>/scripts/meta.py tag --file "<S>/xxx.mp3" --meta "<S>/meta.json" --cover "<S>/cover.jpg"
```

支持 `.mp3`（ID3v2.3，兼容性最好）、`.m4a`（MP4 原子）、`.flac`（Vorbis comment），按扩展名自动选。

返回的 `tags` 就是写进文件的实际值，`cover` 应为 `true`；`codec` 是 `mp3` 或 `alac`，MP3 还应满足 `bitrate ≈ 320000`。这一步先落到文件里，之后 Music 导入时会直接读这些标签。

> 同一首歌的多个版本（原曲 + 翻唱 + 剧情…）**要和原曲合成一个 `track_count`**，
> 并给每首分配连号的 `track_number`。文件名也跟着 title 走，带上同样的后缀。

### 9. 导入 Apple Music

**封面必须分三步，只做第一步会没有封面**（Music 导入时会抹掉 APIC，且不立刻索引）：

```bash
# ① 导入（会在媒体文件夹按 专辑作者/专辑 建好目录）
python3 <skill>/scripts/music_lib.py import --file "<S>/xxx.mp3" --meta "<S>/meta.json" --cover "<S>/cover.jpg"

# ② 给「库内文件」补 APIC —— 注意路径是媒体文件夹，不是 <S>
python3 <skill>/scripts/meta.py tag \
  --file "$HOME/Music/Music/Media.localized/Music/<专辑作者>/<专辑>/01 歌名.mp3" \
  --meta "<S>/meta.json" --cover "<S>/cover.jpg"

# ③ 重启音乐，逼它重新索引
python3 <skill>/scripts/music_lib.py refresh --title 歌名
```

`import --refresh` 也能自动完成 ③（检测到 `artworks=0` 就重启），但 ② 必须手动做。
`refresh` 返回 `artworks_before` / `artworks_after` 对照，**重启后应为 1**。

返回 `music_tags` 是**音乐 App 里真实生效的值**，逐项对照：

- `name` / `artist` / `album` / `albumartist` / `composer` 与预期一致
- `artworks` ≥ 1（封面生效；刚导入时读到 0 属正常，重启后再看）
- `track` 是曲序

有出入就用 `music_lib.py` 重跑（加 `--replace` 会先删掉库里同名同歌手的旧条目再导入，避免重复）。

**改专辑名 / 批量换标签**不能只在库里改：Music 重启后会从文件标签读回旧值，
必须「改库内文件的标签 → 删除条目 → 用新 meta 重导 → 补 APIC → refresh」走完整轮。

### 10. 清理（必做，收尾）

**这一步不能省。** 交付完必须把临时文件和 Downie 的历史记录都清干净，不给用户留垃圾。

```bash
python3 <skill>/scripts/cleanup.py            # 清 tmp/music 下所有任务目录 + 清空 Downie 历史
python3 <skill>/scripts/cleanup.py --slug 歌名 # 只清一个任务
python3 <skill>/scripts/cleanup.py --keep-audio  # 保留音频成品，只删中间文件
python3 <skill>/scripts/cleanup.py --dry-run    # 先看会删什么
```

清的内容：

| 对象 | 说明 |
| --- | --- |
| `tmp/music/<slug>/` | 下载产物、转码中间文件、封面、meta.json |
| `tmp/music/` 根目录散落文件 | 早期落到根目录的产物 |
| `.DownieTemporaryFolder/`、`*.downiepart` | Downie 自己的临时区（有活动任务时自动跳过） |
| Downie 历史记录 | 点「文件 > 清空已完成的下载 / 清空未激活的下载」，等价于用户手点；无辅助功能权限时退回清 `XUSavedDownloads` 数组 |
| `/tmp/downie_pick_*.json` | UI 选择器的请求/结果文件 |

**例外**：如果 Apple Music 关掉了「拷贝到媒体文件夹」，库里引用的是 `tmp` 原路径——
这时必须 `--keep-audio`，否则歌会变成灰色感叹号。汇报里要说明。

### 11. 汇报

给用户一段结论，包含：歌名/歌手/专辑、**版本后缀（原曲 / 翻唱版 / 剧情版…）与它在专辑里的曲序**、音源来自哪（hifini 帖子 or B 站 BV 号 or 网易云等）、最终格式与码率、元数据里哪些字段是自动补的、哪个字段没查到（例如作曲为空）、文件与 Apple Music 里的状态、**已清理掉了哪些临时文件与 Downie 历史**。

如果这次是往已有专辑里补曲目，还要说明专辑现在一共几首、曲序怎么排的。

## 目录与文件约定

```
<工作目录>/tmp/music/<slug>/
├── *.mp3 / *.m4a        # 最终音频（B 站源为 320kbps MP3，hifini FLAC 源为 ALAC）
├── source.flac / *.mp4  # 原始下载（如有）
├── cover.jpg            # 专辑封面
└── meta.json            # 元数据
```

**文件归宿**：音乐 App 默认开启「添加到资料库时拷贝文件到媒体文件夹」，所以导入成功后成片已经
复制到 `~/Music/Music/Media.localized/...`，`tmp/music/<slug>/` 里的内容可以放心删。
确认方式：`music_lib.py` 返回 `ok: true`，或直接看媒体文件夹里出现了对应文件。

流程走完 `cleanup.py` 之后，`tmp/music/` 应该是空的，Downie 主窗口应该显示「没有下载」。

若用户关掉了这个拷贝选项，音乐库会引用 `tmp` 里的原路径——那就要保留音频文件，
不要清理，并在汇报时说明。

**避免重复**：Music 不会自动去重，同名同歌手导入两次会产生两条。
正式导入前先查：

```bash
python3 <skill>/scripts/music_lib.py find --title "歌名"
```

有匹配就加 `--replace` 重导（会先删旧条目）。注意 `--replace` 依赖脚本权限，降级模式下用不了，
需要提醒用户手动删掉多余条目。

## 故障对照

| 现象 | 原因与处理 |
| --- | --- |
| `hifini.py` 报「没有拿到结果」 | 未登录 / 触发风控 / 搜索入口变更。先在 Chrome 里手动打开 hifini 确认能搜，再重试；仍不行走 B 站 |
| `bilibili.py` 报「解析到 0 个视频」 | 旧版本抓网页，`search.bilibili.com` 现在对脚本返回验证码页。已改为 API 优先（看返回的 `source`）。若 `source: api` 仍失败就是 API 限流，等几秒重试或直接给 BV 号 |
| Downie 投递多 P 视频后目录一直空 | `?p=N` 解析不可靠，只认 P1 或卡在解析。改用 `bilibili.py audio --bvid ... --p N --out-dir "<S>"` 直取音轨，再 `convert.py` 转 320k MP3 |
| `hifini.py` 报「需要登录才能搜索」 | Chrome 里 hifini 登录态失效，重新登录后重试 |
| `resolve` 返回 `hidden: true` 且 `replied: false` | 回复失败或无权限，看 `reply_note`；可能需要用户手动回复一次 |
| `pan.py` 返回 `state: need_login` | Chrome 里没登录该网盘 |
| `pan.py` 识别不到文件 | 网页结构变化，先不带 `--save` 跑一次看 `text` 片段，再调整 `LIST_FILES_JS` |
| `downie.py` 超时没新文件 | Downie 卡在解析或该站点需要登录。打开 Downie 界面看队列；确认「自动开始下载」偏好已打开 |
| `downie.py` 返回 `ext: .mp4` | Downie 的后处理没生效。`defaults write com.charliemonroe.Downie-4 XUPostprocessing -int 2` 后重启 Downie；脚本已有本地 ffmpeg 补转兜底 |
| 后处理产出的是 `.m4a` 而不是 `.mp3` | Downie 的「音频格式偏好」被改过或「强制输出 MP3」被关。在 Downie 偏好设置的「后处理」页里勾上 Enforce MP3 / 强制输出 MP3，或让 `downie.py` 的本地补转兜底 |
| MP3 码率远低于 320kbps | 源音频本身码率就低（B 站常见 78–192kbps AAC），转码不会凭空提升。如实告知用户 |
| 标题写「Hi-Res / 无损」但听着没更好 | 多半是标题党。用 `--format original` 下原始音轨 + `ffprobe` 核对，实测常见 AAC 48kHz **130kbps**，反而低于默认的 320kbps MP3。先核对再决定要不要用 |
| `-10004 权限违例` | 只影响「音乐」脚本控制与 Music Converter 界面转换，均为降级项；要恢复就授权后重启程序 |
| `convert.py` 报产物不是 ALAC | Music Converter 的输出格式被设成了 AAC/MP3。改用 `--engine afconvert` |
| 封面不显示 / `artworks=0` | 三步是否都做了：导入 → 给**库内文件**补 APIC → `refresh` 重启。重启前读到 0 是正常的，不代表没封面 |
| 改了专辑名，重启后又变回旧的 | 只改了库没改文件。Music 重启会从文件标签读回，必须改库内文件标签后重导 |
| `make new artwork` 报「不能生成类 artwork」 | 这个版本不支持脚本写 artwork，忽略即可；封面靠文件 APIC + 重启索引 |
| 专辑在音乐里被拆成多张 | `album_artist` 不一致。注意 `find` 返回的是**曲目艺人**，不是 `album artist`（合唱曲两者不同），别照着它写。先 osascript 查真值，改文件标签后重导（见「版本命名约定」下方说明） |
| 专辑里出现两条一模一样的同名曲目 | 补曲目时没加版本后缀。按「版本命名约定」补 `version` 重导；`--replace` 只删同名同歌手，不会误伤别的版本 |
| 翻唱版被当成重复条目删掉 | 重导原曲时 `--replace` 用了模糊匹配。现在带版本后缀的条目按 title 精确匹配，已修 |
| 想改 Downie 的「下载文件保存至文件夹」 | 那个控件是自定义 NSMenu，点开就让 Downie 主线程进模态跟踪、辅助功能全卡死（实测）。别硬试，走容器软链 + 关「确认每次下载的目的地」，见上文 |
| 歌名变成灰色感叹号 | 音乐库引用的是已删的临时文件。检查 Music 是否开了「拷贝到媒体文件夹」，没开就重导并 `--keep-audio` |

## 脚本清单

| 脚本 | 作用 |
| --- | --- |
| `doctor.py` | 环境与权限自检（区分阻塞 / 可降级） |
| `hifini.py` | hifini 搜索 / 进帖子 / 自动回复 / 提取下载地址 |
| `bilibili.py` | B 站搜索（API 优先、页面兜底）+ 按 P 号直取音轨（`audio` 子命令） |
| `pan.py` | 网盘分享页浏览器自动化（填码、转存、监控下载） |
| `downie.py` | 投递 Downie（URL scheme）、配置后处理、等待完成、回收文件、必要时本地补转 MP3 |
| `convert.py` | 格式转换：FLAC→ALAC（Music Converter 界面 / afconvert）、任意格式→MP3 320（ffmpeg/LAME） |
| `meta.py` | iTunes 元数据查询、封面下载、标签读写（`--version` 拼版本后缀） |
| `music_lib.py` | 导入 Apple Music、设置元数据与封面、读回核对（`--version`、`--refresh`）、`refresh` 重启强制重读封面 |
| `cleanup.py` | 收尾清理：临时目录、Downie 临时区、Downie 历史记录 |
| `ui_probe.py` | dump 任意应用的 UI 树，用于校准 UI 自动化 |
| `bin/DownieDestPicker.app` | 独立小 app，以自身 TCC 身份点选 Downie 下载目录（源码见同目录 `.applescript`） |

所有脚本都在 stdout 输出单行 JSON，人可读进度走 stderr。
