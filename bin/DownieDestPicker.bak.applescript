-- DownieDestPicker：独立小 app，以「自己的身份」完成 Downie 下载目录的 UI 选择与界面探测。
--
-- 为什么要独立 app：WorkBuddy 是 Electron 应用，它派生的 osascript 在 TCC 里会被判定成
-- 「osascript」而不是 WorkBuddy，所以给 WorkBuddy 授权辅助功能后脚本仍报 -10004；
-- 反过来用 do shell script 转发到 osascript，又会报「osascript 不允许发送按键 (1002)」。
-- 把逻辑内联在 app 里，权限归属就明确是 DownieDestPicker，且不需要重启宿主程序。
--
-- 协议（文件通信，避免 open --args 的兼容性问题）：
--   请求 /tmp/downie_pick_request.json
--     第 1 行 mode：pick | probe
--     第 2 行（仅 pick）目标目录绝对路径
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

	-- 先确认能跟 System Events 通信（没有自动化权限时这里是第一道关）
	try
		tell application "System Events" to set _ to name of first process
	on error e
		my openAccessibilityPane()
		my writeRes("NEED_PERMISSION||无法控制 System Events：" & e)
		return
	end try

	set outText to ""
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

	if mode is "probe" then
		set outText to my doProbe()
	else
		if (count of parts) < 2 then
			my writeRes("ERROR||pick 模式缺少目标目录")
			return
		end if
		set outText to my doPick(item 2 of parts)
	end if

	my writeRes(outText)
end run

-- ================================================================ 选择目录

on doPick(targetPath)
	-- 下拉里打开文件选择器的项，中英文与不同版本都覆盖一遍
	set menuTitles to {"选择目标文件夹…", "选择目标文件夹...", "Select Destination Folder...", ¬
		"选择文件夹...", "Choose folder...", "自定存储位置...", "Custom Destination...", ¬
		"其他...", "Other..."}

	-- 备份剪贴板：粘贴比 keystroke 可靠（中文路径用 keystroke 会丢字）
	set oldClip to ""
	try
		set oldClip to the clipboard as text
	end try

	-- ---------------------------------------------- 1. 打开设置窗口
	tell application "Downie 4" to activate
	delay 0.8
	try
		tell application "System Events"
			tell process "Downie 4"
				keystroke "," using command down
			end tell
		end tell
	on error e
		-- 把设置面板翻到「辅助功能」，并在 Finder 里选中自己，方便直接拖进列表
		my openAccessibilityPane()
		try
			do shell script "/usr/bin/open -R " & quoted form of (POSIX path of (path to me))
		end try
		return "NEED_PERMISSION||发送按键被拒绝：" & e
	end try

	set winCount to 0
	repeat 16 times
		delay 0.5
		tell application "System Events"
			tell process "Downie 4"
				set winCount to (count of windows)
			end tell
		end tell
		if winCount > 1 then exit repeat
	end repeat

	-- ---------------------------------------------- 2. 点开下载位置下拉
	set hitTitle to ""
	set debugInfo to ""
	tell application "System Events"
		tell process "Downie 4"
			set frontmost to true
			repeat with wi from 1 to (count of windows)
				if hitTitle is not "" then exit repeat
				try
					set elems to entire contents of window wi
				on error
					set elems to {}
				end try
				repeat with e in elems
					if hitTitle is not "" then exit repeat
					try
						if (role of e) is "AXPopUpButton" then
							set mis to {}
							try
								set mis to name of menu items of menu 1 of e
							end try
							if (count of mis) > 0 then
								set debugInfo to debugInfo & "popup:" & (mis as text) & " ; "
							end if
							repeat with cand in menuTitles
								if mis contains (cand as text) then
									try
										click e
										delay 0.7
										click menu item (cand as text) of menu 1 of e
										set hitTitle to (cand as text)
									on error errMsg
										set debugInfo to debugInfo & "clickFail:" & errMsg & " ; "
									end try
									exit repeat
								end if
							end repeat
						end if
					end try
				end repeat
			end repeat
		end tell
	end tell

	if hitTitle is "" then
		return "ERROR||没找到「选择目标文件夹」菜单项（设置窗口没打开或界面已改版）||" & debugInfo
	end if

	-- ---------------------------------------------- 3. 等文件选择面板
	set panel to missing value
	repeat 20 times
		delay 0.5
		set panel to my findPanel()
		if panel is not missing value then exit repeat
	end repeat

	if panel is missing value then
		return "ERROR||点了「" & hitTitle & "」但没等到文件选择面板||" & debugInfo
	end if

	tell application "System Events"
		tell process "Downie 4"
			set frontmost to true
		end tell
	end tell
	delay 0.6

	-- ---------------------------------------------- 4. ⇧⌘G 前往目标目录
	tell application "System Events"
		tell process "Downie 4"
			keystroke "g" using {command down, shift down}
		end tell
	end tell
	delay 1.2

	set the clipboard to targetPath
	tell application "System Events"
		tell process "Downie 4"
			keystroke "a" using command down
			delay 0.2
			keystroke "v" using command down
		end tell
	end tell
	delay 0.8
	tell application "System Events"
		tell process "Downie 4"
			key code 36 -- 回车，导航到该目录
		end tell
	end tell
	delay 2.0

	-- ---------------------------------------------- 5. 确认
	set done to false
	set btnName to ""
	set stillOpen to false
	tell application "System Events"
		tell process "Downie 4"
			repeat with wi from 1 to (count of windows)
				try
					if (count of sheets of window wi) > 0 then set stillOpen to true
				end try
			end repeat
			if stillOpen then
				repeat with wi from 1 to (count of windows)
					if done then exit repeat
					try
						set w to window wi
						if (count of sheets of w) > 0 then
							set pnl to sheet 1 of w
							repeat with b in (buttons of pnl)
								if done then exit repeat
								try
									set bn to name of b
									if bn is "Open" or bn is "打开" or bn is "Choose" or bn is "选择" then
										click b
										set btnName to bn
										set done to true
									end if
								end try
							end repeat
							if not done then
								try
									click button 1 of pnl
									set btnName to "button 1"
									set done to true
								end try
							end if
						end if
					end try
				end repeat
			else
				set done to true
				set btnName to "(回车已确认)"
			end if
		end tell
	end tell

	delay 1.5

	-- 关掉设置窗口，别留残留界面
	tell application "System Events"
		tell process "Downie 4"
			try
				keystroke "." using command down
			end try
		end tell
	end tell

	try
		set the clipboard to oldClip
	end try

	if done then
		return "OK||" & hitTitle & "||" & btnName
	else
		return "ERROR||面板没有确认成功||" & debugInfo
	end if
