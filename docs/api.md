# P1 公共 API

`Bridge(adapter, run_dir)` 是所有操作共用入口；`WindowsAdapter` 只由显式 live 路径构造。导入包/API 不会访问 UI。

返回字段：`ok, operation, request_id, document_id, job_id, data, verification_scope, postconditions, evidence, error, elapsed_ms`。

| operation | 请求 | 范围 |
| --- | --- | --- |
| attach | `{}` | 不可变用户工程基线 |
| list_designs | `{}` | 只读当前工程；列表不授予所有权 |
| create_design | `{}` | 新空工程、唯一新增身份 |
| save_design | `document_id, path` | 本轮路径、文件框/身份/文件核对 |
| close_design | `document_id` | 只关自有且保存已核对的工程，确认页签消失 |
| reopen_design | `path` | 必须是本轮保存且已关闭的同一文件；新句柄 |
| readback | `document_id` | 本场景空工程的明确实体计数 |
| cleanup | `{}` | 尽力诊断保存并关闭自有页签；未知归属不关 |
| disconnect | `{}` | 回收本 Bridge worker，不操作工程或退出 R13 |
| lifecycle | `filename` 可选 | 完整 P1 顺序与硬门禁 |
| capabilities | `{}` | 声明能力及验证范围 |

单项可用 Python `bridge.call(operation, request)`。CLI 不允许跨进程单独 create/save，因为下一进程没有其所有权。批处理示例 JSON（必须通过 `batch --live --request`）：

```json
{
  "out": "D:/ai库/autoform-clean-bridge/runs/batch-new",
  "operations": [
    {"operation":"attach"},
    {"operation":"create_design", "as":"work"},
    {"operation":"readback", "request":{"document_id":"@work"}},
    {"operation":"save_design", "request":{"document_id":"@work", "path":"example.afd"}},
    {"operation":"close_design", "request":{"document_id":"@work"}}
  ]
}
```

batch 最后自动安全清理、断开；完整持久化验收请用 lifecycle。新输出目录必需，退出码 0 仅表示请求已核对通过；未实现业务返回 `not_implemented`，不是软件能力结论。

异常后 `recover_recorded_run(previous_run, output_dir)` 是单独的窄范围诊断收尾工具：仅限同一原 R13 进程/窗口、有效未关闭账本记录及相同 RuntimeId；遇未知新增、过期身份或基线变化拒绝接管。它绝不把原失败改成 lifecycle 通过。
