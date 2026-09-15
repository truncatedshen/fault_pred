# 设备故障预测 · test_mcp_skill 工作空间报告

通过 fault-prediction MCP 会话 + `fault-prediction` skill 构建并执行,流水线运行在
`test_mcp_skill` 工作空间内。

## 服务配置

```
python -m fault_platform serve --port 8765 \
  --data-root      test_mcp_skill/data \
  --storage-root   test_mcp_skill/.fault-platform/pipelines \
  --artifact-cache-mb 512
```

- 数据根目录:`test_mcp_skill/data`,组件路径相对该目录写作 `synthetic_equipment.csv`
- 图 XML:`test_mcp_skill/.fault-platform/pipelines/fault_prediction_equipment.xml`
- 流水线 ID:`pipeline_95b75f0cfd5e`,工作空间:`ws_eba76bf7e7fd`,检查点:`cp_e447553ee779`

## 数据画像

| 项目 | 值 |
| --- | --- |
| 行数 × 列数 | 5760 × 6 |
| 列 | `equipment`, `time`, `label`, `vibration`, `temperature`, `pressure` |
| 设备数 | 90 |
| 时间步 | 每台 0–63,共 64 步 |
| 标签 | 3 类,各 1920 行(0=1920, 1=1920, 2=1920) |
| 缺失率 | 全列为 0 |

关键结构:**每台设备的 `label` 在其全部 64 个时间步上恒定**,设备内无状态跳变。因此该数据集
支持的是「设备当前状态三分类」,不是故障发生时刻预测,也不含退化到失效的轨迹。见文末说明。

## 特征工程

三个传感器列(`vibration`, `temperature`, `pressure`)进入两条并行分支,窗口参数完全一致:

| 参数 | 值 |
| --- | --- |
| group_column / time_column | `equipment` / `time` |
| window_size / step | 16 / 8(重叠窗口) |
| label_column / label_policy | `label` / `strict` |
| 统计分支 | mean, std, rms, min, max, median, range, iqr, skewness, kurtosis, crest_factor |
| 拟合分支 | 线性:coef_1, coef_0, r2, trend_strength, residual_mean, residual_std |

- 统计分支产出 33 个特征,拟合分支产出 18 个,`feature.merge` 后共 **51 个特征**
- 窗口行数:90 台 × 7 窗 = **630** 行
- 传感器特征只取传感器列,`label` 与 `equipment` 未进入特征;窗口标签由 `feature.statistical`
  的 `labels` 端口生成,与聚合后的窗口行对齐
- 频域分支未启用:原始采样率未知,而 `feature.spectral` 的 `sampling_rate` 必须是真实采样率,
  不能臆测

## 验证设计

- 划分方式:`split_method=group`,按设备分组。窗口有重叠,随机划分会让同一台设备的窗口同时落入
  训练与测试集,造成泄漏
- 三个模型使用**完全相同的特征、标签、划分方式、test_size=0.25、random_state=42**,
  `validation.compare` 校验了测试索引一致
- 训练 469 行 / 测试 161 行(23 台设备,类别比 9:5:9)
- 方差筛选与互信息排序作为**终端探索分支**,未接入模型输入

## 结果

| 模型 | Accuracy | Precision | Recall | F1 | ROC-AUC |
| --- | --- | --- | --- | --- | --- |
| Random Forest (n=300) | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| SVM (rbf, C=10) | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| XGBoost (n=300, depth=4) | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |

三者的混淆矩阵同为 `[[63,0,0],[0,35,0],[0,0,63]]`,测试集 161 个窗口全部预测正确。

该结果已由 `python -m fault_platform run <xml> --data-root test_mcp_skill/data` 独立复算,
指标完全一致(SUCCESS,全部 11 个节点通过,无警告)。

### 特征排序(探索性)

- 互信息前几名:`temperature__mean`/`__rms`、`pressure__mean`/`__rms`/`__median`、
  `vibration__mean`;多个特征取到 1.100,接近三分类的 ln3≈1.099
- 随机森林重要度:`pressure__min`、`temperature__median`、`temperature__rms`、`pressure__coef_0`
- XGBoost 重要度高度集中:`pressure__mean` 0.566、`temperature__mean` 0.416,其余均 <0.01
- 方差筛选(阈值 0)未剔除任何特征,51 个特征的方差均大于 0

各传感器按标签的均值差异明显,与上述排序一致:

| label | vibration | temperature | pressure |
| --- | --- | --- | --- |
| 0 | 1.99 | 36.57 | 10.01 |
| 1 | 3.43 | 41.53 | 9.00 |
| 2 | 4.86 | 46.59 | 8.00 |

## 结论的适用范围

1. **这是状态分类,不是未来故障预测。** 设备内 `label` 恒定,数据里不存在故障起始时刻或退化
   过程,因此无法导出「多久后会发生故障」或 RUL 这类目标。若需要真正的预测性维护目标,需要
   带时间演变的数据:故障发生时刻的时间戳,或带剩余寿命的退化轨迹。
2. **满分来自数据本身。** 三类在三个传感器的均值上几乎线性可分(见上表),模型没有遇到困难。
   这个分数证明实现正确,不代表工业现场的泛化能力。合成数据上的指标不能外推。
3. **频域信息未纳入。** 若拿到真实采样率(Hz),可以加 `feature.spectral` 分支,取
   `dominant_frequency`、`band_energy_ratio_*`、`harmonic_ratio` 等特征,对旋转机械的轴承/
   齿轮故障更有价值。
4. **互信息与方差评分在全量数据上拟合**,属于探索性结论,其分数不独立于留出集;模型输入未使用
   它们的筛选结果。

## 图结构

```
ingest ─┬─ overview                                        (终端:画像)
        ├─ stats ─────┬──────────────► merge ─┬─ select_var  (终端:方差筛选)
        │             │                       ├─ select_mi   (终端:互信息, +stats.labels)
        └─ fitting ───┘                       └─ rf / svm / xgb ─► compare
                                                  ▲ stats.labels
```
