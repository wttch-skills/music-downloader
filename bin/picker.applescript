-- Downie 下载目录选择器的「业务逻辑」。
--
-- 这个文件由 DownieDestPicker.app（稳定执行器，已获辅助功能授权）通过
-- `load script` 加载并调用 main(argText)，所以改这里不需要重新编译 app，
-- 也不会让辅助功能授权失效。
--
-- 参数 argText（多行）：第 1 行动作 pick|dump，第 2 行目标目录。
-- 返回：OK||<细节> / ERROR||<原因>||<调试>

property maxDepth : 7

-- ================================================================ 入口

on main(argText)
	set ps to paragraphs of argText
	set action to "dump"
	set targetPath to ""
	if (count of ps) >= 1 then set action to item 1 of ps
	if (count of ps) >= 2 then set targetPath to item 2 of ps

	if action is "dump" then
		-- target 位传 "open" 表示先把设置窗口打开再 dump（用于校准界面结构）
		if targetPath is "open" then
			set oi to my openSettings()
			delay 1.5
		end if
		return my doDump()
	end if

	if action is "runscript" then
		-- 以 applet 自己的 TCC 身份执行外部 .scpt（多行结果原样返回）。
		-- 用途：WorkBuddy 派生的 osascript 控制「音乐」会被拒 -10004，
		-- 换成 applet 发包时身份明确，首次会弹一次「自动化」授权，点允许即可。
		-- 注意：目标脚本可能没有返回值（例如 quit），此时 run script 给 missing value，
		-- 直接 as text 会抛错，所以先兜底再转字符串。
		set r to "done"
		try
			set res to run script (POSIX file targetPath)
			if res is not missing value then set r to res as text
		on error e
			return "ERROR||" & e
		end try
		return "OK||" & r
	end if

	if action is "esc" then
		tell application "System Events"
			tell process "Downie 4"
				try
					key code 53
				end try
			end tell
		end tell
		return "OK||已发送 Esc"
	end if

	if action is "fixdest" then
		-- 关掉「确认每次下载的目的地」，让 Downie 直接落到默认下载目录（被软链到临时目录）
		return my doFixDest()
	end if

	if action is "clearhistory" then
		return my doClearHistory()
	end if

	if action is "openmenu" then
		

-- 只把「下载文件保存至文件夹」的下拉点开就返回：一旦菜单弹出，
		-- Downie 主线程进入模态跟踪，任何 AX 查询都会卡死，所以这里绝不 dump
		set oi to my openSettings()
		delay 1.5
		my selectSettingsPane("下载目录")
		delay 1.0
		return my openDestMenu()
	end if

	if action is "dumpdest" then
		-- 打开设置并切到「下载目录」分页后 dump
		set oi to my openSettings()
		delay 1.5
		set sel to my selectSettingsPane("下载目录")
		delay 1.0
		return my doDump() & linefeed & "SEL|" & sel
	end if

	if action is "dumpmenu" then
		-- 切到「下载目录」页，并点开保存位置下拉，dump 出菜单项
		set oi to my openSettings()
		delay 1.5
		set sel to my selectSettingsPane("下载目录")
		delay 1.0
		set clk to my clickDestControl()
		delay 1.2
		return my doDump() & linefeed & "SEL|" & sel & linefeed & "CLK|" & clk
	end if
	if action is "pick" then
		if targetPath is "" then return "ERROR||pick 缺少目标目录"
		return my doPick(targetPath)
	end if
	return "ERROR||未知动作：" & action
end main

-- ================================================================ 只读探测

