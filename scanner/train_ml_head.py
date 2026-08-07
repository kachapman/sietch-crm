"""ML head training script for the Sietch mail scanner classifier.

Trains a sentence-transformers + logistic/kNN classification head
on labeled email data extracted from the database:
  - Positive examples: emails manually linked to deals (mail_deal_links)
  - Negative examples: unlinked emails from the same accounts
  - Correction examples: from classifier_training_data table
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


def _load_training_data_from_db() -> list[dict]:
    """Query the database for real labeled email data."""
    try:
        # Import db from parent package
        sys.path.insert(0, str(ROOT.parent))
        # Load .env manually if available
        env_path = ROOT.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass
    try:
        import db
        db.init_db()
        query_dicts = db.query_dicts
    except ImportError:
        logger.warning("Could not import db module — falling back to empty dataset")
        return []

    samples: list[dict] = []

    # Positive examples: emails linked to deals (human-verified)
    try:
        linked_rows = query_dicts(
            """SELECT mm.subject, mm.body_text, mm.from_addr, mdl.opportunity_id
               FROM mail_deal_links mdl
               JOIN mail_messages mm ON mm.id = mdl.message_id
               WHERE mm.body_text IS NOT NULL AND mm.body_text != ''"""
        )
        for row in linked_rows:
            text = (row.get("body_text") or row.get("subject") or "").strip()
            if text and len(text) > 20:
                samples.append({"body": text[:2000], "label": "linked"})
        logger.info("Loaded %d positive (linked) examples from DB", len(linked_rows))
    except Exception as e:
        logger.warning("Failed to query linked emails: %s", e)

    # Negative examples: unlinked emails (limit to same accounts, cap at 3x positive count)
    neg_count = len(samples) * 3
    if neg_count < 100:
        neg_count = 100
    try:
        unlinked_rows = query_dicts(
            """SELECT mm.subject, mm.body_text, mm.from_addr
               FROM mail_messages mm
               WHERE mm.id NOT IN (SELECT message_id FROM mail_deal_links WHERE message_id IS NOT NULL)
                 AND mm.body_text IS NOT NULL AND mm.body_text != ''
               ORDER BY RANDOM()
               LIMIT %s""",
            (neg_count,),
        )
        for row in unlinked_rows:
            text = (row.get("body_text") or row.get("subject") or "").strip()
            if text and len(text) > 20:
                samples.append({"body": text[:2000], "label": "not_linked"})
        logger.info("Loaded %d negative (unlinked) examples from DB", len(unlinked_rows))
    except Exception as e:
        logger.warning("Failed to query unlinked emails: %s", e)

    # Correction examples from classifier_training_data
    try:
        correction_rows = query_dicts(
            """SELECT ctd.message_subject, mm.body_text, mm.from_addr,
                      ctd.correct_classification, ctd.correct_project_id
               FROM classifier_training_data ctd
               LEFT JOIN mail_messages mm ON mm.id = ctd.message_id
               WHERE ctd.correct_project_id IS NOT NULL"""
        )
        for row in correction_rows:
            text = (row.get("body_text") or row.get("message_subject") or "").strip()
            if text and len(text) > 20:
                samples.append({"body": text[:2000], "label": "linked"})
        logger.info("Loaded %d correction examples from DB", len(correction_rows))
    except Exception as e:
        logger.warning("Failed to query corrections: %s", e)

    return samples


def train(
    use_db: bool = True,
    mock_samples: int = 0,
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

    all_samples: list[dict] = []

    # Load real data from database
    if use_db:
        db_samples = _load_training_data_from_db()
        all_samples.extend(db_samples)

    # Fallback: generate mock data only if no DB data
    if not all_samples and mock_samples > 0:
        logger.info("No DB data — generating %d mock samples as fallback", mock_samples)
        for i in range(mock_samples):
            all_samples.append({
                "body": f"Claim #{1000 + i} update from carrier. Status update on claim {1000 + i}, please review.",
                "label": "not_linked",
            })

    if not all_samples:
        return {"ok": False, "error": "No training data available — run with --mock-samples for fallback"}

    # Ensure minimum viable dataset
    labels = [s.get("label", "not_linked") for s in all_samples]
    unique_labels = sorted(set(labels))
    if len(unique_labels) < 2:
        return {"ok": False, "error": f"Need at least 2 unique labels, got: {unique_labels}"}

    texts = [s.get("body") or s.get("subject") or "" for s in all_samples]
    logger.info("Training on %d samples with labels: %s", len(all_samples), unique_labels)

    model = SentenceTransformer("all-MiniLM-L6-v2")
    X = np.array(model.encode(texts, convert_to_numpy=True))

    logistic = LogisticRegression(max_iter=1000)
    knn = KNeighborsClassifier(n_neighbors=min(5, len(all_samples) - 1))

    X_train, _, y_train, _ = train_test_split(X, labels, test_size=0.2, random_state=42)
    logistic.fit(X_train, y_train)
    knn.fit(X_train, y_train)

    # Evaluate
    from sklearn.metrics import accuracy_score
    X_test = X[len(X_train):]
    y_test = labels[len(X_train):]
    if len(X_test) > 0:
        log_acc = accuracy_score(y_test, logistic.predict(X_test))
        knn_acc = accuracy_score(y_test, knn.predict(X_test))
        logger.info("Logistic accuracy: %.2f, kNN accuracy: %.2f", log_acc, knn_acc)

    model_path = MODEL_DIR / f"{model_name}.pkl"
    meta_path = MODEL_DIR / f"{model_name}_meta.json"

    import pickle

    with open(model_path, "wb") as f:
        pickle.dump({"logistic": logistic, "knn": knn, "labels": unique_labels}, f)

    with open(meta_path, "w") as f:
        json.dump(
            {
                "model_name": model_name,
                "samples": len(all_samples),
                "labels": unique_labels,
                "positive_count": labels.count("linked"),
                "negative_count": labels.count("not_linked"),
            },
            f,
        )

    return {
        "ok": True,
        "model_path": str(model_path),
        "samples": len(all_samples),
        "labels": unique_labels,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train Sietch mail classifier head")
    parser.add_argument("--no-db", action="store_true", help="Skip DB data loading")
    parser.add_argument("--mock-samples", type=int, default=0, help="Generate N mock samples (fallback)")
    parser.add_argument("--model-name", default="sietch-classifier-head")
    args = parser.parse_args()

    result = train(use_db=not args.no_db, mock_samples=args.mock_samples, model_name=args.model_name)
    if result.get("ok"):
        logger.info("Training complete: %s", result)
    else:
        logger.error("Training failed: %s", result)


if __name__ == "__main__":
    main()
