# Downie / Music Converter / 音乐 App 技术笔记

## Downie 4

- Bundle：`com.charliemonroe.Downie-4`，沙盒应用。
- **脚本字典只有两个命令**（`sdef` 实测）：

  ```applescript
  tell application "Downie 4"
    open all URLs in text "https://..."   -- 返回 1=识别到链接并加入队列，0=没找到链接
    start queue                            -- 返回 1=已启动，0=已在运行，-1=队列为空
  end tell
  ```

- 没有「查询队列状态」「指定保存目录」「选择输出格式」的接口。因此 `downie.py`：
  - 用下载目录快照 diff 找新文件；
  - 用文件大小连续稳定 + 队列 plist 空闲双重确认完成；
  - 支持目录覆盖 `~/Downloads`、容器内 `Data/Downloads`、`~/Music`、`~/Movies`、`~/Desktop`（含一层子目录）。
- 偏好里 `XUAutomaticallyStartsDownloads = false`，所以投递后**必须显式 `start queue`**，否则文件只会躺在队列里。
- 支持的站点包含 B 站，投 `https://www.bilibili.com/video/BVxxxx` 即可。

## Music Converter

- Bundle：`com.shedworx.musicconverter`，版本 1.6.3。
- **没有 AppleScript 字典**（`sdef` 输出为空，包内无 `.sdef` 文件），只能走 System Events 的 UI 自动化。
- Info.plist 注册了 `CFBundleTypeName = "Media File"`，所以可以用 `open -a "Music Converter" <文件>` 把文件送进它的转换列表，不必模拟拖拽。
- UI 自动化步骤见 `convert.py` 的 `MC_CLICK_SCRIPT`：先在上拉菜单里选 Apple Lossless，再点名字含 Convert/转换 的按钮。
  界面结构随版本变化时，用 `ui_probe.py --app "Music Converter"` 重新抓树。
- **兜底等价**：macOS 自带的 `afconvert` 与它用的是同一个 CoreAudio ALAC 编码器，
  `afconvert -f m4af -d alac in.flac out.m4a` 产物一致，所以 UI 自动化失败时自动降级不会损失音质。

## 音乐 App（Music.app）

- 路径 `/System/Applications/Music.app`。
- 关键属性（`sdef` 实测均为可写）：`name` / `artist` / `album` / `album artist` / `composer` / `genre` /
  `year` / `track number` / `track count` / `disc number` / `artwork`（`data` 为 `picture` 类型，可写）。
- 导入用 `add (POSIX file "...") as alias`，返回 track 或 track list，取 `database ID` 作为后续引用。
- **`artwork` 需要 `make new artwork at t` 再设 `data`**，直接赋值无效。
- 顺序很重要：先把标签写进文件（`meta.py tag`），再导入 —— Music 导入时会读文件标签，多数字段一次到位；
  导入后再设一遍是防止 Music 缓存差异，封面则必须显式写。
- 同名同歌手重复导入会产生重复条目。需要覆盖时先按 `name` 查库、匹配 `artist` 后 `delete`，再导入（`--replace`）。

## TCC 权限

- 控制 Downie / Music / System Events 需要「自动化」权限；UI 自动化还需要「辅助功能」权限。
- 报错特征：`execution error: ... 发生权限违例 (-10004)`，或 `Not authorized to send Apple events`。
- 授权后**必须重启发起程序**（WorkBuddy / 终端），否则不会生效。
- `doctor.py` 会逐项检查并给出对应的设置路径。

## iTunes Search API

- `https://itunes.apple.com/search?term=&entity=song&limit=&country=`
- **`country=cn` 在本机网络环境下稳定返回 0 条**（出口 IP 被限流），需按 `cn → us → hk → tw → jp → gb`
  顺序回退，`meta.py` 已内置。
- 返回字段里没有 composer，「作曲」需要从帖子正文或视频简介补，`hifini.py resolve` 的
  `composer_from_post` 就是从论坛正文抓的。
- 封面：`artworkUrl100` 里的 `100x100bb.jpg` 可替换成 `1000x1000bb.jpg` 拿高清图。