on doDump()
	tell application "Downie 4" to activate
	delay 0.6

	set out to {}
	tell application "System Events"
		tell process "Downie 4"
			set frontmost to true
			delay 0.3

			-- 菜单栏：先确认「设置/偏好设置」菜单项到底叫什么
			try
				repeat with mi from 1 to (count of menu bar items of menu bar 1)
					set mbi to menu bar item mi of menu bar 1
					set mbName to ""
					try
						set mbName to (name of mbi) as text
					end try
					set end of out to "MB|" & mi & "|" & mbName
					try
						repeat with itm in menu items of menu 1 of mbi
							set itName to ""
							try
								set itName to (name of itm) as text
							end try
							set end of out to "MI|" & mi & "|" & itName & "|"
						end repeat
					end try
				end repeat
			on error e
				set end of out to "MBERR|" & e
			end try

			repeat with wi from 1 to (count of windows)
				set w to window wi
				set wName to ""
				try
					set wName to (name of w) as text
				end try
				set end of out to "W|" & wi & "|" & wName
				set out to my walk(w, wi, 1, maxDepth, out)
			end repeat
		end tell
	end tell

	set AppleScript's text item delimiters to linefeed
	set txt to out as text
	set AppleScript's text item delimiters to ""
	return "OK||" & txt
end doDump

on walk(el, wi, d, maxD, acc)
	if d > maxD then
		return acc
	end if

	-- UI elements / role / name 都是 System Events 的术语，handler 内必须重新 tell 一次
	tell application "System Events"
		try
			set kids to UI elements of el
		on error
			set kids to {}
		end try
	end tell

	repeat with k in kids
		set r to "?"
		set nm to ""
		set v to ""
		set dsc to ""
		tell application "System Events"
			try
				set r to (role of k) as text
			end try
			try
				set nm to (name of k) as text
			end try
			try
				set v to (value of k) as text
			end try
			try
				set dsc to (description of k) as text
			end try
		end tell
		-- 不过滤：AXMenuButton 之类的关键控件 name/value 常常为空，正是要靠位置判断
		set end of acc to "E|" & wi & "|" & d & "|" & r & "|" & nm & "|" & v & "|" & dsc
		set acc to my walk(k, wi, d + 1, maxD, acc)
	end repeat
	return acc
end walk

-- ================================================================ 选择目录

on doPick(targetPath)
	tell application "Downie 4" to activate
	delay 0.6

	-- 备份剪贴板：粘贴比 keystroke 可靠（中文路径用 keystroke 会丢字）
	set oldClip to ""
	try
		set oldClip to the clipboard as text
	end try

	set openInfo to my openSettings()
	if openInfo starts with "NEED_PERMISSION" then return openInfo

	-- 等设置窗口出现（名字含 设置/Preferences/Settings，或窗口数变多）
	set settingsWin to missing value
	repeat 20 times
		delay 0.4
		set settingsWin to my findSettingsWindow()
		if settingsWin is not missing value then exit repeat
	end repeat
	if settingsWin is missing value then
		return "ERROR||没能打开 Downie 设置窗口||" & openInfo
	end if

	-- 在设置窗口里找下载位置控件，点开下拉并选「选择目标文件夹…」
	set hitTitle to ""
	set debugInfo to ""
	set menuTitles to {"选择目标文件夹…", "选择目标文件夹...", "Select Destination Folder…", ¬
		"Select Destination Folder...", "选择文件夹…", "选择文件夹...", "Choose Folder…", ¬
		"Choose Folder...", "其他…", "其他...", "Other…", "Other..."}

	tell application "System Events"
		tell process "Downie 4"
			set w to my winByRef()
			try
				set elems to entire contents of w
			on error
				set elems to {}
			end try
			repeat with e in elems
				if hitTitle is not "" then exit repeat
				try
					set r to (role of e) as text
					if r is "AXPopUpButton" then
						set mis to {}
						try
							set mis to name of menu items of menu 1 of e
						end try
						set debugInfo to debugInfo & "popup[" & ((mis as text)) & "] ; "
						repeat with cand in menuTitles
							if mis contains (cand as text) then
								try
									click e
									delay 0.8
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
		end tell
	end tell

	if hitTitle is "" then
		return "ERROR||设置窗口里没找到「选择目标文件夹」下拉项||" & debugInfo
	end if

	-- 等文件选择面板
	set panel to missing value
	repeat 20 times
		delay 0.4
		set panel to my findPanel()
		if panel is not missing value then exit repeat
	end repeat
	if panel is missing value then
		return "ERROR||点了「" & hitTitle & "」但没等到文件选择面板||" & debugInfo
	end if

	tell application "System Events"
		tell process "Downie 4" to set frontmost to true
	end tell
	delay 0.5

	-- ⇧⌘G 前往目标目录，粘贴路径后回车
	tell application "System Events"
		tell process "Downie 4"
			keystroke "g" using {command down, shift down}
		end tell
	end tell
	delay 1.0

	set the clipboard to targetPath
	tell application "System Events"
		tell process "Downie 4"
			keystroke "a" using command down
			delay 0.2
			keystroke "v" using command down
		end tell
	end tell
	delay 0.6
	tell application "System Events"
		tell process "Downie 4"
			key code 36
		end tell
	end tell
	delay 1.8

	-- 确认（面板上的「打开/选择/Open」）
	set done to false
	set btnName to ""
	set panel2 to my findPanel()
	if panel2 is not missing value then
		tell application "System Events"
			tell process "Downie 4"
				set pnl to my panelByRef()
				if pnl is not missing value then
					repeat with b in (buttons of pnl)
						if done then exit repeat
						try
							set bn to (name of b) as text
							if bn is "Open" or bn is "打开" or bn is "Choose" or bn is "选择" or bn is "好" or bn is "OK" then
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
			end tell
		end tell
	else
		set done to true
		set btnName to "(回车已确认)"
	end if

	delay 1.2

	-- 关掉设置窗口，别留残留界面
	tell application "System Events"
		tell process "Downie 4"
			try
				keystroke "w" using command down
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

