# af_r13_bridge

AutoForm R13 Bridge：Python/JSON API、同进程batch、抱箍业务工作流、原生求解，以及自有工程保存、关闭、同文件重开核对和最终关闭。

2026-10-29用户允许筛选复用旧库已验证的业务代码。本包直接维护被选取的业务函数，沿用新库会话、所有权、worker和证据模块；不导入旧包，不调用旧CLI/chain/server，不依赖旧录制器。来源和修改见 `docs/provenance-*.json` 与[复用边界](docs/复用边界-2026-10-29.md)。

本次验收：**144项测试通过，同一版本连续三个独立空工程完整通过**，每轮26阶段，耗时 280.155 / 282.801 / 284.173秒，CLI退出0；同文件持久化核对、两次关闭及自有页签归零全部通过。见[证据摘要](docs/evidence/business-acceptance-2026-10-29.json)。

实现和真实验收分开记录：[功能树](docs/功能树.md)、[API](docs/api.md)、[当次状态](docs/status-2026-10-29.md)。旧三连和第08轮空工程验证不替代新业务验证。

## 已接入功能

- STEP导入/提取、自动冲压方向后反向、角度读回、S355MC材料与4 mm厚度、弯曲成型检查。
- 创建空规划并拖入U工序、C-20分配、进入并离开模面编辑器、从成型检查复制料片轮廓。
- 压机线库仑摩擦、凸凹模自动模型和接触及显示、添加不反转参考、唯一自由回弹测量方案。
- GUI Start保存事务、当前job/完整路径/原生日志绑定、原生输入检查和完整求解副本、全部END0与有序Springback判据。
- 非空工程业务读回，原页签保存关闭后重开同一文件，比较业务语义，再保存关闭；失败时保留本轮内容并收尾。

## 运行

在本项目PowerShell执行，使用独立环境中的普通wheel安装；输出目录必须是新目录。

```powershell
& .\.venv\Scripts\python.exe -I -m af_r13_bridge capabilities
& .\.venv\Scripts\python.exe -I -m af_r13_bridge discover
& .\.venv\Scripts\python.exe -I -m af_r13_bridge chain --workflow baogu --runs 3 --live --out D:\ai库\autoform-clean-bridge\runs\new-acceptance
```

默认输入为桌面八角管抱箍的 `抱箍.STEP` 和R13材料库中的 `S355MC_4936_4.00.mtb`，可用 `--part/--material` 指定文件。其他输入仍需单独验证。无运行中的R13时可加 `--launch --license-server 2375@127.0.0.1 --allow-selected-license`；附着不会更改许可证。多个R13会话或未知模态会停止并留证。

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
