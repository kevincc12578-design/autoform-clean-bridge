# STEP导入确认分支修复

错误：`runs/business-dev-20261029-01` 的 `import_part` 返回 `IMPORT_CONFIRMATION_NOT_FOUND`。新生命周期已保存关闭本轮自有工程；不能将失败结果改成通过。

现场 `431541613112100-import_part.json/png` 同时记录了启用的主窗口、`1 零件(s)`、实体 `抱箍-基体-法兰1` 和启用的“提取”入口。截图中无导入确认框，零件已经出现。源STEP的PRODUCT名称解码为“抱箍”，MANIFOLD_SOLID_BREP名称为“基体-法兰1”。

先查资料：Qt官方[QDialog文档](https://doc.qt.io/qt-6/qdialog.html)说明 `open()` 的异步性质，故文件框退出与确认框存在/消失必须独立观察。没有找到公开的AutoForm权威文档保证每次新空工程导入必弹“替换通用零件”确认。STEP名称解码依据[ISO 10303-21公开文本](https://www.steptools.com/stds/step/IS_final_p21e3.html)及[STEP Tools读写说明](https://www.steptools.com/docs/roselib/read_write.html)的X2/X4字符串规则。

比较方案：继续延长等待没有现场依据；直接忽略确认会失去导入成功判据；采用实际导入状态与已识别确认两种分支，保留真正后置条件。

最小修复：文件框路径提交成功后，如出现可识别导入确认则点击并继续观察；如不出现确认，必须读到唯一1个零件、与当前输入STEP产品/实体名称一致的行、唯一启用的提取入口、主窗口启用、无其他窗口，并稳定至少0.4秒，才算导入通过。字段缺失、错误实体、缺少提取入口或残留模态均不能通过。

名称解析限本次单产品单实体STEP，不是通用STEP几何解析，也不证明拓扑或网格有效。几何有效性仍由后续本轮原生检查验证。本修复需要新的live运行；保留原失败证据。
