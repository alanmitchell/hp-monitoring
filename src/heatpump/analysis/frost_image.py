"""Frost score (0 to 10) from a picture of the evaporator coil.

The clean coil is blue and frost is white, so the score comes mostly from how
much colour is left in the middle of the picture, plus how sharp the fin lines
still are (heavy frost fills the gaps between fins). `image_features` turns a
picture into numbers, and a linear model fitted by `frost_train.py` turns those
into a score. The fitted numbers are in `frost_model.json` next to this file.

Needs OpenCV (the `frost` or `camera` extra).
"""

import json
from functools import cache
from pathlib import Path

import cv2
import numpy as np

MODEL_FILE = Path(__file__).with_name("frost_model.json")

# The part of the picture that is used, as fractions of its height and width.
# It lies inside the coil for every camera position seen so far (the camera has
# been moved up and down by about a tenth of the picture height).
ROI_ROWS = (0.32, 0.64)
ROI_COLS = (0.08, 0.92)
# Pixels darker than this (grille wires, shadows) are left out of the colour statistics
MIN_BRIGHTNESS = 0.25
COLUMNS = 8  # the region is also summarised in this many vertical strips


def read_image(path: Path) -> np.ndarray | None:
    """Load a JPG at half size, which is what the features were fitted on."""
    return cv2.imread(str(path), cv2.IMREAD_REDUCED_COLOR_2)


def image_features(image: np.ndarray) -> dict[str, float]:
    """Colour and texture statistics of the coil region of a BGR picture."""
    h, w = image.shape[:2]
    roi = image[
        int(ROI_ROWS[0] * h) : int(ROI_ROWS[1] * h), int(ROI_COLS[0] * w) : int(ROI_COLS[1] * w)
    ].astype(np.float32) / 255
    blue, green, red = roi[..., 0], roi[..., 1], roi[..., 2]
    value = roi.max(axis=2)  # brightness
    sat = (value - roi.min(axis=2)) / (value + 1e-3)  # 0 = white or grey, 1 = pure colour
    blueness = (blue - red) / (blue + green + red + 1e-3)
    gray = (blue + green + red) / 3

    lit = value > MIN_BRIGHTNESS
    if lit.sum() < 1000:  # a very dark picture: use every pixel
        lit = np.ones_like(lit)

    f = {"dark_frac": 1 - float(lit.mean())}
    for name, channel in (("S", sat), ("b", blueness), ("V", value)):
        x = channel[lit]
        f[f"{name}_mean"] = float(x.mean())
        f[f"{name}_std"] = float(x.std())
        for q in (10, 25, 50, 75, 90):
            f[f"{name}_p{q}"] = float(np.percentile(x, q))
    x = sat[lit]
    for limit in (0.15, 0.25, 0.35, 0.5):
        f[f"S_lt{limit}"] = float((x < limit).mean())  # share of nearly white pixels
    # Fins run across the picture, so their lines show as change from row to row
    f["gy"] = float(np.abs(np.diff(gray, axis=0)).mean())
    f["gx"] = float(np.abs(np.diff(gray, axis=1)).mean())
    for i, cols in enumerate(np.array_split(np.arange(sat.shape[1]), COLUMNS)):
        f[f"Scol{i}"] = float(sat[:, cols].mean())
        f[f"gycol{i}"] = float(np.abs(np.diff(gray[:, cols], axis=0)).mean())
    return f


@cache
def load_model() -> dict:
    return json.loads(MODEL_FILE.read_text())


def score_features(features: dict[str, float], model: dict | None = None) -> float:
    """Frost score from `image_features` output, limited to 0 to 10."""
    model = model or load_model()
    total = model["intercept"]
    for name, low, high, mean, scale, coef in zip(
        model["features"], model["low"], model["high"], model["mean"], model["scale"],
        model["coef"], strict=True,
    ):
        # Kept inside the range seen in training, where the straight-line fit holds
        x = min(high, max(low, features[name]))
        total += coef * (x - mean) / scale
    return float(min(10.0, max(0.0, total)))


def image_to_frost(path: Path) -> float | None:
    """Frost score of the JPG at `path`, or None if it cannot be read."""
    image = read_image(path)
    if image is None:
        return None
    return score_features(image_features(image))
