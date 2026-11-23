"""Fits the frost-from-image function (`hp frost-train`).

Training data: coil images taken while the fan was running, each labelled with
the frost score that `coil_pressure` gave at that moment (the same 15 s average
that `frost.py` uses). The model is a ridge regression on the standardised
features of `frost_image.image_features`, written to `frost_model.json`.

Each feature is limited to the range it had in the training images before it is
used, so a picture unlike any of them (fog, a blocked lens) cannot push the
score far outside what the model has seen.

Heavy frost is rare in the data, so images are weighted to even out the score
ranges (up to 8 times). The error is measured by leaving out one day at a time
and predicting it from the others: images a minute apart are nearly identical,
so a random split would flatter the result.
"""

import itertools
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import numpy as np

from heatpump.analysis import frost, frost_image
from heatpump.analysis.queries import _query, _ts
from heatpump.common.paths import image_root

RIDGE = 3.0
BANDS = [0.01, 2, 4, 6, 8]  # score ranges that are weighted evenly
MAX_WEIGHT = 8.0


def training_images(
    con: duckdb.DuckDBPyConnection, start: datetime, end: datetime
) -> list[tuple[datetime, str, float]]:
    """(time, path, frost score from pressure) of the images taken with the fan running.

    Images whose 15 s span also holds a reading below 20 Pa are left out: the
    fan was starting or stopping.
    """
    pid = frost.sensor_id(con, frost.PRESSURE_SENSOR)
    half = frost.PERIOD / 2
    rows = _query(
        con,
        "SELECT i.ts, i.path, avg(p.value) FROM images i JOIN readings p "
        f"ON p.ts >= i.ts - INTERVAL '{half} seconds' AND p.ts < i.ts + INTERVAL '{half} seconds' "
        f"WHERE p.sensor_id = {pid} AND p.quality = 0 AND i.camera_id = {frost.CAMERA_ID} "
        f"AND i.ts >= {_ts(start)} AND i.ts < {_ts(end)} "
        f"GROUP BY i.ts, i.path HAVING min(p.value) >= {frost.FAN_ON_PA} ORDER BY i.ts",
    )
    return [(ts, path, frost.pressure_to_frost(pa)) for ts, path, pa in rows]


def fit(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> dict:
    """Weighted ridge regression on standardised columns."""
    mean, scale = x.mean(axis=0), x.std(axis=0)
    scale[scale == 0] = 1.0
    z = (x - mean) / scale
    z_mean = np.average(z, axis=0, weights=w)
    y_mean = np.average(y, weights=w)
    zc = z - z_mean
    a = (zc * w[:, None]).T @ zc + RIDGE * np.eye(x.shape[1])
    coef = np.linalg.solve(a, (zc * w[:, None]).T @ (y - y_mean))
    return {
        "low": x.min(axis=0).tolist(),
        "high": x.max(axis=0).tolist(),
        "mean": mean.tolist(),
        "scale": scale.tolist(),
        "coef": coef.tolist(),
        "intercept": float(y_mean - z_mean @ coef),
    }


def predict(model: dict, x: np.ndarray) -> np.ndarray:
    x = np.clip(x, model["low"], model["high"])
    z = (x - np.array(model["mean"])) / np.array(model["scale"])
    return np.clip(z @ np.array(model["coef"]) + model["intercept"], 0, 10)


def weights(y: np.ndarray) -> np.ndarray:
    band = np.digitize(y, BANDS)
    counts = np.bincount(band, minlength=len(BANDS) + 1)
    w = counts.max() / counts[band]
    return np.minimum(w, MAX_WEIGHT)


def train(
    con: duckdb.DuckDBPyConnection,
    start: datetime,
    end: datetime,
    root: Path | None = None,
    out: Path = frost_image.MODEL_FILE,
) -> dict:
    root = root or image_root()
    names: list[str] | None = None
    xs, ys, days = [], [], []
    for ts, path, score in training_images(con, start, end):
        image = frost_image.read_image(root / path)
        if image is None:
            continue
        f = frost_image.image_features(image)
        names = names or list(f)
        xs.append([f[n] for n in names])
        ys.append(score)
        days.append(ts.astimezone().date())
    if len(xs) < 100:
        raise SystemExit(f"Only {len(xs)} usable training images; need at least 100")
    x, y, day = np.array(xs), np.array(ys), np.array(days)
    w = weights(y)
    print(f"{len(y)} training images from {len(set(days))} days")

    # Error on each day when it is predicted from the other days
    held_out = np.full(len(y), np.nan)
    for d in sorted(set(days)):
        test = day == d
        if test.all():
            break  # one day only: no held-out estimate
        held_out[test] = predict(fit(x[~test], y[~test], w[~test]), x[test])
    error = held_out - y
    report = {}
    if not np.isnan(error).any():
        report["mean_abs_error"] = round(float(np.abs(error).mean()), 2)
        print(f"Predicting each day from the others: mean error {report['mean_abs_error']} "
              "score points")
        edges = [0, *BANDS, 10.01]
        for lo, hi in itertools.pairwise(edges):
            sel = (y >= lo) & (y < hi)
            if sel.any():
                print(f"  score {lo:g} to {min(hi, 10):g}: {sel.sum():5d} images, "
                      f"mean error {np.abs(error[sel]).mean():.2f}, "
                      f"average prediction off by {error[sel].mean():+.2f}")

    model = {
        "version": f"{datetime.now().astimezone():%Y-%m-%d}",
        "trained_on": {
            "start": start.isoformat(), "end": end.isoformat(), "images": len(y), **report,
        },
        "features": names,
        **fit(x, y, w),
    }
    out.write_text(json.dumps(model, indent=1) + "\n")
    print(f"Wrote {out}")
    return model


def default_range() -> tuple[datetime, datetime]:
    return frost.HISTORY_START, datetime.now(UTC) + timedelta(seconds=1)
