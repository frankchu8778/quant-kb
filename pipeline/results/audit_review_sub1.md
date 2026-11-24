# 抽樣審計：審計員判 critical / major、檢查員放行的條目

審計員 MiniMax-M3.1-Flash。每一條對照原文判斷：**真**（卡片確實錯，檢查員漏抓）、**誤報**（卡片沒錯）、**吹毛求疵**（不算錯但可改）。

「檢查員當時」是 DeepSeek 對同一處有沒有提過（minor 也算）。

## 1. nodate_unknown_08a217

- 嚴重度 / 類型：**critical** / 方法对象错误（審計員整張卡判 reject）
- 欄位：`key_formulas[Growth]`
- 卡片寫：Growth = OP_SD + QPT + NP_Q_YOY（综合成长因子，三个组成因子等权相加）
- 審計員說原文是：附录图表169明确写的是 Growth 由 OP_SD、QPT 和 NP_YOY（净利润增速TTM同比）等权相加；NP_Q_YOY（净利润增速单季度同比）是另一个独立因子，并未参与 Growth 合成。
- 引文：「OP_SD、QPT和NP_YOY因子等权相加」（附录）
- 引文在全文：**找不到**
- 檢查員當時：同欄位沒有提過
- 人工判定：＿誤報（Claude 對照原文：全文第 1976 行附錄表就是「OP_SD、QPT和NP_Q_YOY因子等权相加」，卡片照抄正確；審計員的引文把 NP_Q_YOY 改成 NP_YOY，全文找不到）＿（真 / 誤報 / 吹毛求疵）

## 2. nodate_unknown_08a217

- 嚴重度 / 類型：**major** / 引用不支持（審計員整張卡判 reject）
- 欄位：`method_steps[第9步]`
- 卡片寫：引文写作“OP\_SD、QPT和NP\_Q\_YOY因子等权相加”
- 審計員說原文是：原文附录中不存在该引文字串，原文为“OP_SD、QPT和NP_YOY因子等权相加”，即卡片把 NP_YOY 写成了 NP_Q_YOY，逐字比对不成立。
- 引文：「OP_SD、QPT和NP_YOY因子等权相加」（附录）
- 引文在全文：**找不到**
- 檢查員當時：同欄位沒有提過
- 人工判定：＿誤報（Claude 對照原文：全文第 1976 行附錄表就是「OP_SD、QPT和NP_Q_YOY因子等权相加」，卡片照抄正確；審計員的引文把 NP_Q_YOY 改成 NP_YOY，全文找不到）＿（真 / 誤報 / 吹毛求疵）
