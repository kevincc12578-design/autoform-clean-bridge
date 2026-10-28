# af_r13_bridge

独立重建的 AutoForm R13 Bridge。运行代码、测试、环境与旧库分离；旧工程仅作只读资料，不是依赖。

2026-10-28：P0 隔离与基础接口已完成；P1 **本机中文 R13 空工程生命周期**已真实通过。最新 08 轮用时 39.715 秒：保存 → 关闭确认 → 同文件重开 → 空工程计数核对 → 再保存关闭 → 自有页签归零，原有的 1 个基线页签保持打开。抱箍、材料、求解、数值场和录制尚未实现。

## 功能树

完整分支、状态、代码入口与后续业务路线见 **[功能树](docs/功能树.md)**。

```text
af_r13_bridge
├── 公共入口：Python API / JSON CLI / 同进程批处理       已实现
├── 会话与工程所有权：发现、启动、附着、基线、工程身份   已实现
├── 保存 → 关闭 → 同文件重开核对 → 再次关闭             空工程已实测
├── Windows适配、单写锁、worker超时与退出确认            已实现
├── 隔离审计、原始证据、资源记录、诊断收尾               已实现，范围见功能树
├── 零件、材料、规划、模具、摩擦、参考、测量方案         待实现
├── 原生检查、求解、Springback、结果读取                 待实现
└── 抱箍整链、连续三轮、独立录制、MCP/HTTP               待实现
```

## 入口

在本目录 PowerShell 中执行。所有运行使用独立环境和已安装 wheel，不依赖当前目录的 Python 搜索路径。

```powershell
& .\.venv\Scripts\python.exe -I -m af_r13_bridge capabilities
& .\.venv\Scripts\python.exe -I -m af_r13_bridge audit --out D:\ai库\autoform-clean-bridge\runs\audit-new
& .\.venv\Scripts\python.exe -I -m af_r13_bridge discover
& .\.venv\Scripts\python.exe -I -m unittest discover -s tests -v
```

真实生命周期需要显式 `--live`，输出目录必须不存在。默认附着唯一 R13，会记录并保护已开工程。

```powershell
& .\.venv\Scripts\python.exe -I -m af_r13_bridge lifecycle --live --out D:\ai库\autoform-clean-bridge\runs\p1-new --filename p1-new.afd
```

无 R13 时可加 `--launch`；本机进程级许可证地址为 `--license-server 2375@127.0.0.1`。如需新启动时应用界面中已选许可证方案，加 `--allow-selected-license`；附着会话不会操作许可证选择。系统环境不修改，未知模态停止留证。

JSON 入口：`call lifecycle --live --request request.json`，请求可为 `{"out":"D:/ai库/autoform-clean-bridge/runs/p1-json-new"}`。Python 公共接口在 `af_r13_bridge.api.Bridge`；单项调用顺序在同一个 Bridge 对象中保持身份。命令行单项 create/save/close 必须使用同进程 `batch`，详见 `docs/api.md`。

## 验收与限制

最终代码的 88 项自动测试通过（其中 4 项使用真实 Windows 进程、不操作 AutoForm），日志为 `runs/verification-20261028-01/unit-tests-final5.log`。

- 最新成功证据：`runs/lifecycle-20261028-08/result.json`、`acceptance-audit.json`、`lifecycle-events.jsonl`、各 worker 的请求/响应/隔离报告与截图。`e2e_ok=false`，不代表求解通过。
- 03、04、07 是较早版本的成功记录；01、02、05、06 的失败记录原样保留。它们不是同版整链三连验收。
- 路径证据来自受控文件框完整路径、同一页签 RuntimeId、文档标题、目标文件及同路径重开。未实现 AFD SDK 直接路径查询。真实 dirty 标志未暴露，保持 `null`。
- 当前空工程读回：零件、模具、坐标系、曲线和点的明确显示计数；不以控件缺失代替 0。非空工程不在本次验收范围内。
- 运行时勿人工切换/另存工程；跨进程写入锁约束本 Bridge，同名文件外部另存等人工并发不在已验证范围。
- 内存按生命周期检查点记录，成功轮结束工作集下降，但 private bytes 高于轮次开始；不能据此宣称缓存、全部原生资源释放或长期无增长。
- R13 保留运行；`disconnect` 只停止 Bridge worker，绝不等同工程关闭，也不终止 R13。

## 重新安装

`requirements.lock` 对本机 Windows x64 / Python 3.14 的 wheel 精确锁定 SHA256。`dependency-provenance.json` 记录来源。重新建环境后先安装依赖，再普通安装本包：

```powershell
& D:\Python314\python.exe -I -m venv .venv
& .\.venv\Scripts\python.exe -I -m pip install --require-hashes -r requirements.lock
& .\.venv\Scripts\python.exe -I -m pip install --no-deps --no-build-isolation .
```

源码更改后需要重新安装 wheel；禁止 editable 安装、旧代码复制、旧 CLI 包装和旧目录路径注入。项目规则见 `AGENTS.md`；下一步见 `docs/status-2026-10-28.md`。
