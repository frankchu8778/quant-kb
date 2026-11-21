---
id: nodate_unknown_9dbd98
title: 基于混沌理论的股指期货噪声趋势交易(NTT)策略
broker: 广发证券
date: null
series: 股指期货专题系列报告
source_pdf: inbox/poc/另类交易策略系列之一：基于混沌理论的股指期货噪声趋势交易策略.pdf
fulltext: fulltext/nodate_unknown_9dbd98.md
report_type: 深度专题
value_level: high
reproducibility: medium
primary_family: trend-following/趋势跟踪与CTA
families:
- trend-following/趋势跟踪与CTA
- derivatives-strategy/衍生品策略
- machine-learning/机器学习
methods:
- chaos-fractal/混沌与分形
- noise-filtering/噪声过滤
- trend-signal/趋势信号
- index-futures/股指期货
- neural-networks/神经网络
data_needed:
- 股指期货高频行情数据（30秒频）
- 股指期货连续合约数据
keywords:
- 混沌与分形
- 混沌理论
- 分形
- Hurst
- chaos theory
- 噪声过滤
- 噪声交易
- 去噪
- noise
- 趋势信号
- 均线
- 突破
- trend signal
- 股指期货
- IF
- IC
- IH
- IM
- index futures
- 神经网络
- 深度学习
- LSTM
- GRU
- Transformer
- MLP
- 趋势跟踪与CTA
- CTA
- 期货趋势
- trend following
- 衍生品策略
- 期权
- 套利
- derivatives
- 机器学习
- 人工智能
- AI
- ML
- 粗糙熵
- 相空间重构
- Levenberg-Marquardt拟合
---

# 基于混沌理论的股指期货噪声趋势交易(NTT)策略

## 核心观点

基于混沌理论构建粗糙熵-噪声非线性方程，利用Levenberg-Marquardt算法拟合高频价格序列以提取噪声标准差占比(NTS)。当上午NTS较小且市场呈单边走势时，午后进行股指期货单向趋势建仓。实证显示该策略在2010-2011年样本区间内，年化累积收益率达86.6%（5倍杠杆），夏普比率2.173，显著优于无噪声过滤的普通趋势交易。

## 创新点

将价格序列分解为噪声（随机游走）与非噪声（人为决定/趋势）部分，利用混沌理论中的粗糙熵计算噪声占比。核心洞见在于：噪声较小时市场非平衡性强，趋势延续概率高；噪声较大时市场平衡，震荡概率高。通过NTS阈值过滤掉高噪声时期的交易信号，从而优化趋势跟踪策略的风险收益比。

> 方法论价值：high — 提出基于混沌理论的粗糙熵-噪声模型，通过非线性拟合提取高频数据噪声比例作为趋势过滤条件，方法具有创新性且实证效果显著优于普通趋势交易。

## 方法步骤

1. 1. 数据准备：获取股指期货当月连续合约的30秒高频价格数据。
2. 2. 相空间重构：对上午10:00-11:30的时间序列进行相空间重构，嵌入维n=180，时滞τ=1。
3. 3. 计算关联积分：基于重构的相空间，计算不同阈值ε下的关联积分D_n(ε)。
4. 4. 非线性拟合：使用Levenberg-Marquardt (LM) 算法对粗糙熵与噪声的非线性方程进行拟合，估算噪声标准差σ_noisy。
5. 5. 计算NTS指标：计算噪声标准差与原始数据标准差的比值 NTS = σ_noisy / σ_data。
6. 6. 趋势判定：通过多项式拟合求极值或简单价差法(dif)判定上午是否为单边走势。
7. 7. 信号生成：若上午NTS < 阈值(如10%)且满足单边走势条件，则在午后开盘建仓，收盘前平仓。

## 核心公式

- 噪声趋势指标(NTS)，表示时间序列中噪声所占的比例。

  $$ $NTS = \frac{\sigma_{nosy}}{\sigma_{data}}$ $$

- 递归图(RP)的定义，判断相空间中两点是否邻近。

  $$ $P ( i , j ) = H ( \varepsilon - \left\| \vec { y } _ { i } - \vec { y } _ { j } \right\| )$ $$

- 关联熵K与平均嵌入维<n>的近似关系。

  $$ $K \approx \ln \frac{<n>-1}{<n>-2}$ $$

- 噪声环境下粗糙熵的显式表达式，用于推导噪声标准差。

  $$ $K_{n o i s y}(\varepsilon) = -\frac{1}{\tau} g\left(\frac{\varepsilon}{2 \sigma}\right) \ln \varepsilon+K_{2}(\varepsilon)\left(1+\sqrt{\pi} \frac{\sqrt{\varepsilon^{2} / 3+2 \sigma^{2}}-\varepsilon / \sqrt{3}}{\varepsilon}\right)$ $$

- Levenberg-Marquardt算法的迭代步长更新公式。

  $$ $\delta _ { _ { k } } = ( \boldsymbol { J } _ { _ { k } } ^ { T } \boldsymbol { J } _ { _ { k } } + \lambda _ { _ { k } } \boldsymbol { I } ) ^ { - 1 } \boldsymbol { J } _ { _ { k } } ^ { T } \boldsymbol { \varepsilon } _ { _ { k }}$ $$

## 关键结果

| 指标 | 数值 | 说明 |
|---|---|---|
| 年化累积收益率 | 86.6% | 5倍杠杆，扫描dif阈值0.011-0.017平台结构 |
| 夏普比率 | 2.173 | dif=0.014时，NTT模型 |
| 成功率 | 70% | 扫描dif阈值0.011-0.017平台结构 |
| 夏普比率 | 0.84 | 极值判别法，NTT模型 |

## 局限与注意

- 样本区间较短（2010.4.16-2011.4.15，仅一年），可能过拟合。
- 未计入交易成本，实际收益可能低于报告数据。
- 策略依赖高频数据（30秒），对数据获取和处理能力有要求。
- 仅适用于股指期货，且主要基于A股交易时段特征（上下午间隔短）。
- [推断] 5倍杠杆放大了收益也放大了风险，实盘需严格控制仓位。

## 可复现度：medium

核心算法（相空间重构、LM拟合）有明确数学公式，但非线性拟合的参数选择（如嵌入维、阈值ε范围）对结果敏感，且需要高频数据支持。

## 原文依据

- NTS指标定义及含义：「NTS = \frac{\sigma_{nosy}}{\sigma_{data}} \in [0, 1] 表征了这组时间序列中噪声所占的比例。」
- 交易策略逻辑：「当上午噪声较小,即市场效率较低、人为操作因素较强,并且市场形态呈现单边走势时,我们选择在午后对股指期货进行单向的趋势性建仓,并于收盘前平仓。」
- 实证结果对比：「经过与无噪声条件的普通趋势交易对比,我们发现经过加入噪声阈值的判定,可以对普通趋势交易进行有效优化,大为改善投资效果。」
- 阈值平台结构：「通过改变上午的价差阈值,发现该阈值在 0.011-0.017 之间时收益率出现了一个较好的平台结构,有利于减小该策略的非系统性风险。」
