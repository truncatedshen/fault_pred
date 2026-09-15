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
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import GroupShuffleSplit, train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.svm import SVC
from sklearn.tree import DecisionTreeClassifier

from fault_core.features import (
    CategoricalEncoder,
    coverage_subset,
    rows_without_overlap,
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

    估计器分支：``random_forest`` / ``decision_tree`` / ``svm``（标准化 + 可选概率校准）/
    ``xgboost``（按类别数自动选择 objective）/ ``reservoir_classifier``（标准化 + 储备池 + 逻辑回归）。
    额外参数通过 ``**parameters`` 透传给对应 sklearn/xgboost 估计器。

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
        if features.attrs.get(key) != labels.attrs.get(key):
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
    # 有 provenance 才能做重叠行复查；老数据/手写特征表可能没有这些字段。
    has_coverage = "source_rows_ranges" in features.attrs or bool(features.attrs.get("source_rows"))
    if split_method == "group":
        # 需要窗口组件真的按 group_column 提取过，否则没法保证整组留出。
        groups = features.attrs.get("groups")
        if not features.attrs.get("grouped") or groups is None or len(groups) != len(features):
            raise ValueError("Group split requires feature extraction with group_column")
        train, test = next(
            GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=random_state).split(
                features, y, groups=groups
            )
        )
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
        train, test = next(
            GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=random_state).split(
                features, y, groups=list(assets)
            )
        )
    elif split_method == "temporal":
        boundary = int(len(features) * (1 - test_size))
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
    # 加权平均的精确率/召回率/F1：类别不平衡时比单纯 accuracy 更可读。
    precision, recall, f1, _ = precision_recall_fscore_support(
        y[test], predicted, average="weighted", zero_division=0
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
    metrics = {
        "algorithm": algorithm,
        "accuracy": float(accuracy_score(y[test], predicted)),
        # 类别不平衡时 balanced_accuracy 比 accuracy 更能说明问题（等于各类召回的平均）。
        "balanced_accuracy": float(balanced_accuracy_score(y[test], predicted)),
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
        "roc_auc": auc,
        "average_precision": _average_precision(
            estimator, features.iloc[test], y[test], len(encoder.classes_)
        ),
        "confusion_matrix": confusion_matrix(
            y[test], predicted, labels=np.arange(len(encoder.classes_))
        ).tolist(),
        "classes": encoder.classes_.tolist(),
        "per_class_recall": {
            str(label): float(value)
            for label, value in zip(
                encoder.classes_,
                recall_score(
                    y[test], predicted, average=None, labels=np.arange(len(encoder.classes_)), zero_division=0
                ),
            )
        },
        "test_class_counts": {
            str(encoder.classes_[label]): int(count)
            for label, count in zip(*np.unique(y[test], return_counts=True))
        },
        "train_class_counts": {
            str(encoder.classes_[label]): int(count)
            for label, count in zip(*np.unique(y[train], return_counts=True))
        },
        "positive_class": str(encoder.classes_[_positive_index(encoder, positive_class)]),
        "miss_rate": _miss_rate(y[test], predicted, encoder, positive_class),
        "split_method": split_method,
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
