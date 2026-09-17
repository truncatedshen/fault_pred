# Agent少数服从多数投票正确率分析（孔多塞陪审团定理）
> 问题描述：设有 $N$ 个独立同分布的Agent，单个Agent做出正确决策的概率为 $p$，错误概率为 $1-p$；采用少数服从多数投票规则进行集体决策。分析集体决策正确率，分奇数、偶数两种总Agent数量；偶数下进一步区分三种平局处置策略。

## 前提假设
1. $N$ 个Agent决策**相互独立**；
2. 每个Agent独立决策，正确概率恒等于 $p$；
3. $X$：随机变量，表示N个Agent里面做出正确决策的数量，则 $X\sim \boldsymbol{B(N,p)}$（二项分布）
$$
P(X=i)=\binom{N}{i}\,p^{i}(1-p)^{N-i},\quad i=0,1,\dots,N
$$

## 一、奇数 $N$ 的情形：$N=2k+1,\ k\in \mathbb{N}$
### 建模
总Agent数目为奇数，不可能出现平分票数。
集体结果正确，当且仅当**正确票数严格大于半数**，也就是至少 $k+1$ 个Agent回答正确。

集体正确率：
$$
P_{\mathrm{odd}}(N,p)=\sum_{i=k+1}^{2k+1}\binom{N}{i}\,p^{i}(1-p)^{N-i}
$$

### 性质简要证明（孔多塞陪审团定理）
1. **当 $p>0.5$**：$\lim_{N\to\infty}P_{\mathrm{odd}}(N,p)=1$
直观理解：因为个体倾向正确；独立采样下大数定律保证，正确票数占比依概率收敛到 $p>0.5$；只要N足够大，几乎必然多数Agent选正确答案，集体正确率趋近1。

2. **当 $p=0.5$**：$P_{\mathrm{odd}}(N,0.5)=0.5$

**证明**：
$$
P(X\ge k+1\mid p=0.5)=\sum_{i=k+1}^{2k+1}\binom{2k+1}{i}\left(\frac12\right)^{2k+1}
$$
二项分布 $B(2k+1,0.5)$ 对称；$P(X\ge k+1)=P(X\le k)$；又 $P(X\ge k+1)+P(X\le k)=1$；
于是 $P(X\ge k+1)=0.5$。

3. **当 $p<0.5$**：$\lim_{N\to\infty}P_{\mathrm{odd}}(N,p)=0$
个体更容易犯错；N越大，多数Agent倾向错误答案；集体正确率趋于0。

> 小结奇数N：
> - $p>0.5$：增加Agent数目可以提升集体正确率；
> - $p=0.5$：增加Agent数目完全没有效果；
> - $p<0.5$：增加Agent数目反而让结果变得更差。

## 二、偶数 $N$ 的情形：$N=2k,\ k\in\mathbb{N}$
此时有可能出现恰好 $k$ 票正确，$k$ 票错误，也就是**平局**。平局没有天然多数，必须额外定义平局处置策略。下面分析3种工程上常见策略：

### 方案A：平局时随机猜测（以50%概率输出正确答案）
当 $X>k$，集体正确；当 $X<k$，集体错误；当 $X=k$（平局），以 $1/2$ 的概率猜对。

$$
P_{A}(N,p)=\sum_{i=k+1}^{2k}\binom{N}{i}p^{i}(1-p)^{N-i}\;+\;\frac12\binom{N}{k}p^{k}(1-p)^{k}
$$

**特例证明：N=2（k=1）**
$$
\begin{aligned}
P_A(2,p)
&=\binom{2}{2}p^{2}+\frac12\cdot\binom{2}{1}p(1-p)\\
&=p^{2}+\frac12\cdot 2p(1-p)\\
&=p^{2}+p(1-p)\\
&=p
\end{aligned}
$$
> **重要结论：2个Agent投票+平局随机猜，集体正确率等于单个Agent正确率，投票没有收益。**

**极限性质证明（$N=2k\to\infty$）**
- $p>0.5$：平局概率 $\binom{2k}{k}p^{k}(1-p)^{k}\xrightarrow{k\to\infty}0$；此时 $P_A(N,p)$ 和奇数情形一样收敛于 $1$；同等算力下，奇数N的收敛速度略优于偶数A，因为偶数会引入平局随机噪声；
- $p=0.5$：
$$
\begin{aligned}
P_A(2k,0.5)&=\sum_{i=k+1}^{2k}\binom{2k}{i}\left(\frac12\right)^{2k}+\frac12\binom{2k}{k}\left(\frac12\right)^{2k}\\
&=\left(\frac12-\frac12\binom{2k}{k}\frac1{2^{2k}}\right)+\frac12\binom{2k}{k}\frac1{2^{2k}}\\
&=\frac12
\end{aligned}
$$
即无论k多大，方案A在p=0.5时恒等于0.5；
- $p<0.5$：$P_A(N,p)\to0$。

### 方案B：平局直接弃权（拒绝输出，丢弃该样本；仅统计非平局样本上的条件正确率）
只在非平局的样本上评估正确率。平局样本直接放弃回答。

