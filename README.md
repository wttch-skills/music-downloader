# music-downloader

一个跑在 macOS 上的 **WorkBuddy skill**：把「想听的一首歌」变成「Apple Music 里信息完整、带封面的本地曲目」。

搜索音源 → 下载 → 转码 → 补元数据与封面 → 导入「音乐」→ 收尾清理，整条链路脚本化，Agent 可以无人值守跑完。

```
hifini 搜索 ──┐
              ├─→ Downie 4 下载 / B 站音轨直取 ─→ 转码 ─→ 写标签 ─→ 导入 Music ─→ 清理
B 站兜底 ─────┘
```

## 特性

- **双音源**：优先 [hifini 音乐磁场](https://hifini.com.cn)（真无损 FLAC、母带），搜不到或 CDP 不可用时自动兜底 B 站。
- **无人值守**：Downie 的「自动开始下载」「后处理=仅提取音频」「不确认目的地」全由脚本幂等维护，不弹面板、不卡住。
- **格式按源分级**：有损源 → **MP3 320kbps**；hifini 的真无损 → **Apple Lossless (ALAC)**，不做无意义的二次有损转码。
- **元数据完整**：歌名 / 歌手 / 专辑 / **专辑作者** / 作曲 / 曲序 / 封面，全部写进文件标签再导入，避免 Music 重启后回滚。
- **多版本共存**：`（翻唱版）` `（剧情版）` `（纯享版）` `（伴奏版）` `（纯享版·有音效）` 等后缀规范化，同一专辑里多版本不混淆、不被误删。
- **收尾必清**：临时目录、Downie 临时区、Downie 历史记录全清干净，不留垃圾。
- **所有脚本 stdout 只输出一行 JSON**，人可读进度走 stderr，便于 Agent 解析。

## 环境要求

| 依赖 | 说明 |
| --- | --- |
| macOS | 依赖 `Music.app`、`osascript`、`afconvert`，无跨平台计划 |
| Python 3.8+ | `mutagen` `requests` `beautifulsoup4` `lxml`（实测环境 3.13） |
| [Downie 4](https://software.charliemonroe.net/downie/) | 付费软件，B 站等站点的主力下载器 |
| Google Chrome + 远程调试 | hifini 需要登录态，必须走 CDP |
| ffmpeg | 可选，本地补转 MP3 / 处理封面时用 |

```bash
python3 -m pip install mutagen requests beautifulsoup4 lxml
brew install ffmpeg            # 可选
```

## 安装

skill 目录即仓库根目录，克隆到工作区的 skills 下即可：

```bash
git clone git@github.com:wttch-skills/music-downloader.git \
  "<工作区>/.workbuddy/skills/music-downloader"
```

装完先跑自检：

```bash
python3 <skill>/scripts/doctor.py
```

`ready: true` 表示无阻塞项。自检结果里每一项都标了 `blocking`，阻塞项必须解决，降级项缺了会自动走替代路径。

## 快速开始

```bash
S="<skill>/scripts"

# 1. 搜索（把候选整理给用户挑，别自己决定）
python3 $S/hifini.py search --query "载梦行舟 剑网3"
python3 $S/hifini.py resolve --url "<帖子地址>"     # 自动回复取隐藏内容、提取网盘/直链

# 2. 下载
python3 $S/downie.py dest --probe                   # 核对下载目录（可选，只读）
python3 $S/downie.py download --url "<链接>" --slug "zaimengxingzhou"
python3 $S/bilibili.py audio --bvid BV1BBnb6aEww --p 4 --out-dir "$S"   # 多 P 视频直取音轨

# 3. 转码
python3 $S/convert.py run --file source.flac --format alac

# 4. 元数据与封面
python3 $S/meta.py search --title "载梦行舟" --artist "剑网3"
python3 $S/meta.py tag --file "载梦行舟.mp3" --meta meta.json --cover cover.jpg --version 翻唱版

# 5. 导入并核对
python3 $S/music_lib.py find --title "载梦行舟"
python3 $S/music_lib.py import --file "..." --meta meta.json --cover cover.jpg --replace
python3 $S/music_lib.py refresh --title "载梦行舟"   # 强制重读封面

# 6. 收尾（必做）——--history 是「只清历史」，所以分两条
python3 $S/cleanup.py --slug "zaimengxingzhou"     # 任务临时目录
python3 $S/cleanup.py --history                    # 清空 Downie 历史
```

完整流程、每步的判定标准与故障对照见 **[SKILL.md](SKILL.md)**，那是 Agent 的执行规范，也是本仓库的主文档。

## 目录结构

```
.
├── SKILL.md                    # 执行规范（流程 / 约定 / 故障对照）
├── scripts/
│   ├── doctor.py               # 环境与权限自检（区分阻塞 / 可降级）
│   ├── hifini.py               # 搜索 / 进帖 / 自动回复 / 提取下载地址
│   ├── bilibili.py             # 搜索（API 优先）+ 按 P 号直取音轨
│   ├── pan.py                  # 网盘分享页自动化（填码、转存、监控下载）
│   ├── downie.py               # 投递 Downie、配置后处理、等待完成、回收文件
│   ├── convert.py              # FLAC→ALAC / 任意→MP3 320
│   ├── meta.py                 # iTunes 元数据查询、封面下载、标签读写
│   ├── music_lib.py            # 导入 Music、读写元数据、重启刷新封面
│   ├── cleanup.py              # 临时文件与 Downie 历史清理
│   ├── cdp.py                  # Chrome DevTools Protocol 客户端
│   ├── ui_probe.py             # dump 任意应用 UI 树，校准 UI 自动化
│   └── _common.py              # 路径 / JSON / 子进程 / TCC 转发等公共能力
├── bin/
│   ├── DownieDestPicker.app    # 独立小 app（TCC 身份执行器）
│   ├── DownieDestPicker.applescript   # 执行器壳
│   └── picker.applescript      # 业务逻辑（改这里，不用重编 app）
└── references/                 # hifini 站点结构与依赖应用说明
```

## 权限授权（首次一次性）

脚本会自己检测并引导，这里先说明白，免得到处找设置：

| 权限 | 用途 | 怎么给 |
| --- | --- | --- |
| Chrome 远程调试 | hifini 搜索 / 网盘转存 | Chrome 打开 `chrome://inspect/#remote-debugging`，勾选 Allow remote debugging |
| 辅助功能 · `bin/DownieDestPicker.app` | 点 Downie 菜单、清空下载历史 | 系统设置 → 隐私与安全性 → 辅助功能 → 把该 app 拖进列表并勾选 |
| 自动化 · `DownieDestPicker` → 音乐 | 控制 `Music.app` | 首次脚本操作音乐时系统弹窗，点「允许」 |

> **为什么要绕一层 app**：WorkBuddy 是 Electron 应用，它派生的 `osascript` 在 TCC 里被记成「osascript」
> 而不是 WorkBuddy，所以给 WorkBuddy 授权后脚本仍报 `-10004 权限违例`。
> 独立 app 的 TCC 身份明确，**授权一次长期有效，且不需要重启 WorkBuddy**。

## 几个已踩实的坑

这些不是猜的，是实测结论，也写进了 SKILL.md 的故障对照表：

- **Downie 的「下载文件保存至文件夹」改不了。** 那是个自定义 `AXMenuButton`，一被点开 Downie 主线程就进入 `NSMenu` 模态跟踪，所有辅助功能查询直接卡死。改用**容器软链**（`~/Library/Containers/com.charliemonroe.Downie-4/Data/Downloads` → `tmp/music`）+ 关掉「确认每次下载的目的地」。
- **多 P 视频别指望 Downie。** `?p=N` 解析不可靠，常只认 P1 或卡在解析。走 `bilibili.py audio --p N` 直取音轨。
- **`artworks=0` 不代表没封面。** Music 导入时会重写标签抹掉 APIC，且不会立刻索引封面。可靠流程是三步：导入 → 给**库内文件**补 APIC → 重启 Music 强制重读。
- **`album_artist` 要单独查。** `music_lib.py find` 返回的是**曲目艺人**，合唱曲里它和 `album artist` 常常不同（如 `剑网3 & 王大力 & …` vs `剑网3`），照错的那个写，专辑立刻被拆成两张。
- **标题写「Hi-Res / 无损」的多是标题党。** B 站匿名最高只给 30280 档（常见 AAC 130–192kbps），转 MP3 320 只是换封装。用 `--format original` 取原始音轨、`ffprobe` 核实后再如实告知。

## 说明

- 仅用于个人备份已合法获取的音乐，不提供任何音源，下载行为由使用者自行负责。
- macOS 专用；脚本依赖系统 `Music.app` 的 AppleScript 字典，版本升级后个别属性可能变化。
- `bin/DownieDestPicker.app` 以编译产物形式入库：重新 `osacompile` 会改变 CDHash，导致已有的辅助功能授权失效，所以仓库里直接带成品 app。