-- ================================================================ 定位窗口

-- 通过菜单栏打开设置窗口，返回说明文本
on openSettings()
	try
		tell application "System Events"
			tell process "Downie 4"
				set frontmost to true
				repeat with mi from 1 to (count of menu bar items of menu bar 1)
					set mbi to menu bar item mi of menu bar 1
					try
						repeat with itm in menu items of menu 1 of mbi
							set itName to ""
							try
								set itName to (name of itm) as text
							end try
							-- 必须精确匹配：Apple 菜单里有「系统设置…」，模糊 matching 会误开系统偏好
							if my isSettingsItem(itName) then
								click itm
								return "menu:" & itName
							end if
						end repeat
					end try
				end repeat
			end tell
		end tell
	on error e
		my openAccessibilityPane()
		return "NEED_PERMISSION||发送菜单点击被拒绝：" & e
	end try

	-- 兜底：⌘,
	try
		tell application "System Events"
			tell process "Downie 4" to keystroke "," using command down
		end tell
		return "keystroke:cmd,"
	on error e2
		return "NEED_PERMISSION||发送按键被拒绝：" & e2
	end try
end openSettings

on isSettingsItem(t)
	set s to (t as text)
	if s is "设置..." or s is "设置…" then return true
	if s is "偏好设置..." or s is "偏好设置…" then return true
	if s is "Settings..." or s is "Settings…" then return true
	if s is "Preferences..." or s is "Preferences…" then return true
	return false
end isSettingsItem

