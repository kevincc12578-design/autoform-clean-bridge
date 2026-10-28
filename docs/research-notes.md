# 本轮调研与现场问题

日期：2026-10-28。以下区分直接观察、官方接口说明与尚未证明的推断。所有判断只适用于本轮 R13 和当前桌面环境；不继承旧库的成功状态。

## 许可证服务器字符串与启动窗口

直接观察：首轮子进程继承 `AF_LICENSE_SERVER=2375@localhost`，出现许可证失败。随后仅对新启动进程传入 `AF_LICENSE_SERVER=2375@127.0.0.1`，进入许可证选择窗口；应用当前已选方案后出现 R13 主窗口及欢迎框。没有修改系统环境、许可证服务或安全设置。最初显示 Error 的进程在准备关闭动作前已经不存在，不能把它记录为本库成功关闭或回收的进程。

证据保存在 `runs/discovery-20261028-01/`、`runs/discovery-20261028-02/` 的窗口、worker 请求/响应和启动记录中。

| 候选方案 | 适用性与风险 | 本轮决定 |
| --- | --- | --- |
| 保留 `localhost`，继续重试 | 不改变环境，但同一失败没有新证据；重复启动可能留下多个错误窗口 | 不自动重试 |
| 显式 IPv4 回环地址，仅放入新进程环境 | 减少主机名解析路径差异；仍需核对本机服务实际地址和结果 | 本次场景成功，保留为可配置参数 |
| 修改系统解析、网络配置或许可证服务 | 影响范围大于本桥接，且本轮没有证据表明必须修改 | 未实施 |

推断：`localhost` 与显式 IPv4 的差异可能涉及地址解析、监听地址或客户端协议选择，但本轮没有抓取足够网络/服务证据证明唯一根因。不能写成“已确认 IPv6 导致失败”，也不能宣称所有 AutoForm 安装都应使用这个地址。

