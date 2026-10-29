# af_r13_bridge

AutoForm R13 Bridge：Python/JSON API、同进程batch、抱箍业务工作流、原生求解，以及自有工程保存、关闭、同文件重开核对和最终关闭。

2026-10-29用户允许筛选复用旧库已验证的业务代码。本包直接维护被选取的业务函数，沿用新库会话、所有权、worker和证据模块；不导入旧包，不调用旧CLI/chain/server，不依赖旧录制器。来源和修改见 `docs/provenance-*.json` 与[复用边界](docs/复用边界-2026-10-29.md)。

当前增量合并版验收：**178项测试通过，同一版本连续三个独立空工程完整通过**，每轮26阶段，耗时 324.099 / 322.679 / 321.846秒，CLI退出0；同文件持久化核对、两次关闭及自有页签归零全部通过，独立审计3872项通过。见[当前证据摘要](docs/evidence/increment-merge-acceptance-2026-10-29.json)。

实现和真实验收分开记录：[功能树](docs/功能树.md)、[API](docs/api.md)、[当前合并与验收记录](docs/增量合并记录-2026-10-29.md)。旧三连和第08轮空工程验证不替代当前增量验证。

## 抱箍工序说明

本项目将项目维护者于2026-10-28提供并演示的抱箍工序路线自动化。当前已验收案例为指定抱箍STEP、S355MC材料、4 mm板厚及AutoForm R13；其他零件与材料需要另行验证。完整动作、参数来源和验证范围见[抱箍工序说明](docs/抱箍工序说明.md)。

1. 导入STEP并提取零件；冲压方向先自动设置，再选择方向下拉中的“反向”。
2. 加载S355MC材料并确认4 mm板厚；选择“弯曲零件”进行成型检查，点击“应用”并等待完成。
3. 新建空规划，拖入演示中的U图标工序，规划内显示为C-20，再将“拉延区域1”分配给该工序。进入并离开模面编辑器，本路线忽略压料面，不生成压料面或补充面。
4. 创建平板料片并复制成型检查的料片轮廓；在压机线上选择库仑摩擦；显示凸模、凹模并核对自动模型及自动接触状态。
5. 添加参考模型，依次执行“定义→全部输入→确定”，**参考模型不反转**；创建测量方案并设置自由回弹。
6. 启动模拟并保存到全新路径，核对本次求解日志；另对保存文件的独立副本执行原生输入检查与完整求解。最后保存关闭原工程、重开同一文件核对业务值、再次保存关闭。

U图标与C-20描述同一个规划工序。验收中的“26阶段”包含会话、业务动作及保存重开复核，不代表26道制造工序。求解完成与Springback成功不等于产品回弹公差合格。

## 已接入功能

- STEP导入/提取、自动冲压方向后反向、角度读回、S355MC材料与4 mm厚度、弯曲成型检查。
- 创建空规划并拖入U工序、C-20分配、进入并离开模面编辑器、从成型检查复制料片轮廓。
- 压机线库仑摩擦、凸凹模自动模型和接触及显示、添加不反转参考、唯一自由回弹测量方案。
- GUI Start保存事务、当前job/完整路径/原生日志绑定、原生输入检查和完整求解副本、全部END0与有序Springback判据。
- 非空工程业务读回，原页签保存关闭后重开同一文件，比较业务语义，再保存关闭；失败时保留本轮内容并收尾。

## 运行

### 窗口尺寸与复现条件

当前版本在现有会话入口固定主窗口客户区为 **1800×960**、屏幕客户区原点 **(30,50)**、窗口 **DPI=96**，并核对显示器可用工作区。导入零件前的最大化已移除。每次受控输入检查窗口状态、鼠标捕获和弹窗上下文；尺寸、位置或 DPI 改变时停止输入。合并范围及新证据见[增量合并记录](docs/增量合并记录-2026-10-29.md)。

原始操作演示记录的参考环境如下。这些是2026-10-28录像中的观测值，不能替代2026-10-29三轮验收中未保存的字段：

| 项目 | 原始演示记录 |
| --- | --- |
| 显示器区域 | 1920 × 1080像素，左上角`(0, 0)` |
| 可用工作区 | 1920 × 1040像素，左上角`(0, 0)` |
| AutoForm窗口DPI读数 | 96 |
| 主窗口外框 | 左/上/右/下为`[-8, -8, 1928, 1048]`，含边框，不是客户区尺寸 |
| Win32客户区尺寸 | 原始演示未单独记录，不能从外框直接当作已知值 |
| Windows设置中的缩放百分比 | 未单独记录；窗口DPI读数96不能无条件等同于系统缩放100% |

