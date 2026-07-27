"""ML head training script for the Sietch mail scanner classifier.

Trains a sentence-transformers + logistic/kNN classification head
on labeled email data stored in the database or mock data.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sietch.train_ml")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "mail_scanner"
MODEL_DIR = DATA_DIR / "ml_models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)


def generate_mock_data(n: int = 600) -> list[dict]:
    mock: list[dict] = []
    for i in range(n):
        mock.append(
            {
                "subject": f"Claim #{1000 + i} update from carrier",
                "body": f"Status update on claim {1000 + i}, please review.",
                "label": "carrier_email",
            }
        )
    return mock


def train(
    mock_samples: int = 300,
    use_feedback: bool = True,
    model_name: str = "sietch-classifier-head",
) -> dict:
    try:
        from sentence_transformers import SentenceTransformer
        from sklearn.linear_model import LogisticRegression
        from sklearn.neighbors import KNeighborsClassifier
        from sklearn.model_selection import train_test_split
        import numpy as np
    except ImportError as e:
        return {"ok": False, "error": f"Missing dependency: {e}"}

    mock_data = generate_mock_data(mock_samples)
    feedbacks: list[dict] = []
    feedback_path = DATA_DIR / "feedback.jsonl"
    if use_feedback and feedback_path.exists():
        with open(feedback_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    feedbacks.append(json.loads(line))

    all_samples = mock_data + feedbacks
    if not all_samples:
        return {"ok": False, "error": "No training data available"}

    texts = [s.get("body") or s.get("subject") or "" for s in all_samples]
    labels = [s.get("label", "uncertain") for s in all_samples]

    model = SentenceTransformer("all-MiniLM-L6-v2")
    X = np.array(model.encode(texts, convert_to_numpy=True))

    unique_labels = sorted(set(labels))
    if len(unique_labels) < 2:
        unique_labels.append("uncertain")

    logistic = LogisticRegression(max_iter=1000)
    knn = KNeighborsClassifier(n_neighbors=5)

    X_train, _, y_train, _ = train_test_split(X, labels, test_size=0.2, random_state=42)
    logistic.fit(X_train, y_train)
    knn.fit(X_train, y_train)

    model_path = MODEL_DIR / f"{model_name}.pkl"
    meta_path = MODEL_DIR / f"{model_name}_meta.json"

    import pickle

    with open(model_path, "wb") as f:
        pickle.dump({"logistic": logistic, "knn": knn, "labels": unique_labels}, f)

    with open(meta_path, "w") as f:
        json.dump(
            {"model_name": model_name, "samples": len(all_samples), "labels": unique_labels},
            f,
        )

    return {"ok": True, "model_path": str(model_path), "samples": len(all_samples)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Train Sietch mail classifier head")
    parser.add_argument("--generate-mock", type=int, default=0, help="Generate N mock samples")
    parser.add_argument("--use-feedback", action="store_true", help="Include feedback data")
    parser.add_argument("--model-name", default="sietch-classifier-head")
    args = parser.parse_args()

    if args.generate_mock > 0:
        data = generate_mock_data(args.generate_mock)
        logger.info("Generated %d mock samples", len(data))

    result = train(mock_samples=args.generate_mock or 300, use_feedback=args.use_feedback)
    if result.get("ok"):
        logger.info("Training complete: %s", result)
    else:
        logger.error("Training failed: %s", result)


if __name__ == "__main__":
    main()