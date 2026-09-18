"""Train/test validation with aligned indices and explicit split provenance.

这是"给结论"的一层：把窗口特征与标签切分、训练、评估，并返回
``model / prediction / metrics``（部分算法还有 ``importance``）。

设计上最值得注意的三点：

1. **切分方式由数据结构决定，而不是由习惯决定。** 重叠窗口必须用 ``group``/``asset``/
   ``temporal``；``stratified`` 在检测到重叠或重复分组时直接拒绝，
   否则训练集和测试集会共享原始行，指标会明显虚高。
2. **provenance 全程校验。** 特征与标签必须索引一致、来源字段一致；
   切分完成后再用 ``windows_share_rows`` 复查两个集合是否共享原始行。
3. **警告随指标一起返回。** 上游的全量拟合警告（``evaluation_warnings``）会被带进
   ``metrics["warnings"]``，ROC-AUC 无法计算等情况也会补一条，而不是静默给 None。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    confusion_matrix,
    precision_recall_fscore_support,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.svm import SVC
from sklearn.tree import DecisionTreeClassifier

from fault_core.features import (
    CategoricalEncoder,
    coverage_subset,
    rows_without_overlap,
    values_equal,
    windows_share_rows,
)


@dataclass
class TrainedClassifier:
    """训练好的分类器：估计器 + 标签编码器 + 列模式 + 内嵌的类别编码器。

    它承担的是"可复用的推理入口"角色：输入既可以是训练时的那批数值特征列，
    也可以是原始类别列——后者由内嵌的 :class:`~fault_core.features.CategoricalEncoder`
    现场转换。因此模型经过 pickle、缓存溢写到磁盘或检查点恢复后，仍能直接吃新数据。
    """

    estimator: Any
    encoder: LabelEncoder
    columns: list[str]
    categorical_encoders: list[CategoricalEncoder] = field(default_factory=list)

    @staticmethod
    def _is_finite_numeric(features: pd.DataFrame) -> bool:
        """整表能否安全转成 float 且全部有限；用于判断"是否已是训练时的特征模式"。"""
        try:
            return bool(np.isfinite(features.to_numpy(dtype=float, copy=False)).all())
        except (TypeError, ValueError):
            return False

    def prepare_features(self, features: pd.DataFrame) -> pd.DataFrame:
        """把输入整理成估计器需要的列（列名与顺序都要与训练时一致）。

        两条路径：列名完全一致且数值有限时直接返回；
        否则用内嵌的类别编码器把原始类别列补成编码后的列，再按训练列顺序重排。
        缺少必需列或转换后仍非有限值都会报错，绝不"尽力凑一凑"。
        """
        if not isinstance(features, pd.DataFrame):
            raise ValueError("Prediction features must be a pandas DataFrame")
        if list(features.columns) == self.columns and self._is_finite_numeric(features):
            return features

        prepared = features.copy()
        for categorical_encoder in self.categorical_encoders:
            # 如果调用方已经自己编码过（列在且是数值），就不重复编码——重复会得到两套语义。
            encoded_already = all(
                column in prepared.columns and pd.api.types.is_numeric_dtype(prepared[column])
                for column in categorical_encoder.output_columns
            )
            if encoded_already:
                continue
            missing = set(categorical_encoder.columns) - set(features.columns)
            if missing:
                raise ValueError(
                    "Prediction data needs either the fitted categorical features or raw columns: "
                    f"{sorted(missing)}"
                )
            encoded = categorical_encoder.transform(features)
            for column in encoded.columns:
                prepared[column] = encoded[column]

        missing = [column for column in self.columns if column not in prepared.columns]
        if missing:
            raise ValueError(f"Prediction data cannot produce trained feature columns: {missing}")
        prepared = prepared.loc[:, self.columns]
        if not self._is_finite_numeric(prepared):
            raise ValueError("Prediction features must be finite numeric values after transformation")
        return prepared

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        """预测原始标签（把编码后的整数还原成训练时见过的类别名）。"""
        prepared = self.prepare_features(features)
        return self.encoder.inverse_transform(np.asarray(self.estimator.predict(prepared), dtype=int))


class ReservoirTransformer(BaseEstimator, TransformerMixin):
    """确定性回声状态（reservoir）映射：每一行特征独立跑一遍储备池。

    这里的"时间步"不是数据的时间轴，而是把``n_steps``次迭代当作固定的非线性展开，
    因此对同一行输入永远得到同一状态向量（种子固定、无随机性），
    可以用作逻辑回归前端的固定随机特征映射。
    """

    def __init__(
        self,
        reservoir_size: int = 50,
        spectral_radius: float = 0.9,
        input_scale: float = 0.5,
        leaking_rate: float = 1.0,
        n_steps: int = 3,
        random_state: int = 42,
    ):
        self.reservoir_size = reservoir_size
        self.spectral_radius = spectral_radius
        self.input_scale = input_scale
        self.leaking_rate = leaking_rate
        self.n_steps = n_steps
        self.random_state = random_state

    def fit(self, x: Any, y: Any = None) -> ReservoirTransformer:
        """初始化输入权重与循环权重（不训练：这是固定映射，不是可学习网络）。"""
        values = np.asarray(x, dtype=float)
        if values.ndim != 2:
            raise ValueError("Reservoir input must be a two-dimensional feature matrix")
        rng = np.random.default_rng(self.random_state)
        self.input_weights_ = rng.uniform(
            -self.input_scale, self.input_scale, size=(self.reservoir_size, values.shape[1] + 1)
        )
        recurrent = rng.normal(size=(self.reservoir_size, self.reservoir_size))
        # 归一化谱半径：把循环矩阵的模最大特征值缩放到 spectral_radius（<1 保证状态收敛）。
        radius = float(np.max(np.abs(np.linalg.eigvals(recurrent))))
        self.recurrent_weights_ = recurrent * (self.spectral_radius / radius if radius else 0.0)
        self.n_features_in_ = values.shape[1]
        return self

    def transform(self, x: Any) -> np.ndarray:
        """逐行迭代状态方程，返回每行的最终状态向量。"""
        values = np.asarray(x, dtype=float)
        # 列数必须与 fit 时一致，否则权重矩阵与输入维度对不上。
        if values.ndim != 2 or values.shape[1] != self.n_features_in_:
            raise ValueError("Reservoir feature schema differs from training")
        output = np.empty((len(values), self.reservoir_size), dtype=float)
        for row_index, row in enumerate(values):
            # 每行从零状态出发（独立样本假设）；signal 前置 1.0 是偏置项。
            state = np.zeros(self.reservoir_size, dtype=float)
            signal = np.r_[1.0, row]
            for _ in range(self.n_steps):
                candidate = np.tanh(self.input_weights_ @ signal + self.recurrent_weights_ @ state)
                # 泄漏积分：leaking_rate=1 时完全替换，越小越保留上一状态。
                state = (1 - self.leaking_rate) * state + self.leaking_rate * candidate
            output[row_index] = state
        return output


def _average_precision(estimator: Any, holdout: pd.DataFrame, y: np.ndarray, classes: int) -> float | None:
    """PR-AUC：不依赖判定阈值，是故障样本稀少时最有参考价值的指标。

    二分类取正类的概率列，多分类取宏平均；模型没有 ``predict_proba``（例如关闭概率的 SVM）
    或数据不足以计算时返回 None，调用方会在报告里看到空值而不是错误的数字。
    """
    if not hasattr(estimator, "predict_proba"):
        return None
    try:
        probability = estimator.predict_proba(holdout)
        if classes == 2:
            return float(average_precision_score(y, probability[:, 1]))
        return float(average_precision_score(y, probability, average="macro"))
    except ValueError:
        return None


def _class_balance(y: np.ndarray, classes: np.ndarray) -> tuple[dict[str, int], dict[str, float]]:
    """一侧样本（训练集或测试集）的类别构成：**计数 + 占比**。

    为什么两样都要给：故障预测里正类常常只占几个百分点，光看 ``test_count=228``
    看不出"这 228 行里只有 13 行是故障"。占比才是让 accuracy 可被正确解读的那个数字——
    这也是为什么它是和 count 一起算、一起返回，而不是留给调用方自己除。
    """
    counted = {str(classes[label]): int(count) for label, count in zip(*np.unique(y, return_counts=True))}
    total = int(len(y))
    rates = {name: (count / total if total else 0.0) for name, count in counted.items()}
    return counted, rates


def _class_aware_group_split(
    groups: Any, y: np.ndarray, test_size: float, random_state: int
) -> tuple[np.ndarray, np.ndarray]:
    """整组留出，但**按类别分层**——保证训练集与测试集都拿得到故障样本。

    为什么不能直接用 ``GroupShuffleSplit``：它只按组随机分。真实数据里故障往往集中在少数几台
    设备上，于是很容易分出一个"一个正类都没有"的测试集，而那时的 accuracy / balanced_accuracy
    全是假象（实测 HBM 原始日志：按服务器留出时测试集 155 个窗口里 0 个正类，accuracy=1.0）。

    做法（确定性，不依赖 sklearn 版本）：

    1. 先随机打乱组的顺序（只吃 ``random_state``，保证同参数可复现、不同种子结果不同）；
    2. **类越稀有越先安排**：对该类，逐个把它所在的组放进"这一类相对目标份额更缺"的一侧；
       某一侧还没有这个类时优先放过去——这两条合起来保证**只要 ≥2 个组含这个类，两边就都有**，
       这正是"测试集不能没有故障样本"要的性质；
    3. 兜底：万一还有组没落地，就按窗口数从大到小填进"规模相对目标更缺"的一侧。
       （正常数据上第一步用不到：每组至少含一个类，逐类分配总会把每个组都安排掉。）

    代价要说清楚：这样切出来的测试集**不是均匀随机抽组**，而是为了可评估性刻意分层的。
    只有一个组含故障时无法两全——那时它必须留在训练集（否则模型学不到这个类），
    测试集仍然没有正类，调用方会收到明说这一点的警告。

    这个"某一侧还没有这个类就放过去"的规则是实测补上的：只按份额比例分配时，两个各含 4 个
    正类的组会被**同时**放进训练集（比例 4/6 < 4/2），测试集于是又是 0 个正类——正是这条
    函数要解决的问题本身。
    """
    labels = np.asarray(y)
    ids = np.asarray([str(item) for item in groups])
    unique_groups = list(dict.fromkeys(ids.tolist()))
    if len(unique_groups) < 2:
        raise ValueError(f"Group split needs at least two groups, found {len(unique_groups)}")
    np.random.default_rng(random_state).shuffle(unique_groups)
    # 打乱后的次序是**唯一**的并列裁决依据：若改用组名做 tie-break，计数相同的组永远按名字
    # 排，random_state 就变成了摆设（实测 9 组数据在 seed 0..5 下给出完全相同的划分）。
    order = {name: position for position, name in enumerate(unique_groups)}
    classes = [int(value) for value in np.unique(labels)]
    sizes = {name: int((ids == name).sum()) for name in unique_groups}
    counts = {
        name: {value: int(((ids == name) & (labels == value)).sum()) for value in classes}
        for name in unique_groups
    }
    total_windows = int(labels.size)
    shares = {"train": 1.0 - test_size, "test": test_size}
    assigned: dict[str, str] = {}
    side_size = {"train": 0, "test": 0}
    side_class = {side: dict.fromkeys(classes, 0) for side in ("train", "test")}

    def place(name: str, side: str) -> None:
        assigned[name] = side
        side_size[side] += sizes[name]
        for value, count in counts[name].items():
            side_class[side][value] += count

    def balance(per_side: dict[str, float], target: float) -> str:
        """把"放进哪一侧"交给"相对目标份额更缺"的那边（目标的份额由 test_size 决定）。"""
        ratio = {side: per_side[side] / max(shares[side] * target, 1e-9) for side in ("train", "test")}
        return "train" if ratio["train"] <= ratio["test"] else "test"

    # 2. 稀有类先落地：每一个类都尽量两边都有。
    for value in sorted(classes, key=lambda item: (int((labels == item).sum()), item)):
        total = int((labels == value).sum())
        candidates = sorted(
            (name for name in unique_groups if name not in assigned and counts[name][value] > 0),
            key=lambda name: (-counts[name][value], -sizes[name], order[name]),
        )
        for name in candidates:
            present = {side: side_class[side][value] for side in shares}
            # "某一侧还没有这个类"优先于按比例平衡：这正是"测试集不能一个正类都没有"的保证。
            # 两边都有（或都没有）之后才交给比例规则去照顾规模。
            absent = [side for side in ("train", "test") if present[side] == 0]
            side = (
                absent[0]
                if len(absent) == 1
                else balance({s: present[s] + counts[name][value] for s in shares}, total)
            )
            place(name, side)
    # 3. 其余组补规模。
    for name in sorted(
        (item for item in unique_groups if item not in assigned), key=lambda item: (-sizes[item], order[item])
    ):
        side = balance({s: side_size[s] + sizes[name] for s in shares}, total_windows)
        place(name, side)

    if not side_size["train"] or not side_size["test"]:
        raise ValueError("Class-aware group split produced an empty side; adjust test_size or data")
    train = np.array([position for position, name in enumerate(ids) if assigned[name] == "train"])
    test = np.array([position for position, name in enumerate(ids) if assigned[name] == "test"])
    return train, test


def _class_aware_temporal_boundary(y: np.ndarray, test_size: float) -> tuple[int, str | None]:
    """时间留出的切点允许在目标比例附近**小幅移动**，好让两侧都有故障样本。

    时间切分必须保持"训练在前、测试在后"，不能像 :func:`_class_aware_group_split` 那样任意
    挑组。但切点本身是可以挪的：故障往往集中在某几段，末段偏偏全是正常样本时，把切点往前
    挪一点就能拿到一个"测得到故障"的留出集，而这正是评估的**前提**。

    可移动范围限定在 ``test_size`` 的 ±50% 之内（即测试集占 ``[0.5, 1.5] × test_size``），
    这样测试规模不会被挪到失去代表性的地步；超出这个范围宁可保留目标切点，让
    :func:`_split_balance_findings` 去明说"测试集没有正类"。

    返回 ``(切点, 说明或 None)``。说明只在**切点被移动**、或**挪了也凑不齐**时给出——
    留出集不是"最后 25%"这件事必须留在指标里，否则报告里的数字解释不通。
    """
    labels = np.asarray(y)
    size = int(labels.size)
    target = int(size * (1 - test_size))
    classes = np.unique(labels)
    # ±50% 的窗口：下限对应"测试集最多 1.5×test_size"，上限对应"最少 0.5×test_size"。
    low = max(1, int(np.floor(size * (1 - 1.5 * test_size))))
    high = min(size - 1, int(np.ceil(size * (1 - 0.5 * test_size))))
    best: int | None = None
    for boundary in range(low, high + 1):
        if len(np.unique(labels[:boundary])) != len(classes):
            continue
        if len(np.unique(labels[boundary:])) != len(classes):
            continue
        if best is None or abs(boundary - target) < abs(best - target):
            best = boundary
    if best is None:
        note = (
            f"Temporal split: no cut point inside [{low}, {high}] keeps every class on both sides "
            f"(target boundary {target}); the holdout below may be missing a class, so read its "
            "metrics with that in mind"
        )
        return target, note
    if best == target:
        return target, None
    share = (size - best) / size
    return best, (
        f"Temporal split: boundary moved from {target} to {best} so both sides contain every class; "
        f"the holdout is now {size - best}/{size} rows ({share:.1%}) instead of the requested "
        f"{test_size:.1%}"
    )


def _split_balance_findings(
    train_counts: dict[str, int], test_counts: dict[str, int], positive_class: str | None
) -> list[str]:
    """切分本身是否还剩下两类样本——没有故障样本的测试集，指标全是假象。"""
    notes: list[str] = []
    only_train = [name for name in train_counts if name not in test_counts]
    only_test = [name for name in test_counts if name not in train_counts]
    if only_train:
        notes.append(
            f"Holdout split has no {', '.join(sorted(only_train))} rows: train {train_counts}, "
            f"test {test_counts}. accuracy/balanced_accuracy/per_class_recall on this split say "
            "nothing about that class — do not read them as a score."
        )
    if only_test:
        notes.append(
            f"Training split has no {', '.join(sorted(only_test))} rows: train {train_counts}, "
            f"test {test_counts}. The model cannot have learned this class."
        )
    if positive_class is not None and not test_counts.get(positive_class):
        notes.append(
            f"The test set contains no positive ({positive_class}) rows; every metric here measures "
            "the negative class only."
        )
    return notes


def _positive_index(encoder: LabelEncoder, positive_class: str | None) -> int | None:
    """确定哪个类别算"故障类"，用于计算漏报率。

    显式给出 ``positive_class`` 时按字符串匹配，匹配不到直接报错（拼错标签是常见事故）；
    留空则约定取排序后的**最后一个类别**，因此标签命名会影响默认含义——报告里要写清楚。
    """
    if positive_class:
        for index, label in enumerate(encoder.classes_):
            if str(label) == str(positive_class):
                return index
        raise ValueError(
            f"positive_class {positive_class!r} is not among the labels {list(encoder.classes_)}"
        )
    return len(encoder.classes_) - 1


def _miss_rate(
    y_true: np.ndarray, predicted: np.ndarray, encoder: LabelEncoder, positive_class: str | None
) -> float | None:
    """漏报率：真实故障中被判成正常的比例（二分类里代价最高的错误）。

    只对二分类计算；测试集里没有故障样本时返回 None。
    """
    if len(encoder.classes_) != 2:
        return None
    positive = _positive_index(encoder, positive_class)
    faults = y_true == positive
    if not faults.any():
        return None
    return float((predicted[faults] != positive).mean())


def _coverage(attrs: dict[str, Any], train: np.ndarray, test: np.ndarray) -> dict[str, Any]:
    """How many validation groups (instances, assets) were held out for real.

    Window keys such as ``g3_w1080`` do not name the equipment, so the mapping lives in
    the feature attributes; without this an evaluation cannot tell "new asset" from
    "new event on an asset already seen".

    返回训练/测试各有多少实例（分组）与资产，以及测试集中**训练时没见过**的数量与前 20 个样例。
    报告时把这几个数字和指标并列，才能说明"这个分数是不是在没见过的设备上得到的"。
    """
    report: dict[str, Any] = {}
    for name, key in (("instances", "groups"), ("assets", "assets")):
        labels = attrs.get(key)
        # 没有该属性（例如没设 asset_column）时跳过这一组，而不是报错：
        # 资产级覆盖信息是可选增强，缺失本身由 split 校验负责拦截。
        if labels is None or len(labels) != len(attrs.get("groups", labels)):
            continue
        train_labels = sorted({str(labels[position]) for position in train})
        test_labels = sorted({str(labels[position]) for position in test})
        unseen = [label for label in test_labels if label not in set(train_labels)]
        report[f"train_{name}"] = len(train_labels)
        report[f"test_{name}"] = len(test_labels)
        report[f"test_{name}_unseen"] = len(unseen)
        report[f"test_{name}_unseen_sample"] = unseen[:20]
    return report


def validate_model(
    features: pd.DataFrame,
    labels: pd.Series,
    algorithm: str = "random_forest",
    split_method: str = "stratified",
    test_size: float = 0.25,
    random_state: int = 42,
    positive_class: str | None = None,
    **parameters: Any,
) -> dict[str, Any]:
    """训练并评估一个分类器；``algorithm`` 决定估计器，``split_method`` 决定划分方式。

     ``split_method`` 四选一：

     * ``stratified``：随机分层切分，仅适用于行之间相互独立的数据；
     * ``group``：按 ``group_column``（实例）整组留出；
     * ``asset``：按资产整组留出，需要上游窗口组件设置了 ``asset_column``；
     * ``temporal``：按现有行序取后段为测试，并剔除与测试窗口共享原始行的训练窗口（purge）。

     三种"整组/整段留出"都**按类别分层**：故障样本必须同时落在训练集和测试集里，否则
     测试集一个正类都没有，``accuracy``/``balanced_accuracy`` 会变成纯粹的假象（实测 HBM
     原始日志：按服务器留出时测试集 155 个窗口里 0 个正类、accuracy=1.0）。``group``/``asset``
     的做法见 :func:`_class_aware_group_split`，``temporal`` 的做法见
     :func:`_class_aware_temporal_boundary`；真的做不到（例如只有一个实例发生过故障）时，
     平台不会静默——指标里会出现明说这一点的 warning。

    估计器分支：``random_forest`` / ``decision_tree`` / ``svm``（标准化 + 可选概率校准）/
    ``xgboost``（按类别数自动选择 objective）/ ``reservoir_classifier``（标准化 + 储备池 + 逻辑回归）。
    额外参数通过 ``**parameters`` 透传给对应 sklearn/xgboost 估计器。

    ``metrics`` 里的**分数全部来自留出集**（训练侧只有 ``train_count`` / ``train_class_counts`` /
    ``train_class_rates`` / ``train_indices``），且 ``precision`` / ``recall`` / ``f1`` 是
    **宏平均（各类等权，不做加权）**：加权平均在故障稀少时由多数类主导，会把"一个故障都没抓到"
    藏起来。逐类数值在 ``per_class_precision`` / ``per_class_recall`` / ``per_class_f1`` /
    ``per_class_support`` 里，与 ``confusion_matrix`` 的行列一一对应，可以手算复核。

    返回 ``{"model", "prediction", "metrics"}``，有特征重要性的算法再带 ``"importance"``。
    """
    # ---- 输入校验：索引、列、provenance、缺失值、类别数 ----
    if not features.index.is_unique or not labels.index.is_unique or not features.index.equals(labels.index):
        raise ValueError("Feature and label indices must be unique and exactly aligned")
    if len(features) < 8 or features.shape[1] == 0:
        raise ValueError("Validation needs at least eight samples and one feature")
    if labels.name in features.columns:
        raise ValueError("The label column cannot also be a model feature")
    # provenance 一致 = 特征和标签来自同一批窗口（同一份 source_rows）。不一致说明接线错了。
    for key in ("source_rows", "source_rows_ranges", "assets", "source_path", "source_id"):
        # 区间是 numpy 数组（为了躲开 pandas 深拷贝 attrs），比较必须走 values_equal。
        if not values_equal(features.attrs.get(key), labels.attrs.get(key)):
            raise ValueError(f"Feature and label provenance differs: {key}")
    if labels.isna().any():
        raise ValueError("Labels contain missing values; filter or relabel before validating")
    broken = [str(column) for column, incomplete in features.isna().any().items() if incomplete]
    if broken:
        # 这是真实任务里最常见的一类失败：平窗口产生 NaN 频谱，被一路带到模型。
        raise ValueError(
            "Features contain missing values (window features such as spectra are NaN on flat "
            f"windows): {broken[:8]}. Insert feature.imputation before the model, or drop those columns."
        )
    if not np.isfinite(features.to_numpy(dtype=float, copy=False)).all():
        raise ValueError("Features must not contain infinite values; clean them upstream")
    if labels.nunique() < 2:
        raise ValueError("Classification needs at least two classes")
    encoder = LabelEncoder().fit(labels)
    y = encoder.transform(labels)
    indices = np.arange(len(features))
    warnings = list(features.attrs.get("evaluation_warnings", []))
    # 切分被调整过（时间切点移动）时在这里留一句话：指标里的数字得配得上"留出集其实不是最后 25%"
    split_note: str | None = None
    # 有 provenance 才能做重叠行复查；老数据/手写特征表可能没有这些字段。
    has_coverage = "source_rows_ranges" in features.attrs or bool(features.attrs.get("source_rows"))
    if split_method == "group":
        # 需要窗口组件真的按 group_column 提取过，否则没法保证整组留出。
        groups = features.attrs.get("groups")
        if not features.attrs.get("grouped") or groups is None or len(groups) != len(features):
            raise ValueError("Group split requires feature extraction with group_column")
        # 按类别分层地整组留出：保证故障样本落到两侧，而不是"随到随分"。
        train, test = _class_aware_group_split(groups, y, test_size, random_state)
    elif split_method == "asset":
        # Whole assets (wells/machines) are held out, which is the honest question when a
        # deployment meets equipment it has never seen. Needs asset_column upstream.
        assets = features.attrs.get("assets")
        if assets is None or len(assets) != len(features):
            raise ValueError(
                "Asset split requires asset_column on the upstream window component "
                "(it records which asset each window belongs to)"
            )
        distinct_assets = set(assets)
        if len(distinct_assets) < 2:
            raise ValueError(f"Asset split needs at least two assets, found {len(distinct_assets)}")
        # 整座资产留出同样按类别分层：留出的数据中心一台故障都没有，等于没评估。
        train, test = _class_aware_group_split(list(assets), y, test_size, random_state)
    elif split_method == "temporal":
        # 切点可以在目标比例附近小幅移动，好让两侧都有故障样本；移动的事实写进 split_note。
        boundary, split_note = _class_aware_temporal_boundary(y, test_size)
        train, test = indices[:boundary], indices[boundary:]
        if has_coverage:
            # Interval logic keeps streamed features from expanding every window's rows.
            # 重叠窗口会让"时间上在后"的测试窗口与训练窗口共享原始行，必须把后者剔除。
            train = np.array(rows_without_overlap(features.attrs, list(test), list(train)), dtype=int)
    elif split_method == "stratified":
        # 两个明确拒绝：重叠窗口、以及同一台设备贡献多个窗口的情况。
        if features.attrs.get("overlapping"):
            raise ValueError("Overlapping windows require group or temporal split")
        groups = features.attrs.get("groups")
        if features.attrs.get("grouped") and groups and len(set(groups)) < len(groups):
            raise ValueError("Repeated equipment groups require group or temporal split")
        train, test = train_test_split(indices, test_size=test_size, random_state=random_state, stratify=y)
    else:
        raise ValueError(f"Unknown split method: {split_method}")
    # 切分后必须仍能训练：训练集要覆盖全部类别，测试集不能为空。
    if len(train) < 2 or len(test) < 1 or len(np.unique(y[train])) != len(encoder.classes_):
        raise ValueError("Split leaves too few rows or omits a class from training; adjust split/data")
    if has_coverage:
        # 兜底复查：即使选了 group/asset，也要确认两个集合真的没有共享原始行。
        if windows_share_rows(
            coverage_subset(features.attrs, list(train)), coverage_subset(features.attrs, list(test))
        ):
            raise ValueError("Train/test windows share source rows")
    if algorithm == "random_forest":
        # n_jobs=1：服务端并发由运行时控制，模型内部再并行会让内存与 CPU 不可预测。
        estimator = RandomForestClassifier(random_state=random_state, n_jobs=1, **parameters)
    elif algorithm == "decision_tree":
        estimator = DecisionTreeClassifier(random_state=random_state, **parameters)
    elif algorithm == "svm":
        # 标准化必须放在 pipeline 内部，保证只在训练折上拟合，避免用测试集统计量。
        probability = parameters.pop("probability", True)
        estimator = make_pipeline(StandardScaler(), SVC(random_state=random_state, **parameters))
        if probability:
            # 概率校准用交叉验证；折数受最小类样本数限制，样本太少会导致无意义校准。
            folds = min(5, int(np.bincount(y[train]).min()))
            if folds < 2:
                raise ValueError("Probability calibration requires at least two training samples per class")
            estimator = CalibratedClassifierCV(estimator, cv=folds, ensemble=False)
    elif algorithm == "xgboost":
        try:
            from xgboost import XGBClassifier
        except ImportError as exc:
            raise ValueError("Install XGBoost with pip install '.[xgboost]'") from exc
        objective = parameters.pop("objective", "auto")
        # 目标函数必须与任务匹配（二分类 logistic / 多分类 softprob），写错会得到无意义的概率。
        expected = "binary:logistic" if len(encoder.classes_) == 2 else "multi:softprob"
        if objective not in {"auto", expected}:
            raise ValueError(f"This classification task requires objective={expected}")
        estimator = XGBClassifier(
            random_state=random_state,
            n_jobs=1,
            objective=expected,
            eval_metric="logloss" if len(encoder.classes_) == 2 else "mlogloss",
            **parameters,
        )
    elif algorithm == "reservoir_classifier":
        # 储备池参数与逻辑回归参数在同一个 **parameters 里，这里按名字分流。
        reservoir_parameters = {
            name: parameters.pop(name)
            for name in (
                "reservoir_size",
                "spectral_radius",
                "input_scale",
                "leaking_rate",
                "n_steps",
            )
            if name in parameters
        }
        estimator = make_pipeline(
            StandardScaler(),
            ReservoirTransformer(random_state=random_state, **reservoir_parameters),
            LogisticRegression(random_state=random_state, **parameters),
        )
    else:
        raise ValueError(f"Unknown algorithm: {algorithm}")
    estimator.fit(features.iloc[train], y[train])
    predicted = estimator.predict(features.iloc[test]).astype(int)
    # 逐类精确率/召回率/F1 + 支持数。**不做加权平均**：加权值在故障稀少时由多数类决定，
    # 会把"一个故障都没抓出来"藏起来（实测过 miss_rate=1.0、accuracy=0.94 的模型）。
    # ``labels`` 用全部已知类别（而不是只在测试集里出现的那些），这样逐类表与混淆矩阵的行列
    # 一一对应：召回率_i = 混淆矩阵[i,i] / 第 i 行之和，可以手算复核。
    # 测试集里没出现的类别，precision/recall/F1 按 zero_division=0 记为 0（support 为 0 说明
    # 它没被测到），而不是被悄悄排除后让分数看起来更好。
    class_labels = np.arange(len(encoder.classes_))
    per_precision, per_recall, per_f1, per_support = precision_recall_fscore_support(
        y[test], predicted, labels=class_labels, zero_division=0
    )
    auc = None
    if hasattr(estimator, "predict_proba"):
        # 二分类取正类概率；多分类用一对多的宏平均 ROC-AUC。
        probability = estimator.predict_proba(features.iloc[test])
        try:
            auc = (
                float(roc_auc_score(y[test], probability[:, 1]))
                if len(encoder.classes_) == 2
                else float(
                    roc_auc_score(
                        y[test], probability, multi_class="ovr", labels=np.arange(len(encoder.classes_))
                    )
                )
            )
        except ValueError:
            warnings.append("ROC-AUC unavailable: the holdout split does not contain all classes.")
    else:
        warnings.append("ROC-AUC unavailable: this model does not provide probabilities.")
    prediction = pd.DataFrame(
        {
            "actual": encoder.inverse_transform(y[test]),
            "predicted": encoder.inverse_transform(predicted),
        },
        index=features.index[test],
    )
    # 训练/测试各自的类别构成：计数与占比一起给，"228 行测试集"才不是个没信息量的数字。
    train_class_counts, train_class_rates = _class_balance(y[train], encoder.classes_)
    test_class_counts, test_class_rates = _class_balance(y[test], encoder.classes_)
    warnings.extend(
        _split_balance_findings(
            train_class_counts,
            test_class_counts,
            str(encoder.classes_[_positive_index(encoder, positive_class)]),
        )
    )
    metrics = {
        "algorithm": algorithm,
        "accuracy": float(accuracy_score(y[test], predicted)),
        # 类别不平衡时 balanced_accuracy 比 accuracy 更能说明问题（等于各类召回的平均）。
        "balanced_accuracy": float(balanced_accuracy_score(y[test], predicted)),
        # 头条的 precision/recall/f1 = **宏平均**（各类等权，未加权）。数值恒等于下面
        # per_class_* 的算术平均，所以可以拿混淆矩阵与逐类表手算核对，不需要猜口径。
        "precision": float(per_precision.mean()),
        "recall": float(per_recall.mean()),
        "f1": float(per_f1.mean()),
        # 口径写在载荷里，别让读的人去猜"这是加权还是宏平均"。
        "averaging": "macro",
        "roc_auc": auc,
        "average_precision": _average_precision(
            estimator, features.iloc[test], y[test], len(encoder.classes_)
        ),
        "confusion_matrix": confusion_matrix(
            y[test], predicted, labels=np.arange(len(encoder.classes_))
        ).tolist(),
        "classes": encoder.classes_.tolist(),
        "per_class_precision": {
            str(label): float(value) for label, value in zip(encoder.classes_, per_precision)
        },
        "per_class_recall": {str(label): float(value) for label, value in zip(encoder.classes_, per_recall)},
        "per_class_f1": {str(label): float(value) for label, value in zip(encoder.classes_, per_f1)},
        # 支持数 = 测试集里这个类真实出现了多少次（等于混淆矩阵每行之和）。
        "per_class_support": {str(label): int(value) for label, value in zip(encoder.classes_, per_support)},
        "test_class_counts": test_class_counts,
        "train_class_counts": train_class_counts,
        "test_class_rates": test_class_rates,
        "train_class_rates": train_class_rates,
        "positive_class": str(encoder.classes_[_positive_index(encoder, positive_class)]),
        "miss_rate": _miss_rate(y[test], predicted, encoder, positive_class),
        "split_method": split_method,
        # 切分被调整过时的说明（目前只有 temporal 会移动切点），没调整就是 None。
        "split_note": split_note,
        "train_count": len(train),
        "test_count": len(test),
        "random_state": random_state,
        "train_indices": features.index[train].tolist(),
        "test_indices": features.index[test].tolist(),
        # coverage 与指标并列：没有它，"0.72 的 AUC"无法判断是否来自没见过的设备。
        "coverage": _coverage(features.attrs, train, test),
        "prediction_distribution": prediction["predicted"].value_counts().to_dict(),
        "warnings": warnings,
    }
    outputs = {
        "model": TrainedClassifier(
            estimator,
            encoder,
            list(features.columns),
            deepcopy(list(features.attrs.get("categorical_encoders", []))),
        ),
        "prediction": prediction,
        "metrics": metrics,
    }
    if hasattr(estimator, "feature_importances_"):
        outputs["importance"] = (
            pd.DataFrame({"feature": features.columns, "importance": estimator.feature_importances_})
            .sort_values("importance", ascending=False)
            .reset_index(drop=True)
        )
    return outputs
