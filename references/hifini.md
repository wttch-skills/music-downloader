# hifini.com.cn 站点笔记

> 结论基于 2026-11 实测。站点改版后请用 `hifini.py probe` 重新校准。

## 平台特征

- 站点名「HiFiNi 音乐磁场」，Discuz 系论坛模板，主营无损音乐分享（FLAC / 母带 / MP3-320K）。
- **有 JS 反爬**：未执行 JS 时首页只返回 39 字节 `<script src="/_guard/auto.js"></script>`。curl / WebFetch 一律拿不到内容。
- **搜索需要登录**：未登录访问搜索地址会被重定向到「用户登录」页（`<title>用户登录</title>`）。回复取链同样依赖登录态。
- 因此本 skill 走 CDP 复用用户 Chrome 的真实登录态，不开独立浏览器。

## URL 结构

| 用途 | 形式 |
| --- | --- |
| 首页 | `https://www.hifini.com.cn/` |
| 搜索 | `https://www.hifini.com.cn/?search.htm&keyword=<URL编码关键词>` |
| 帖子 | `https://www.hifini.com.cn/thread-<数字>.htm` |
| 版块 | `forum-<数字>.htm` |
| 标签 | `tag-<数字>.htm` |
| 用户 | `user-<数字>.htm` |

搜索表单实测：`<form action="?search.htm">` + `<input name="keyword" placeholder="输入歌名,搜索一下">`，GET 提交，
所以是 `?search.htm&keyword=` 这种「query 里带页面名」的非标准形式，注意别写成 `/search.htm?keyword=`。

## 帖子标题的格式约定

标题里直接标了音质，是选源的主要依据，例如：

```
Beyond《情人》[24Bit/192Khz最高音质 内嵌封面歌词][臻品母带...]
阿梨粤《不该用情》[MP3-320K/9M]
美玲儿《爱江山更爱美人》[FLAC/MP3-320K]
```

`hifini.py search` 会把 FLAC / 母带 / 24Bit / MP3-320K / 内嵌封面 等标记提取到 `format` 字段。

## 已知陷阱

- **公告/求曲帖混在结果里**：标题常带 `[小火]` `[必火]` `[火火]` 前缀，或是「求一首…」「发贴必看…」「版规」。
  这些不是音源，脚本已按 `ADMIN_HINTS` 过滤。
- **隐藏内容**：下载地址常包在「本帖含有隐藏内容，请您回复后查看」里。回复后需要**重新加载页面**隐藏块才会渲染，
  脚本在回复成功后会自动重新导航一次。
- **网盘为主**：下载地址大概率是百度网盘 / 夸克，Downie 无法处理。只有 `direct` 类型（论坛附件、直链 flac/mp3）
  才能全自动下载，其余需要用户手动存到临时目录。
- **同曲多版本**：同一首歌常有 FLAC / MP3 / 24Bit / 演唱会版等多个帖子，必须让用户选，不能自动挑。

## 校准方法

登录态下执行，把页面结构 dump 到 `tmp/music/_probe/page.json`：

```bash
python3 scripts/hifini.py probe --url "https://www.hifini.com.cn/thread-XXXX.htm"
```

看 `textareas` / `buttons` / `links` 三项，据此调整 `REPLY_JS_TMPL` 的选择器。
