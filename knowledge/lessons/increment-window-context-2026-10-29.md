# 输入守护合并时必须保留业务上下文

问题：隔离环境的输入守护只覆盖空工程和文件框。直接接入既有业务后，出现 `WINDOW_NOT_OWNED`、`UNEXPECTED_OWNED_WINDOW` 和 `FILENAME_SUGGESTION_CONTEXT_INVALID`，实际包括无 Win32 owner 的 AFMenu/材料提示、与材料文件框同 owner 的预览、伸出文件框的建议列表、Qt/Windows 工具提示。

根因：原生 owner 链不是 Qt 所有业务窗口关系的完整表达，工具提示也不等于交互模态。Qt 根据类型和阴影等样式生成不同原生类名。旧业务封装有时只保留错误字符串，丢失被拒绝窗口的细节，使瞬时问题难以回看。

方案：在既有输入入口增加固定 profile、捕获及额外窗口守护；业务特例由当前 UIA 类型与新鲜原生窗口快照证明，材料预览只在本次文件框事务内允许。只排除明确的工具提示/阴影类，仍核对捕获和点击命中。结构化拒绝明细写入现有监督器 stderr，保留原失败结果。文件框建议列表核对 Edit、焦点、进程、线程、贴合位置与工作区，不能仅要求它位于文件框内部。

候选方案比较：全局放行同进程会失去保护；强制关闭辅助窗口会改变既有流程；当前按业务证据限定允许的做法保留了两者。不要重新实现生命周期来解决输入上下文问题。

来源：[Qt窗口类注册源码](https://raw.githubusercontent.com/qt/qtbase/5.15/src/plugins/platforms/windows/qwindowscontext.cpp)、[Win32 GetCapture](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getcapture)、[Win32自动补全](https://learn.microsoft.com/en-us/windows/win32/shell/how-to-enable-autocomplete-manually)、[Qt QDialog](https://doc.qt.io/qt-6/qdialog.html)。

本地证据、修复轮次与最终验证范围见 [增量合并记录](../../docs/增量合并记录-2026-10-29.md)；离线和恢复成功不能替代同版三个空工程整链通过。
