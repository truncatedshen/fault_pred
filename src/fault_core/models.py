"""Train/test validation with aligned indices and explicit split provenance."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier
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

from fault_core.features import (
    CategoricalEncoder,
    coverage_subset,
    rows_without_overlap,
    windows_share_rows,
)


@dataclass
class TrainedClassifier:
    estimator: Any
    encoder: LabelEncoder
    columns: list[str]
    categorical_encoders: list[CategoricalEncoder] = field(default_factory=list)

    @staticmethod
    def _is_finite_numeric(features: pd.DataFrame) -> bool:
        try:
            return bool(np.isfinite(features.to_numpy(dtype=float, copy=False)).all())
        except (TypeError, ValueError):
            return False

    def prepare_features(self, features: pd.DataFrame) -> pd.DataFrame:
        """Accept either the original encoded schema or raw categorical columns."""
        if not isinstance(features, pd.DataFrame):
            raise ValueError("Prediction features must be a pandas DataFrame")
        if list(features.columns) == self.columns and self._is_finite_numeric(features):
            return features

        prepared = features.copy()
        for categorical_encoder in self.categorical_encoders:
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
        prepared = self.prepare_features(features)
        return self.encoder.inverse_transform(np.asarray(self.estimator.predict(prepared), dtype=int))


def _average_precision(estimator: Any, holdout: pd.DataFrame, y: np.ndarray, classes: int) -> float | None:
    """PR-AUC: the threshold-free metric that matters when faults are rare."""
    if not hasattr(estimator, "predict_proba"):
        return None
    try:
        probability = estimator.predict_proba(holdout)
        if classes == 2:
            return float(average_precision_score(y, probability[:, 1]))
        return float(average_precision_score(y, probability, average="macro"))
    except ValueError:
        return None


def _miss_rate(y_true: np.ndarray, predicted: np.ndarray, encoder: LabelEncoder) -> float | None:
    """Share of true faults predicted as normal (binary tasks: the costly error)."""
    if len(encoder.classes_) != 2:
        return None
    positive = encoder.transform([encoder.classes_[-1]])[0]
    faults = y_true == positive
    if not faults.any():
        return None
    return float((predicted[faults] != positive).mean())


def _coverage(attrs: dict[str, Any], train: np.ndarray, test: np.ndarray) -> dict[str, Any]:
    """How many validation groups (instances, assets) were held out for real.

    Window keys such as ``g3_w1080`` do not name the equipment, so the mapping lives in
    the feature attributes; without this an evaluation cannot tell "new asset" from
    "new event on an asset already seen".
    """
    report: dict[str, Any] = {}
    for name, key in (("instances", "groups"), ("assets", "assets")):
        labels = attrs.get(key)
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
    **parameters: Any,
) -> dict[str, Any]:
    if not features.index.is_unique or not labels.index.is_unique or not features.index.equals(labels.index):
        raise ValueError("Feature and label indices must be unique and exactly aligned")
    if len(features) < 8 or features.shape[1] == 0:
        raise ValueError("Validation needs at least eight samples and one feature")
    if labels.name in features.columns:
        raise ValueError("The label column cannot also be a model feature")
    for key in ("source_rows", "source_rows_ranges", "source_path", "source_id"):
        if features.attrs.get(key) != labels.attrs.get(key):
            raise ValueError(f"Feature and label provenance differs: {key}")
    if labels.isna().any() or not np.isfinite(features.to_numpy(dtype=float, copy=False)).all():
        raise ValueError("Features and labels must not contain missing/infinite values")
    if labels.nunique() < 2:
        raise ValueError("Classification needs at least two classes")
    encoder = LabelEncoder().fit(labels)
    y = encoder.transform(labels)
    indices = np.arange(len(features))
    warnings = list(features.attrs.get("evaluation_warnings", []))
    has_coverage = "source_rows_ranges" in features.attrs or bool(features.attrs.get("source_rows"))
    if split_method == "group":
        groups = features.attrs.get("groups")
        if not features.attrs.get("grouped") or groups is None or len(groups) != len(features):
            raise ValueError("Group split requires feature extraction with group_column")
        train, test = next(
            GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=random_state).split(
                features, y, groups=groups
            )
        )
    elif split_method == "temporal":
        boundary = int(len(features) * (1 - test_size))
        train, test = indices[:boundary], indices[boundary:]
        if has_coverage:
            # Interval logic keeps streamed features from expanding every window's rows.
            train = np.array(rows_without_overlap(features.attrs, list(test), list(train)), dtype=int)
    elif split_method == "stratified":
        if features.attrs.get("overlapping"):
            raise ValueError("Overlapping windows require group or temporal split")
        groups = features.attrs.get("groups")
        if features.attrs.get("grouped") and groups and len(set(groups)) < len(groups):
            raise ValueError("Repeated equipment groups require group or temporal split")
        train, test = train_test_split(indices, test_size=test_size, random_state=random_state, stratify=y)
    else:
        raise ValueError(f"Unknown split method: {split_method}")
    if len(train) < 2 or len(test) < 1 or len(np.unique(y[train])) != len(encoder.classes_):
        raise ValueError("Split leaves too few rows or omits a class from training; adjust split/data")
    if has_coverage:
        if windows_share_rows(
            coverage_subset(features.attrs, list(train)), coverage_subset(features.attrs, list(test))
        ):
            raise ValueError("Train/test windows share source rows")
    if algorithm == "random_forest":
        estimator = RandomForestClassifier(random_state=random_state, n_jobs=1, **parameters)
    elif algorithm == "svm":
        probability = parameters.pop("probability", True)
        estimator = make_pipeline(StandardScaler(), SVC(random_state=random_state, **parameters))
        if probability:
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
    else:
        raise ValueError(f"Unknown algorithm: {algorithm}")
    estimator.fit(features.iloc[train], y[train])
    predicted = estimator.predict(features.iloc[test]).astype(int)
    precision, recall, f1, _ = precision_recall_fscore_support(
        y[test], predicted, average="weighted", zero_division=0
    )
    auc = None
    if hasattr(estimator, "predict_proba"):
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
        "balanced_accuracy": float(balanced_accuracy_score(y[test], predicted)),
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
        "roc_auc": auc,
        "average_precision": _average_precision(estimator, features.iloc[test], y[test], len(encoder.classes_)),
        "confusion_matrix": confusion_matrix(
            y[test], predicted, labels=np.arange(len(encoder.classes_))
        ).tolist(),
        "classes": encoder.classes_.tolist(),
        "per_class_recall": {
            str(label): float(value)
            for label, value in zip(
                encoder.classes_,
                recall_score(y[test], predicted, average=None, labels=np.arange(len(encoder.classes_)), zero_division=0),
            )
        },
        "test_class_counts": {
            str(label): int(count) for label, count in zip(*np.unique(y[test], return_counts=True))
        },
        "miss_rate": _miss_rate(y[test], predicted, encoder),
        "split_method": split_method,
        "train_count": len(train),
        "test_count": len(test),
        "random_state": random_state,
        "train_indices": features.index[train].tolist(),
        "test_indices": features.index[test].tolist(),
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
    if algorithm != "svm":
        outputs["importance"] = (
            pd.DataFrame({"feature": features.columns, "importance": estimator.feature_importances_})
            .sort_values("importance", ascending=False)
            .reset_index(drop=True)
        )
    return outputs