end doPick

-- 找文件选择面板：优先 sheet，其次标题含 Open/打开 的独立窗口
on findPanel()
	set panel to missing value
	tell application "System Events"
		tell process "Downie 4"
			repeat with wi from 1 to (count of windows)
				try
					if (count of sheets of window wi) > 0 then
						set panel to sheet 1 of window wi
						exit repeat
					end if
				end try
			end repeat
			if panel is missing value then
				repeat with wi from 1 to (count of windows)
					try
						set wName to name of window wi
						if wName contains "Open" or wName contains "打开" then
							set panel to window wi
							exit repeat
						end if
					end try
				end repeat
			end if
		end tell
	end tell
	return panel
end findPanel

-- ================================================================ 探测（只读）

on doProbe()
	tell application "Downie 4" to activate
	delay 0.8
	try
		tell application "System Events"
			tell process "Downie 4"
				keystroke "," using command down
			end tell
		end tell
	on error e
		my openAccessibilityPane()
		try
			do shell script "/usr/bin/open -R " & quoted form of (POSIX path of (path to me))
		end try
		return "NEED_PERMISSION||发送按键被拒绝：" & e
	end try

	set winCount to 0
	repeat 16 times
		delay 0.5
		tell application "System Events"
			tell process "Downie 4"
				set winCount to (count of windows)
			end tell
		end tell
		if winCount > 1 then exit repeat
	end repeat

	set out to {}
	tell application "System Events"
		tell process "Downie 4"
			set frontmost to true
			repeat with wi from 1 to (count of windows)
				try
					set wName to name of window wi
				on error
					set wName to ""
				end try
				set end of out to "0|AXWindow|" & wName & "|" & wi
				try
					set elems to entire contents of window wi
				on error
					set elems to {}
				end try
				repeat with e in elems
					set r to "?"
					try
						set r to (role of e) as text
					end try
					set nm to ""
					try
						set nm to (name of e) as text
					end try
					set v to ""
					try
						set v to (value of e) as text
					end try
					if nm is not "" or v is not "" then
						set end of out to "1|" & r & "|" & nm & "|" & v
					end if
				end repeat
			end repeat
		end tell
	end tell

	tell application "System Events"
		tell process "Downie 4"
			try
				keystroke "." using command down
			end try
		end tell
	end tell

	set AppleScript's text item delimiters to linefeed
	set txt to out as text
	set AppleScript's text item delimiters to ""
	return "OK||" & txt
end doProbe

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
