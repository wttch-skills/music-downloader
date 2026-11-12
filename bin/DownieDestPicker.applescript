-- DownieDestPicker：稳定的「执行器」，不含任何业务逻辑。
--
-- 设计原因：
--   WorkBuddy 是 Electron 应用，它派生的 osascript 在 TCC 里被记成「osascript」而不是
--   WorkBuddy，所以给 WorkBuddy 授权辅助功能后脚本仍报 -10004；用 do shell script 转发
--   又会报「osascript 不允许发送按键 (1002)」。只有独立 app 的 TCC 身份是明确的。
--   但 ad-hoc 签名的 app 一旦重新编译，CDHash 就变，辅助功能授权会失效、要用户重拖一次。
--   所以这里把 app 做成「只负责加载并运行外部脚本」的壳：辅助功能授权一次长期有效，
--   之后所有 UI 逻辑都改 ../bin/picker.applescript，随时迭代都不用再动这个二进制。
--
-- 协议（文件通信，避免 open --args 的兼容性问题）：
--   请求 /tmp/downie_pick_request.json
--     第 1 行  mode：check | script
--     第 2 行  （script 模式）外部 .scpt 绝对路径
--     第 3 行起 传给外部脚本 main(argText) 的参数（多行用 linefeed 连接）
--   结果 /tmp/downie_pick_result.json
--     OK||<细节> / ERROR||<原因>||<调试> / NEED_PERMISSION||<原因>

property reqPath : "/tmp/downie_pick_request.json"
property resPath : "/tmp/downie_pick_result.json"
property accessURL : "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"

-- ================================================================ 入口

on run
	set req to ""
	try
		set req to do shell script "/bin/cat " & quoted form of reqPath & " 2>/dev/null"
	end try
	if req is "" then
		my writeRes("ERROR||没有读到请求文件 " & reqPath)
		return
	end if

	-- AppleScript 文本的「行」是 CR 不是 LF，必须用 paragraphs
	set parts to paragraphs of req
	set mode to item 1 of parts

	-- 先确认能跟 System Events 通信（没有辅助功能权限时这里是第一道关）
	try
		tell application "System Events" to set _ to name of first process
	on error e
		my openAccessibilityPane()
		my writeRes("NEED_PERMISSION||无法控制 System Events：" & e)
		return
	end try

	if mode is "check" then
		-- 轻量自检：读别的进程 UI 需要辅助功能，读窗口数不会产生任何副作用
		try
			tell application "System Events"
				tell process "Finder"
					set _ to count of windows
				end tell
			end tell
			my writeRes("OK||辅助功能已授权")
		on error e
			my openAccessibilityPane()
			try
				do shell script "/usr/bin/open -R " & quoted form of (POSIX path of (path to me))
			end try
			my writeRes("NEED_PERMISSION||" & e)
		end try
		return
	end if

	if mode is "script" then
		my runScriptMode(parts)
		return
	end if

	my writeRes("ERROR||未知模式：" & mode)
end run

-- ================================================================ 外部脚本分发

on runScriptMode(parts)
	if (count of parts) < 2 then
		my writeRes("ERROR||script 模式缺少脚本路径")
		return
	end if

	set scptPath to item 2 of parts

	-- 第 3 行起全部参数，用 linefeed 拼回去交给外部脚本
	set argText to ""
	if (count of parts) >= 3 then
		repeat with i from 3 to (count of parts)
			if i is 3 then
				set argText to item i of parts
			else
				set argText to argText & linefeed & (item i of parts)
			end if
		end repeat
	end if

	-- 编译产物可能被系统清理，缺失时让 Python 侧重新 osacompile
	if not (do shell script "/bin/test -f " & quoted form of scptPath & " && echo yes || echo no") is "yes" then
		my writeRes("ERROR||外部脚本不存在：" & scptPath)
		return
	end if

	set s to missing value
	try
		set s to load script (POSIX file scptPath)
	on error e
		my writeRes("ERROR||加载外部脚本失败：" & e & "||" & scptPath)
		return
	end try

	set outText to ""
	try
		set outText to s's main(argText)
	on error e1
		try
			set outText to run s
		on error e2
			my writeRes("ERROR||执行外部脚本失败：" & e1 & " / " & e2)
			return
		end try
	end try

	if outText is missing value then set outText to ""
	my writeRes(outText as text)
end runScriptMode

-- ================================================================ 工具

on openAccessibilityPane()
	try
		do shell script "/usr/bin/open " & quoted form of accessURL
	end try
end openAccessibilityPane

on writeRes(t)
	try
		set cmd to "/bin/cat > " & quoted form of resPath & " <<'__WB_EOF__'" & linefeed & t & linefeed & "__WB_EOF__"
		do shell script cmd
	end try
end writeRes

on idle
	quit
end idle