先计算非平局发生概率：$1-\binom{N}{k}p^{k}(1-p)^{k}$
$$
P_{B}(N,p)=\frac{\displaystyle\sum_{i=k+1}^{2k}\binom{N}{i}\,p^{i}(1-p)^{N-i}}
{\displaystyle 1-\binom{N}{k}p^{k}(1-p)^{k}}
$$

性质：
1. $p>0.5$，$N\to\infty$，平局概率趋于0，$P_{B}\to1$；但是系统整体回答率逐步下降（一部分问题弃权不输出）；
2. $p=0.5$：对称性可以证明条件正确率依然等于0.5，只是大量样本直接弃权；
3. $p<0.5$，$N\to\infty$，$P_{B}\to0$。

### 方案C：平局直接判定集体决策错误（保守策略，平局一律算错）
平局直接视为集体决策失败。
$$
P_{C}(N,p)=\sum_{i=k+1}^{2k}\binom{N}{i}\,p^{i}(1-p)^{N-i}
$$

性质：
$P_{C}(N,p) \le P_{A}(N,p)$，因为方案C直接丢掉平局那一半猜对的机会。
1. $p>0.5$：仍然收敛到1，但是收敛速度最慢；
2. $p=0.5$：可以证明 $P_{C}(2k,0.5)<0.5$；增加Agent数目整体正确率会低于0.5；
3. $p<0.5$：随N增大迅速趋向0；**该策略不推荐用于多Agent投票系统。**

## 三、数值示例对比
取 $p=0.6$

| N | 设置 | 集体正确率 |
|---|---|---|
| 3（奇数,k=1） | $P_{\mathrm{odd}}$ | 0.648 |
| 4（偶数,k=2） | $P_A$（平局随机猜） | 0.68256 |
| 4（偶数,k=2） | $P_B$（平局弃权，条件正确率） | 0.7333 |
| 4（偶数,k=2） | $P_C$（平局直接判错） | 0.5184 |
| 2（偶数,k=1） | $P_A$（平局随机猜） | 0.6（等价单agent） |

> 注意：N=4‑A虽然数值高于N=3，但这只是特定p下的结果；从渐近角度，同等算力预算优先选择奇数N。

## 四、现实工程启示（LLM Agent多智能体投票）
1. **理论前提是独立！现实LLM Agent之间决策高度相关，会共享同类错误；此时陪审团定理给出的正确率上界无法达到，投票收益下降甚至失效。这是最容易被忽略的一点。**
2. 如果做投票集成，优先选取**奇数个Agent**，直接消除平局问题；
3. 如果算力限制只能偶数个Agent：
    - 方案A平局随机：简单，但引入额外随机噪声；
    - 方案B平局弃权：适合高可靠场景；遇到分歧直接拒绝输出，可以降低幻觉风险；很多对齐后的推理系统采用该思路；
    - 方案C平局一律判错不推荐，会显著降低整体正确率；
4. N=2的双Agent投票没有理论收益（方案A），不要指望两个大模型互相投票就自动提升答案质量。

## 五、附录：Python计算与绘图代码
```python
import math
import numpy as np
import matplotlib.pyplot as plt

def prob_odd(N, p):
    k = (N - 1)//2
    total = 0.0
    for i in range(k+1, N+1):
        c = math.comb(N, i)
        total += c * (p**i) * ((1-p)**(N-i))
    return total

def prob_A(N, p):
    if N % 2 ==1:
        return prob_odd(N,p)
    k = N//2
    sum_right = 0.0
    for i in range(k+1, N+1):
        c = math.comb(N,i)
        sum_right += c * (p**i)*((1-p)**(N-i))
    tie = math.comb(N,k)*(p**k)*((1-p)**k)
    return sum_right + 0.5 * tie

def prob_B(N, p):
    if N %2 ==1:
        return prob_odd(N,p)
    k = N//2
    sum_right =0.0
    for i in range(k+1,N+1):
        c = math.comb(N,i)
        sum_right += c*(p**i)*((1-p)**(N-i))
    tie = math.comb(N,k)*(p**k)*((1-p)**k)
    denom = 1.0 - tie
    if abs(denom)<1e-12:
        return np.nan
    return sum_right / denom

def prob_C(N,p):
    if N%2 ==1:
        return prob_odd(N,p)
    k=N//2
    sum_right=0.0
    for i in range(k+1,N+1):
        c=math.comb(N,i)
        sum_right += c*(p**i)*((1-p)**(N-i))
    return sum_right

if __name__=="__main__":
    p=0.6
    ns = list(range(2,13))
    ya=[prob_A(n,p) for n in ns]
    yb=[prob_B(n,p) for n in ns]
    yc=[prob_C(n,p) for n in ns]

    plt.figure(figsize=(10,6))
    plt.plot(ns,ya,label="Strategy A: tie random guess",marker='o')
    plt.plot(ns,yb,label="Strategy B: tie abstain(cond acc)",marker='s')
    plt.plot(ns,yc,label="Strategy C: tie mark wrong",marker='^')
    plt.axhline(y=p,ls="--",color="gray",label="single agent p=0.6")
    plt.xlabel("Number of agent N")
    plt.ylabel("Collective accuracy")
    plt.title(f"Voting accuracy under different tie handling, p={p}")
    plt.legend()
    plt.grid(alpha=0.3)
    plt.show()
