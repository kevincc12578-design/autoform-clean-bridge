# 2026-10-29 业务接入问题记录

## 材料文件框与内部编辑器

`business-dev-20261029-03` 的材料文件框虽读回本轮完整 MTB 路径，第一次 Open 后仍在；旁边材料预览器不能证明导入完成。随后背后的 AddMaterialEditor 保持未提交状态。原轮失败保留，不能因后续现场清理改为通过。

方案比较：盲目重复点击没有后置条件依据；对打开模态的按钮使用 Invoke 可能阻塞；选择输入路径稳定后提交并等待原文件框消失。文件框处理尚未被新整链验证时保持待验。

现有文件框取消后预览器一起消失。内部 AddMaterialEditor 的鼠标取消未使编辑器退出；改用其明确支持的 InvokePattern 后，现有恢复工具完成诊断保存和关闭，自有工程归零。证据：`runs/material-editor-cleanup-20261029-03/recovery/recovery-result.json`。这只是失败收尾，不是业务链验收。

原已验证鼠标方法包含移入目标后的等待；新 native.click_point 恢复这一时序，并保留目标窗口命中检查。不能仅凭输入发送成功声称业务完成。

参考：[Qt QFileDialog](https://doc.qt.io/qt-6/qfiledialog.html)、[Microsoft Invoke](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.invokepattern.invoke?view=windowsdesktop-10.0)、[mouse_event](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-mouse_event)。未找到公开 AutoForm 说明解释本次精确时序，结论限于现场观察。

## 开发脚本

`business-dev-20261029-04` 的成型检查应用按钮中心在 `(436,1054)`，窗口命中为Windows任务栏而非R13，因此动作被拒绝，随后本轮工程保存关闭。恢复窗口的底部超出工作区；业务导入前改为最大化R13，使底部操作进入可见工作区，各动作仍按当前控件矩形与命中检查执行。未隐藏任务栏或移除检查。依据：[ShowWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-showwindow)、[窗口与任务栏工作区](https://learn.microsoft.com/en-us/windows/win32/winmsg/about-windows)。

PowerShell 校准数据表达式中的逗号参与数组求值，引发 Object[] 除法错误；拆成独立标量计算后组装数组。替代方案是分别加括号或用 Python 计算，选择最小标量修复。依据：[运算符优先级](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_operator_precedence)。

临时检查脚本输出含零宽字符时 GBK 编码失败，原始 worker JSON 仍保存。后续临时脚本显式 `-X utf8`；未改系统环境。依据：[Python命令行](https://docs.python.org/3/using/cmdline.html)。

## 最小化恢复后的输入时序

研发第06轮 `create_design` 发送了输入但没有新增工程，保留失败结论且清理后自有0。随后只读审计发现R13处于最小化状态；恢复后即时截图仍处于DWM动画中，UIA已提供最终按钮矩形。该现象与微软说明的“GetWindowRect返回最终矩形而视觉仍在动画”一致，但不能排除同时的桌面交互影响，因此不将推断写成已证明的唯一原因。

保留一次创建和唯一新增身份判据；只在 `IsIconic` 恢复路径加0.4秒过渡等待，随后重新进行前台与窗口命中校验。没有新增自动重试创建。相比全局关闭系统动画或无条件重复点击，此修正只影响真实恢复动作。新链必须重新验证。

来源：[ShowWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-showwindow)、[微软关于DWM动画与GetWindowRect](https://devblogs.microsoft.com/oldnewthing/20120326-00/?p=8003)。

## 非空零件读回必须进入导入活动页

第07轮 GUI Start、本次原生日志、独立副本检查与完整求解全部通过，独立审计原件也通过这些项；但 `read_before` 的零件数未知、名字为空。根因是只进入PartSetup父阶段时保留上次成型检查活动，零件计数与列表位于导入活动。该轮 extract 原始树已经显示 `PartImportPageView` 内的 `1 零件(s)` 与实际零件名。

`read_part_state` 改为显式进入 `part.import`，仅在当前 `PartImportPageView` 读取名字，未知仍失败。保留父页导航通用语义，不扫描隐藏树、不重新导入。新增两项回归验证切换活动和导航失败。依据：[Qt QTabWidget当前页语义](https://doc.qt.io/qt-6/qtabwidget.html)。该失败轮次不算完整验收，已保存并关闭自有工程。