-- 关掉「确认每次下载的目的地」复选框。复选框的 AXPress 不会阻塞，
-- 和保存位置那个 AXMenuButton 不同（后者点开会进 NSMenu 模态跟踪，把 AX 查询全卡死）。
on doFixDest()
	set oi to my openSettings()
	if oi starts with "NEED_PERMISSION" then
		return oi
	end if
	delay 1.5
	my selectSettingsPane("下载目录")
	delay 1.0

	set n to my findSettingsWindow()
	if n is missing value then
		return "ERROR||没找到设置窗口"
	end if

	set vBefore to "?"
	set vAfter to "?"
	set found to false

	tell application "System Events"
		tell process "Downie 4"
			set elems to {}
			try
				set elems to entire contents of window n
			end try
			set cbx to missing value
			repeat with e in elems
				try
					set r to (role of e) as text
				on error
					set r to ""
				end try
				if r is "AXCheckBox" then
					set nm to ""
					try
						set nm to (name of e) as text
					end try
					if nm contains "确认每次下载的目的地" then
						set cbx to e
					end if
				end if
			end repeat
			if cbx is not missing value then
				set found to true
				try
					set vBefore to ((value of cbx) as integer) as text
				end try
				if vBefore is "1" then
					click cbx
					delay 0.6
					try
						set vAfter to ((value of cbx) as integer) as text
					end try
				else
					set vAfter to vBefore
				end if
			end if
		end tell
	end tell

	if not found then
		return "ERROR||没找到「确认每次下载的目的地」复选框"
	end if

	tell application "System Events"
		tell process "Downie 4"
			keystroke "w" using command down
		end tell
	end tell

	return "OK||确认目的地：" & vBefore & "->" & vAfter
end doFixDest

on clickDestControl()
	set n to my findSettingsWindow()
	if n is missing value then return "NO_WINDOW"

	tell application "System Events"
		tell process "Downie 4"
			set frontmost to true
			try
				set elems to entire contents of window n
			on error
				return "NO_CONTENT"
			end try
			repeat with e in elems
				try
					if (role of e) as text is "AXMenuButton" then
						-- 注意：AXPress 会让 NSMenu 进入模态跟踪循环，把 AppleScript 卡死，
						-- 所以这里用瞬时的坐标点击
						set pos to position of e
						set sz to size of e
						set cx to (item 1 of pos) + ((item 1 of sz) div 2)
						set cy to (item 2 of pos) + ((item 2 of sz) div 2)
						click at {cx, cy}
						return "CLICKED_AT_" & cx & "_" & cy
					end if
				end try
			end repeat
			-- 退路：找 value 不是「下载」的 AXPopUpButton
			repeat with e in elems
				try
					if (role of e) as text is "AXPopUpButton" then
						set v to ""
						try
							set v to (value of e) as text
						end try
						if v is not "下载" then
							click e
							return "CLICKED_POPUP:" & v
						end if
					end if
				end try
			end repeat
		end tell
	end tell
	return "NOT_FOUND"
end clickDestControl


-- 分页切换是异步的，标题要轮询等，不能点完就判断
on waitPaneTitle(paneName, n, limit)
	repeat with i from 1 to limit
		delay 0.4
		set ttl to ""
		tell application "System Events"
			tell process "Downie 4"
				try
					set ttl to (name of window n) as text
				end try
			end tell
		end tell
		if ttl contains paneName then return true
	end repeat
	return false
end waitPaneTitle

-- 点设置窗口左侧列表里的某个分页（AXCell 或其中的 AXStaticText）
-- 实测直接把 AXCell click 掉没反应，必须按坐标点，所以两种方式都试
on selectSettingsPane(paneName)
	set n to my findSettingsWindow()
	if n is missing value then return "NO_WINDOW"

	tell application "System Events"
		tell process "Downie 4"
			set frontmost to true
			delay 0.3
			try
				set elems to entire contents of window n
			on error
				return "NO_CONTENT"
			end try

			repeat with e in elems
				try
					if (role of e) as text is "AXCell" then
						set nm to ""
						try
							set nm to (name of e) as text
						end try
						if nm is paneName then
							-- 1) 原生 click
							try
								click e
								if my waitPaneTitle(paneName, n, 6) then return "CLICKED_CELL"
							end try
							-- 2) 坐标点击（AXCell 常常不响应 click 动作）
							try
								set pos to position of e
								set sz to size of e
								set cx to (item 1 of pos) + ((item 1 of sz) div 2)
								set cy to (item 2 of pos) + ((item 2 of sz) div 2)
								click at {cx, cy}
								if my waitPaneTitle(paneName, n, 8) then return "CLICKED_AT"
								return "NOT_SWITCHED:" & paneName
							on error errMsg
								return "CLICK_ERR:" & errMsg
							end try
						end if
					end if
				end try
			end repeat
		end tell
	end tell
	return "NOT_FOUND"