合并前的历史三轮验收均记录了相同的主窗口外框，但没有持久化DPI、显示器区域和客户区尺寸。来源与原件哈希见[历史窗口参考证据](docs/evidence/window-reference-2026-10-29.json)。窗口DPI的含义参见[微软GetDpiForWindow说明](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getdpiforwindow)。

上述最大化尺寸属于合并前的历史参考，不能替代当前固定窗口的验收。固定窗口由现有 `WindowsAdapter` 准备，同一个 profile 传递给生命周期和业务 worker；没有引入第二套会话、所有权或保存关闭实现。窗口尺寸一致仍不能替代界面布局与完整流程验证，其他显示环境需单独验证。

运行期间应保持窗口位置、大小、显示缩放及所在显示器稳定。

### 执行命令

在本项目PowerShell执行，使用独立环境中的普通wheel安装；输出目录必须是新目录。

```powershell
& .\.venv\Scripts\python.exe -I -m af_r13_bridge capabilities
& .\.venv\Scripts\python.exe -I -m af_r13_bridge catalog --query "springback"
& .\.venv\Scripts\python.exe -I -m af_r13_bridge discover
& .\.venv\Scripts\python.exe -I -m af_r13_bridge chain --workflow baogu --runs 3 --live --out D:\ai库\autoform-clean-bridge\runs\new-acceptance
```

默认输入为桌面八角管抱箍的 `抱箍.STEP` 和R13材料库中的 `S355MC_4936_4.00.mtb`，可用 `--part/--material` 指定文件。其他输入仍需单独验证。无运行中的R13时可加 `--launch --license-server 2375@127.0.0.1 --allow-selected-license`；附着不会更改许可证。多个R13会话或未知模态会停止并留证。

`catalog` 是无需 UI 的本机官方帮助索引检索；当前索引含 21 类、257 项，所有条目的可调用状态仍未验证。原有 16 个业务操作入口保留，目录检索不会把索引条目自动变成可执行功能。

研发可用 `--runs 1 --until set_material` 等截止点；会收尾，但保持 `e2e_ok=false`，不能当完整验收。所有必需阶段通过且每轮少于20分钟、关闭收尾成功时完整chain才退出0。单项操作使用同一Python Bridge实例或 `batch --live --request request.json`，不跨CLI进程传递工程所有权。

原空工程入口继续保留：

```powershell
& .\.venv\Scripts\python.exe -I -m af_r13_bridge lifecycle --live --out D:\ai库\autoform-clean-bridge\runs\new-empty-check
```

## 验证边界

每轮保存 `workflow.json`，父目录保存 `chain.json` 和源码/安装包manifest；原始worker请求、响应、截图、日志、哈希与资源记录保留在独立run目录。三连必须由同一冻结源码与同一CLI连续执行，不拼接研发轮次。

路径由文件框事务、同页签RuntimeId、标题及文件哈希证明；真实dirty值仍为null。重开后读取业务配置和工序关系，未解码完整AFD结果场，也未判定产品回弹公差。原生检查/求解使用独立副本，不冒充已回写GUI原件。GUI求解完成由本次job和原生日志绑定证明。

`disconnect`仅回收Bridge worker，不关闭工程或退出R13。原生作业支持按job_id请求取消并读取终态；终止UI worker不等于取消已提交的GUI求解，GUI取消能力未实现。内存按检查点记录，不宣称全部原生缓存释放。

## 安装与测试

依赖由 `requirements.lock` 锁定，来源见 `dependency-provenance.json`。源码修改后重新安装；禁止editable、旧路径注入和目录联接。

```powershell
& .\.venv\Scripts\python.exe -I -m pip install --require-hashes -r requirements.lock
& .\.venv\Scripts\python.exe -I -m pip install --no-deps --no-build-isolation .
& .\.venv\Scripts\python.exe -I -m unittest discover -s tests -v
```

默认测试不操作AutoForm，部分监督测试使用真实Python子进程。离线通过不等于业务现场通过。`runs/`、AFD、原始录像与本机环境保留本地，不上传仓库。
