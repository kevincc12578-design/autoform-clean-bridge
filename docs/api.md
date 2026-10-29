# AutoForm R13 Bridge API

更新日期：2026-10-29。本文描述当前新增接口；**本次指定抱箍场景已通过同版三轮完整业务链及非空工程持久化现场验收**。旧库的三轮通过记录仅是代码筛选依据，不能替代新库验收。

## 会话与返回结构

`Bridge(adapter, run_dir)` 是工程和业务操作的公共入口。导入 `af_r13_bridge.api` 不访问 UI；真实操作由显式 live 路径构造 `WindowsAdapter`。同一 Bridge 对象保存所有权，不能在另一个进程中凭文件名重建工程句柄。

结果结构为：`ok, operation, request_id, document_id, job_id, data, verification_scope, postconditions, evidence, error, elapsed_ms`。`ok=true` 只表示本次请求的后置条件满足；不能从单次点击、空工程生命周期或单项业务成功推导整链通过。具体 GUI/native job 信息保留在对应操作的 `data` 中，顶层 `job_id` 可为 null。

### 工程操作

| operation | 请求 | 语义 |
| --- | --- | --- |
| `attach` | `{}` | 记录不可变用户工程基线 |
| `list_designs` | `{}` | 读取工程清单，不授予所有权 |
| `create_design` | `{}` | 创建唯一新增空工程并返回 `document_id` |
| `save_design` | `document_id, path` | 保存至本轮目录；核对文件框路径、身份与文件。相对路径按本轮目录解析 |
| `close_design` | `document_id` | 关闭本会话自有且保存已核对的工程，确认页签消失 |
| `reopen_design` | `path` | 重开本会话保存并关闭的同一文件；哈希一致，返回新句柄 |
| `readback` | `document_id` | 空工程的明确实体计数；非空业务使用 `read_business` |
| `cleanup` | `{}` | 诊断保存并关闭自有工程；未知归属不接管 |
| `disconnect` | `{}` | 回收 Bridge worker，不等于关闭工程或退出 R13 |
| `lifecycle` | 可选 `filename` | 固定新建空工程的完整生命周期验收 |
| `capabilities` | `{}` | 能力声明；验证范围须查当次原始证据 |

### 业务操作

下列操作均要求当前自有 `document_id`。业务执行前后检查用户基线与工程清单，不能通过业务操作创建、切换或接管其他工程。输入文件建议使用绝对路径；首个验收对象为指定抱箍 STEP 与 S355MC、4 mm，路径参数可配置不代表任意输入已经验证。

| operation | 额外请求字段 | 操作及后置条件 |
| --- | --- | --- |
| `import_part` | `path` | 导入 STEP，核对本次输入及零件出现 |
| `extract_part` | 无 | 提取零件，确认提取步骤结束 |
| `set_direction` | 无 | 自动设置冲压方向，再从方向下拉执行反向；读回三轴角度 |
| `set_material` | `path`，可选 `thickness_mm=4.0` | 材料与厚度读回，等待编辑器及延迟提示结束 |
| `forming_check` | 无 | 弯曲零件成型检查，应用并读取本次有效结果 |
| `create_plan` | 无 | 创建空规划，按录像校准位置拖入 U 工序 |
| `assign_operation` | 无 | C-20 × 拉延区域1分配，核对目标格关系和图案 |
| `visit_die_face` | 无 | 进入模面编辑器并切换离开；不生成压料面或补充面 |
| `copy_blank` | 无 | 创建平板料片，复制录像预选的成型检查料片轮廓并确认提交 |
| `set_friction` | 无 | 明确选压机线上下文，再设置并读取库仑摩擦 |
| `configure_tools` | 无 | 核对 C-20 凸凹模自动模型、自动接触；单独核对显示眼睛状态 |
| `add_reference` | 无 | 添加参考、全部输入并提交；公共路线不执行参考反转 |
| `set_springback` | 无 | 唯一具名测量方案，约束条件设置为自由回弹 |
| `start_simulation` | `path`，可选 `timeout=300` | GUI Start生成模拟输入，保存到本轮全新 AFD，绑定本次路径/job及求解日志 |
| `read_business` | `material_stem`，可选 `thickness_mm=4.0, expected_angles` | 读取非空业务语义，返回稳定 `values` 与原始 `checks`；本操作本身不证明持久化 |
| `solve` | 可选 `timeout=900, output_name="solver", license_server` | 针对本会话已登记保存文件，在独立副本上原生检查并完整求解 |

`expected_angles` 使用实际执行反向时返回的 `angles_deg`，例如键为 `X`、`Y'`、`Z''` 的对象。不得用历史角度硬填为本次观察。`start_simulation.path` 必须是本轮目录内不存在的绝对 AFD 路径；内部保存事务核对后才登记到新生命周期。

`read_business.data.values` 包含零件、材料、厚度、角度、工序关系、料片提交、自动工具、摩擦、参考输入、测量方案及自由回弹设置。坐标、截图路径、时间戳及控件临时身份只保留在 `checks`/证据中。无法直接观察的参考方向、料片来源或几何拓扑保持 null/未验证；工具显示不能证明启用或几何有效。

## Python 与同进程 batch

Python 使用 `bridge.call(operation, request)`。命令行单项工程/业务调用不能跨进程继承所有权，因此应使用 `batch --live --request`，或由 `chain` 编排。