参考入口为 [Reprise Software：IPv6 considerations](https://docs.reprisesoftware.com/docs/15-2/admin/ipv6-considerations.html)。本文编写时该页面抓取超时，因此仅保留其作为后续核查入口，不将未读出的正文当作本机根因证明。

代码策略：`connect(..., allow_selected_license=False)` 默认不应用许可证。只有本次实际新启动且调用者显式开启该选项时，才在验证窗口类型和唯一 checkout 按钮后应用已选方案；不更改许可证选项。现有会话附着时即使传 True，也不操作其许可证窗口。未知启动窗口立即留证并返回 `needs_user`。

## 文件框 ValuePattern 读回正确，但保存目标不同

直接观察：在系统“另存为”文件框中通过 `ValuePattern.SetValue` 写入绝对路径后，UIA 读回显示目标字符串正确，但提交后文件落在 `C:/Users/Administrator/Documents/Untitled.afd`。这是一次真实意外产物，保留且未删除。随后遇到同名覆盖提示时选择“否”，没有覆盖该文件。

加入焦点处理的 ValuePattern 路线没有解决本次现象。切换为受守护的 Unicode 真实键盘输入后，本次成功另存为 `D:/ai库/autoform-clean-bridge/runs/calibration-20261028-01/calibration.afd` 并关闭该自有工程。对应文件框观察、请求/响应、`saved.json`、`closed.json` 和 AFD 留在校准目录。

| 候选方案 | 官方依据与适用范围 | 本轮结果/决定 |
| --- | --- | --- |
| `ValuePattern.SetValue`，先聚焦再读回 | Microsoft 示例展示先聚焦并使用 ValuePattern 为文本框赋值；该说明不保证宿主应用已接收完整保存事务 | 本次未解决，不继续把字符串读回当保存证明 |
| 聚焦目标输入框、选择原内容、Unicode 键盘输入，再读回并提交 | Microsoft 将模拟键盘输入列为文本写入的另一条路线；实际输入仍需目标和焦点守护 | 本次保存校准成功，继续保留路径、标题、文件框结束及重开验证 |
| 调用文件框内部 `IFileDialog` 对象 | 官方接口提供文件名、目录、显示及结果等方法；本轮没有已验证的入口可附着第三方应用正在显示的内部实例 | 未采用，不尝试私有对象或进程内存路线 |

文本框接口背景见 [Microsoft：Add Content to a Text Box Using UI Automation](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/add-content-to-a-text-box-using-ui-automation)。公共文件框方法说明见 [Microsoft：IFileDialog](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nn-shobjidl_core-ifiledialog)。这些文档解释可用接口，不直接证明 AutoForm 此处的失败原因。

推断：现象可能与控件通知、内部文件名状态或提交时机有关；本轮未观察宿主内部事件，不能确定具体机制，更不能推广为“ValuePattern 总是无效”。本次结论仅是：在这套文件框中，ValuePattern 的值读回不足以证明最终保存路径，Unicode 输入路线解决了当前校准案例。

由此保留的门禁是完整事务：精确文件框和自有工程 → 输入绝对路径并读回 → 提交 → 等待文件框结束和主窗恢复 → 同一 RuntimeId/标题与文件证据 → 真正关闭 → 从同一路径重开并核对语义。`dirty` 仍是 `None`，路径来源注明为文件框事务证据；没有冒充 AFD SDK 对源路径的直接读取。

## UIA 调用的隔离与过期对象

Microsoft 说明 UI Automation 与 Windows 消息/COM 线程关系不当时可能出现阻塞或性能问题，并指出跨 COM apartment 生命周期可能使元素失效。参见 [Microsoft：Understanding Threading Issues](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading)。该文档是线程与对象生命周期依据，不是对本实现所有超时场景的保证。

本轮采用短命 worker：每次调用重新获得窗口与控件，父监督器控制超时，异常或取消时回收自有 worker，保留请求、响应、stdout/stderr 和命令原件。最终实现用私有 Windows Job Object 同时约束 venv 启动器及其解释器子进程。Windows 命名互斥按 R13 PID 限制单写入；实际输入前还核验窗口归属、当前矩形、前台与命中窗口。

与长时间保留 UIA 对象相比，这增加了启动开销，但便于限制单次探测并清除过期对象。没有把 COM 元素跨 worker 序列化传递；页签 RuntimeId 仅用于当前会话定位，关闭后不能复用旧工程 token。

## 判据与后续核查

- 校准工程的保存和关闭成功，不等于完整生命周期通过；还必须核对同文件重开、语义回读、再次关闭、自有页签归零和基线保留。
- P1 当前只核对空工程的显式实体计数，不能由此推断非空 CAD、材料、工序或求解结果已持久化。
- 45 项离线测试是本记录编写时的阶段结果，尚未包含后续 CLI 测试和待结束的现场生命周期验证。
- 本轮没有证明许可证问题的唯一网络根因，也没有证明 ValuePattern 问题的内部通知根因；如需进一步定位，应另设有界调查并保留新的原始证据。

## 后续现场修正与 03 轮验收审计

保存期间另观察到短暂的可见顶层窗口：owner 为 0，标题为 `AutoForm Forming`，原生 class 为 `Qt5QWindowIcon`，矩形位于桌面可用区域之外；其 UIA 根为 `HoopsViewImpl`。这类已观察到的临时视口只作为“保存仍在收尾”的证据，适配器等待它消失，不能把其出现或文件产生当作保存完成。未匹配该特征的额外窗口仍保持未知模态处理。

打开框和另存为框也不能共用同一定位假设。本机场景中打开框标题为“打开设计文件”，文件名 Edit 的 AutomationId 为 `1148`；另存为框标题为“另存为”，对应 Edit 为 `1001`。02 轮因打开框识别不符失败，已取消文件框并确认工程归零；该失败未计入成功。03 轮原始 worker 证据分别记录两个控件，并提交和读回完全相同的工作文件路径。

`runs/lifecycle-20261028-03/acceptance-audit.json` 独立重解析了 71 次 worker 请求/响应、73 份隔离记录及生命周期事件，并记录 231 份关键证据的 SHA256。核对创建、保存、第一次关闭清单为空、同一路径重开、前后空工程显式计数相同、再次保存关闭、清理为零和断开顺序；最终 AFD 哈希与最后保存事件一致。03 轮 CLI 用时 31.646 秒，生命周期内部用时 31.436 秒。该结论只覆盖 P1 空工程，本轮基线是零工程，未做求解或三轮抱箍链验收。

资源必须分别报告：03 轮工作集由 245,260,288 降到 196,644,864 字节；专用字节由 869,720,064 增到 1,069,096,960 字节，增加 **199,376,896 字节**。结束页签为零不代表全部内存释放；单轮数据也不足以判定持续泄漏。

审计期间曾同时观察到两个属于另一个正在执行的 04 轮命令的 Python worker，之后只读进程快照归零；它们不是 03 轮遗留。这个观察同时提示真实取消验收的边界：Python 官方说明 Windows venv 使用 redirector，而 Windows 上 `subprocess.kill()` 针对该进程调用终止接口。正常 worker 完成不能替代真实取消/超时情况下的子进程回收验证；需另做不触 UI 的进程实验或进程容器验证。参见 [Python venv](https://docs.python.org/3/library/venv.html) 和 [Python subprocess](https://docs.python.org/3/library/subprocess.html)。03 正常链审计明确将 `cancellation_cleanup_live_verified` 保持为 `false`。

## 最终版文件框定位与 07 轮

05轮遍历打开框中的Shell目录项时，读取AutomationId遇到COM错误。最终实现用原生EnumChildWindows/GetDlgCtrlID定位文件名和确认/取消按钮，仅观察这些核心控件；不再为了输入路径遍历整个文件目录树。参考 [Microsoft EnumChildWindows](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-enumchildwindows) 与 [GetDlgCtrlID](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getdlgctrlid)。

06轮发送Unicode路径后固定0.2秒读回失败，而随后原始观察显示完整正确路径。改为最多3秒等待输入框值达到精确目标，不重复输入、不放宽字符串比较。[Microsoft SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)说明输入进入输入流；该函数返回不作为宿主控件已完成处理的证明。06失败结果保持不变，取消本轮打开框后保留原有页签。

07轮在上述修正后完整通过，用时39.181秒。基线1个页签保留，本轮自有和未知新增页签均为0。再次保存关闭后，工作集较开始减少，专用字节仍增加234795008字节；未宣布全部原生内存释放。校准时的意外Untitled.afd已校验哈希后移入本库calibration证据目录，原始内容保留。

真实进程测试最初用“PID与创建时间还能查询”判断未退出，这个判据不够准确：退出后的内核对象可因未关闭句柄继续存在。改为检查退出状态后，final4仍观察到两个conhost在Job清单为0的瞬间尚未退出，不能将本次失败全部解释为对象残留。最终监督器增加已记录成员的PID/创建时间核对与限时退出等待，不按历史PID额外终止进程。final5的88项测试全部通过，4项真实进程测试原件保存在 `runs/worker-tree-selftest-20261028-final5/`。依据 [Microsoft Terminating a Process](https://learn.microsoft.com/en-us/windows/win32/procthread/terminating-a-process) 与 [psutil Process.wait](https://psutil.readthedocs.io/stable/#psutil.Process.wait)。旧失败日志保留。

08轮使用最终安装版本完整通过，用时39.715秒，基线1保留、自有0、未知0；源文件与安装副本清单及独立审计留在08目录。专用字节增加271323136字节，原生资源完全释放依然未获验证。
