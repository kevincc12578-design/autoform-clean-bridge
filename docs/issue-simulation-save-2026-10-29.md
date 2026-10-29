# GUI Start 保存期间读取 AFD 被拒绝

2026-10-29，`runs/business-dev-20261029-05/run-1/gui-solver/simulation.json` 记录：Save As 输入和文件创建后，文件仅有 9,568,994 字节，随后 `_save_transaction()` 的完整 SHA256 读取抛出 `PermissionError(13, 'Permission denied')`。只读复查时文件已增至 26,111,359 字节并可读取。故已知的是初始验证发生在文件仍变化期间；原异常未记录 WinError，不能把 errno 13 单独断言为某一个 Win32 错误码。

官方依据：

- [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)：元数据访问可能成功，而读访问仍被现有句柄共享模式拒绝；新共享模式不能覆盖现有句柄限制。
- [Synchronous and Asynchronous I/O](https://learn.microsoft.com/en-us/windows/win32/fileio/synchronous-and-asynchronous-i-o)：操作启动和真正完成是不同事件。正文件大小不能替代应用层完成信号。
- [Python os.access](https://docs.python.org/3/library/os.html#os.access)：预检查和实际打开之间存在竞态，应执行实际读取并处理异常。

比较方案：

1. 初次保存后固定等待再计算哈希：仍可能落入较慢写入，且不证明本次求解完成。
2. 改用更宽松的共享模式读取：不能越过现有共享限制，并可能读取并发写入中的内容，不采用。
3. **采用：** 初次只核对文件框路径及原页签身份，保存事务保持 pending；本次 GUI job 完成且原生日志满足有序 Springback/全部 END0 后，在原阶段剩余时间内等待普通读取可用，要求两次完整哈希及元数据一致，再返回成功保存事务。暂时的 PermissionError/FileNotFoundError有界重试；永久不可读或持续变化保持失败。

回归测试位于 `tests/test_solver_business.py::FinalSaveBytesTests`：初期不误报保存、暂时拒读、不同哈希、读取期间文件变化和永久锁定超时。新现场结果由后续独立 run 记录；本修复文档不替代现场验收。