```json
{
  "out": "D:/ai库/autoform-clean-bridge/runs/business-batch-new",
  "operations": [
    {"operation": "attach"},
    {"operation": "create_design", "as": "work"},
    {"operation": "import_part", "request": {"document_id": "@work", "path": "C:/Users/Administrator/Desktop/八角管抱箍/抱箍.STEP"}},
    {"operation": "extract_part", "request": {"document_id": "@work"}},
    {"operation": "save_design", "request": {"document_id": "@work", "path": "part-check.afd"}},
    {"operation": "close_design", "request": {"document_id": "@work"}}
  ]
}
```

`@alias` 和 `@last` 指向同一会话产生的当前句柄。batch 在首个失败后停止后续操作，最后清理并断开；禁止嵌套 `lifecycle` 或手动 `disconnect`。batch 的单项成功不自动成为整链验收，也不会自行比较保存重开前后的业务语义。

## 抱箍 workflow 与 CLI

`chain` 已实现，工作流只调用公共 Bridge。先完成一轮，再使用冻结源码和安装副本连续三轮验证。源码和安装包不一致会拒绝运行；输出目录必须全新。

```powershell
& ./.venv/Scripts/python.exe -I -m af_r13_bridge chain --workflow baogu --runs 1 --live --out D:/ai库/autoform-clean-bridge/runs/baogu-single-new
& ./.venv/Scripts/python.exe -I -m af_r13_bridge chain --workflow baogu --runs 3 --live --out D:/ai库/autoform-clean-bridge/runs/baogu-three-new
```

可选 `--part <STEP绝对路径>`、`--material <MTB绝对路径>`。默认附着唯一 R13；无 R13 时使用 `--launch`，需要应用界面当前所选许可时另加 `--allow-selected-license`，可用 `--license-server 2375@127.0.0.1` 设置子进程许可地址。不得以结束附着 R13 代替正常关闭。

`--runs` 支持1至3。研发可使用 `--runs 1 --until <业务operation>`，例如 `--until set_material`；它会执行必要收尾，但 `e2e_ok=false`，不能当作完整通过。当前 CLI 对这种未完成整链的运行返回非零退出码，局部结果看 `development_ok` 和阶段原件。

完整顺序为：新空工程与空计数 → 15项业务及求解 → `read_business` → 保存 → 关闭原页签 → 重开同一文件 → 再读业务并比较稳定值 → 再次保存关闭 → 自有页签0、未知新增0、用户基线保留、worker退出。失败的必需后续阶段标为 `skipped_dependency`。

每轮写入 `workflow.json`，整链写入 `chain.json`、源码/安装清单、隔离报告及各阶段原始请求、响应、截图、日志。完整成功还要求所有必需阶段通过、每轮少于20分钟、当前求解Springback完成及全部原生END0；三轮成功才标记 `three_consecutive_verified=true`。

## 原生求解作业与取消

`af_r13_bridge.solver` 另外提供 Python 函数：

```python
from af_r13_bridge.solver import status_job, cancel_job

state = status_job(r"D:\ai库\autoform-clean-bridge\runs\某轮\solver\full-solve")
request = cancel_job(state["work_dir"], state["job_id"])
```

`work_dir` 应采用实际 `native-job.json` 或求解结果返回的目录，不猜目录名。以上是原生作业函数，当前不属于 `Bridge.call` 或独立 CLI 子命令。`status_job` 读取自有作业记录；`cancel_job` 验证 `job_id` 后写协作取消请求，返回 `cancel_requested=true` 不等于作业已经退出，必须继续读取终态。

取消只针对本库创建的原生求解进程树，**不取消 GUI Start 提交的应用内作业**。停止 UI worker、`disconnect` 或结束原生副本计算也不表示 GUI 作业已取消。

原生求解在副本上进行，记录 `file_roles` 区分 GUI 原件、输入检查副本、完整求解副本；原生副本的结果不自动回写 GUI 原件。日志检查要求本次 C-20、材料、正网格、凸凹模初始化，及有序的Springback收敛→模拟成功结束→全部可解析END0。完成求解不等于已读全场数值或产品公差合格。

## 失败与恢复

常见错误包括 `live_opt_in_required`、`invalid_document_handle`、`not_owned`、`save_required`、`file_changed`、`business_postcondition_failed`、`workflow_stage_failed`、`persistence_mismatch`、`installation_stale`。业务失败将阻止继续创建新工作，但仍尝试本轮自有工程的诊断保存关闭；未知模态或归属不明时保留原件和失败证据。

`recover_recorded_run(previous_run, output_dir)` 是窄范围诊断收尾：核对原 R13 进程/窗口、有效未关闭账本和相同 RuntimeId；不能接管未知工程，也不能把旧失败改成验收通过。

## 2026-10-29 指定业务验收

同一冻结版本已连续完成三个独立空工程的26阶段整链，144项自动测试通过。GUI与原生日志、同文件业务持久化和两次关闭的原件均已独立复核；详见[状态](status-2026-10-29.md)和[证据摘要](evidence/business-acceptance-2026-10-29.json)。`capabilities`是能力声明，静态`e2e_ok=false`不冒充一次运行结果；实际结果以当轮`chain.json`为准。原生取消仅有离线进程测试，不因正常求解链通过而宣称真实R13取消已验。