end selectSettingsPane

on findSettingsWindow()
	set found to missing value
	tell application "System Events"
		tell process "Downie 4"
			repeat with wi from 1 to (count of windows)
				set w to window wi
				set wName to ""
				try
					set wName to (name of w) as text
				end try
				if wName contains "设置" or wName contains "偏好" or wName contains "Preferences" or wName contains "Settings" or wName contains "Downie 4" then
					set found to wi
					exit repeat
				end if
				-- 设置窗口常常没有标题，用子元素里是否出现「通用/后处理」判断
				try
					set elems to entire contents of w
					repeat with e in elems
						try
							if (role of e) is "AXStaticText" then
								set sv to (value of e) as text
								if sv contains "通用" or sv contains "后处理" or sv contains "General" or sv contains "Postprocessing" then
									set found to wi
									exit repeat
								end if
							end if
						end try
					end repeat
				end try
				if found is not missing value then exit repeat
			end repeat
		end tell
	end tell
	return found
end findSettingsWindow

on winByRef()
	set n to my findSettingsWindow()
	if n is missing value then return missing value
	tell application "System Events"
		tell process "Downie 4"
			return window n
		end tell
	end tell
end winByRef

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
						if wName contains "Open" or wName contains "打开" or wName contains "选择" then
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

on panelByRef()
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
						if wName contains "Open" or wName contains "打开" or wName contains "选择" then
							set panel to window wi
							exit repeat
						end if
					end try
				end repeat
			end if
		end tell
	end tell
	return panel
end panelByRef

-- 清空 Downie 的历史/已完成列表：走「文件」菜单里的两个清空项，
-- 这样和用户手动点效果一致，也顺带把队列里残留的条目去掉。
on doClearHistory()
	tell application "Downie 4" to activate
	delay 0.6
	my dismissDialog()
	delay 0.5
	set r1 to my clickFileMenuItem("清空已完成的下载")
	delay 1.2
	my dismissDialog()
	delay 0.4
	set r2 to my clickFileMenuItem("清空未激活的下载")
	delay 1.2
	my dismissDialog()
	set r3 to my clickFileMenuItem("清空已完成的下载")
	return "OK||" & r1 & " / " & r2 & " / " & r3
end doClearHistory

on clickFileMenuItem(itemName)
	tell application "System Events"
		tell process "Downie 4"
			set frontmost to true
			repeat with mi from 1 to (count of menu bar items of menu bar 1)
				set mbi to menu bar item mi of menu bar 1
				set mbName to ""
				try
					set mbName to (name of mbi) as text
				end try
				if mbName is "文件" or mbName is "File" then
					try
						click mbi
						delay 0.5
						click menu item itemName of menu 1 of mbi
						return "clicked:" & itemName
					on error e
						try
							key code 53
						end try
						return "fail:" & itemName & "(" & e & ")"
					end try
				end if
			end repeat
		end tell
	end tell
	return "no-file-menu"
end clickFileMenuItem

-- 清空操作会弹确认框（「移除 / 取消」），要点「移除」而不是拍回车
on dismissDialog()
	tell application "System Events"
		tell process "Downie 4"
			repeat with wi from 1 to (count of windows)
				try
					repeat with b in (buttons of window wi)
						set bn to ""
						try
							set bn to (name of b) as text
						end try
						if bn is "移除" or bn is "Remove" or bn is "确定" or bn is "OK" or bn is "好" or bn is "清空" or bn is "删除" then
							click b
							return "clicked:" & bn
						end if
					end repeat
				end try
			end repeat
		end tell
	end tell
	return "no-dialog"
end dismissDialog

on openAccessibilityPane()
	try
		do shell script "/usr/bin/open " & quoted form of "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
	end try
end openAccessibilityPane
