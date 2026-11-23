# Frost sensor: how it was built and how good it is

Written 2026-11-22, when the `frost` sensor was added. It records what was
asked for, what was built, the measurements behind the image function, and what
is still unverified. How to run things is in the main README ("Frost score") and
`deploy/README.md` ("hp-frost"); this file is the background.

All numbers here come from the development machine, using a copy of the
mini-PC's database and images covering 2026-11-16 1:42 PM to 2026-11-21 5:22 PM
Alaska Standard Time. Nothing has been run on the mini-PC yet.

## What was asked for

From `INSTRUCTIONS.md` and the discussion that followed:

- A derived sensor, `frost`, on a 0 to 10 scale, stored as a sensor (not in
  `image_metrics`) so it can be graphed in hp-web.
- Fan running (`coil_pressure` above 20 Pa): a score every 15 s from the average
  `coil_pressure` over the 15 s centred on the score's time. 23 Pa or less is 0,
  80 Pa or more is 10, linear between. Readings below 20 Pa are left out of the
  average.
- Fan not running: a score for every stored coil image, at the image's time,
  from an image function fitted to the fan-on images and their pressure scores.
- If there is no coil pressure reading, score the image.
- A way to produce scores for history from 2026-11-16 1:42 PM AKST, and scores
  on an ongoing basis, from a separate `hp-frost` service.

`coil_pressure` (sensor_id 5) is currently a Setra sensor, not the DG-700. It is
stored on change with a 15 s maximum interval, so every 15 s span holds at least
one reading while the collector runs.

## What was built

| Piece | Where |
|---|---|
| Pressure score, selection of fan-off images, `compute`, `store`, `backfill` | `src/heatpump/analysis/frost.py` |
| Image features and scoring (OpenCV, numpy) | `src/heatpump/analysis/frost_image.py` |
| Fitted numbers of the image function | `src/heatpump/analysis/frost_model.json` |
| Fitting the image function | `src/heatpump/analysis/frost_train.py` |
| Ongoing service | `src/heatpump/frost/main.py` (`hp-frost`), `deploy/hp-frost.service` |
| Commands | `hp frost-backfill`, `hp frost-train` in `src/heatpump/cli.py` |
| Tests | `tests/test_frost.py` (9 tests, against a real `hp-db`) |
| Dependencies | new `frost` extra (OpenCV) in `pyproject.toml` |

### Rules as implemented

- **Pressure score.** Each good reading of 20 Pa or more is assigned to the
  15 s clock time (:00, :15, :30, :45) whose span [t - 7.5 s, t + 7.5 s) holds
  it. The score at t is from the plain average of those readings. A span with
  no reading of 20 Pa or more gets no pressure score.
- **Image score.** An image is scored unless the newest good pressure reading
  at or before it is 20 Pa or more and no older than 60 s. So an image is
  scored when the fan is off, and also when the pressure sensor is silent.
- **Near a fan stop or start** both kinds of score can appear within a few
  seconds of each other. That is intended.
- Scores are rounded to 0.01 and stored in `readings` with quality 0. The
  sensor is registered as `frost`, kind `analog`, unit `frost 0-10`.

### History and ongoing scores share one function

`frost.compute(con, start, end)` returns every score with a time in
[start, end). `hp frost-backfill` calls it in 6-hour chunks, deleting a chunk's
old frost rows just before storing the new ones, so it can be re-run.
`hp-frost` calls it every 5 s for [where it left off, now - 20 s). Where it
left off is the newest stored frost score, read from the database at start and
after any database error. On a database with no frost scores it starts from now.

`hp-frost` keeps no spool file. A score that could not be stored is computed
again.

## The image function

### What the pictures show

The clean coil is blue. Frost turns it white, starting in vertical stripes and
filling in. By a pressure score of about 4 the coil already looks mostly white;
beyond that the visible change is mainly that the horizontal fin lines blur as
frost fills the gaps. During defrost (fan off) the coil goes back to blue over
a few minutes, which is exactly the range the image score has to cover.

The camera is lit day and night and the pictures look alike at all hours. It
was moved at least once (2026-11-21), shifting the coil down by about a tenth
of the picture height.

### Features

The JPG is read at half size (1280 x 960). The region used is rows 32% to 64%
and columns 8% to 92% of the picture, which is inside the coil for both camera
positions. Pixels darker than 0.25 (grille wires, the dark band in the middle)
are left out of the colour statistics. From the region, 44 numbers:

- saturation, blueness ((B - R) / (B + G + R)) and brightness: mean, standard
  deviation and the 10th, 25th, 50th, 75th, 90th percentiles of each
- share of pixels with saturation below 0.15, 0.25, 0.35 and 0.5
- share of dark pixels
- mean row-to-row change in grey level (fin-line sharpness) and mean
  column-to-column change
- mean saturation and fin-line sharpness in each of 8 vertical strips

Average values by pressure score, which show where colour stops helping:

| Pressure score | Images | Mean saturation | Fin-line sharpness |
|---|---|---|---|
| 0 | 192 | 0.53 | 0.151 |
| 0 to 1 | 598 | 0.51 | 0.168 |
| 1 to 2 | 395 | 0.44 | 0.187 |
| 2 to 3 | 248 | 0.35 | 0.203 |
| 3 to 4 | 231 | 0.29 | 0.206 |
| 4 to 5 | 198 | 0.24 | 0.188 |
| 5 to 6 | 112 | 0.18 | 0.152 |
| 6 to 7 | 63 | 0.16 | 0.138 |
| 7 to 8 | 34 | 0.15 | 0.128 |
| 8 to 10 | 11 | 0.14 | 0.111 |

Saturation flattens above a score of 5; sharpness keeps falling, which is why
the texture features are kept.

### Training data

- 7,353 images from the start time on; 2,199 had the fan running.
- 117 of those were dropped because their 15 s span also held a reading below
  20 Pa (fan starting or stopping), leaving 2,082.
- Labels are heavily weighted to light frost: half are below 1.5, and only 11
  are above 8. Only 50 pressure readings in the whole period reached 80 Pa.

### Model

Ridge regression (strength 3) on the standardised features, solved with numpy.
Images are weighted to even out the score ranges 0, 0 to 2, 2 to 4, 4 to 6,
6 to 8 and 8 to 10, up to a weight of 8. Before scoring, each feature is limited
to the range it had in the training images. The result is limited to 0 to 10.

The limiting was added after a test with a plain blue picture scored 9: with no
fin lines at all, the straight-line fit ran far outside anything it was fitted
on. With the limit, plain blue scores 0 and plain white 8.5, and accuracy on
real images is unchanged.

### Accuracy

Measured by leaving out one day at a time and predicting it from the other
five. Images a minute apart are nearly identical, so a random split would look
better than it is.

Final model:

| Pressure score | Images | Mean error | Average prediction off by |
|---|---|---|---|
| 0 | 192 | 0.39 | +0.39 |
| 0 to 2 | 993 | 0.60 | +0.12 |
| 2 to 4 | 479 | 0.75 | -0.13 |
| 4 to 6 | 310 | 0.90 | +0.31 |
| 6 to 8 | 97 | 0.88 | -0.24 |
| 8 to 10 | 11 | 1.66 | -1.66 |
| all | 2,082 | 0.68 | |

Per held-out day the mean error ranged from about 0.4 to 1.2; 2026-11-21, the
day the camera had moved, was the worst.

Alternatives tried during development (same test; these used scikit-learn and
are not in the repo):

| Model | Mean error | Note |
|---|---|---|
| Mean saturation only | 0.87 | about 4 points low above a score of 8 |
| 6 features, ridge | 0.79 | |
| 6 features with squared terms | 0.68 | 2.8 low above 8; extrapolates badly |
| 13 features, ridge, weighted | 0.78 | |
| Gradient boosting, all features | 0.74 | |
| Colour features only (no texture), weighted | 0.67 | worse above a score of 6 (1.14 against 0.95) |
| **All 44 features, ridge, weighted** | **0.68** | chosen |

All of them land between 0.65 and 0.9. My reading is that the remaining error
is mostly that pressure and appearance are not the same thing (frost inside the
coil is not visible), rather than something a better model would remove. That
is an interpretation, not something measured.

### Checks on the fan-off images

The image score is only ever used when the fan is off, where there is no
pressure score to compare with. Two indirect checks, from the backfilled scores:

- **Fan stops.** 44 times a pressure score of 1 or more was followed within
  75 s by an image score. The image score differed by 1.0 on average (median
  -0.17). By pressure score before the stop: 1 to 3, median -0.97; 3 to 5,
  +0.02; 5 to 7, +0.31; 7 to 10, -1.01. The image is up to a minute later and
  often already in defrost, so some of the drop is real melting.
- **Fan starts.** 121 times an image score was followed within 75 s by a
  pressure score. They differed by 0.42 on average (median -0.13).
- **Unexpected rises.** Of 5,153 fan-off images, 4 scored more than 2 points
  above the last pressure score before them, and 31 more than 1 point above.
  I did not look at those pictures individually.

One example of each handover (2026-11-20): the pressure score ran 4.9 up to
9:33:00, then the images at 9:33:24 and 9:34:24 scored 4.6 and 4.3, and the
score then held near 4.3 while the unit was idle.

## What the backfill produced

On the copy: 13,987 scores from 2026-11-16 1:42 PM to 2026-11-21 5:22 PM, in
about 4 minutes. Image scoring takes about 50 ms per image on the development
machine. Fitting the model (`hp frost-train`) took about 1.5 minutes.

## Known limits and things not verified

- **Heavy frost reads low** from images (about 1.7 low above a score of 8),
  from only 11 training images. More data with heavy frost and a re-fit should
  help.
- **Camera position.** The region is fixed. A move larger than the one already
  seen needs new `ROI_ROWS` / `ROI_COLS` values in `frost_image.py` and a
  re-fit.
- **Conditions not in the data**: snow on the grille or coil, a fogged or
  blocked lens, a failed light. Features are limited to their training range,
  which bounds the damage but does not make such scores right.
- **After an `hp-db` outage**, `hp-sensors` and `hp-camera` reload what they
  spooled. If `hp-frost` reaches that stretch first, its scores there are
  incomplete. `hp frost-backfill --start <time>` repairs it.
- **Backfill while `hp-frost` runs** can store the same recent scores twice.
  Stop the service during a backfill.
- **Sensor id.** `frost` takes the next free `sensor_id`. If `hp-sensors`
  registered a new channel in the same instant the two could collide; this is
  not guarded against.
- **hp-web.** The query layer returns `frost` like any sensor (checked), but
  the graph has not been looked at in a browser. Image scores more than
  `max_gap_seconds` (300) apart should show as separate points rather than a
  line; images are currently 60 s apart, so this does not arise yet.
- **Mini-PC.** Not installed there. Scoring speed on the N150 is not measured.
- **The 20 Pa edge.** A reading of exactly 20 Pa counts as fan running.

## Re-fitting later

```bash
uv run --env-file config/hp-db.env hp frost-train            # rewrites frost_model.json
sudo systemctl stop hp-frost
uv run --env-file config/hp-db.env hp frost-backfill         # apply it to stored scores
sudo systemctl start hp-frost
```

`hp frost-train` prints the same leave-one-day-out table as above, so a new fit
can be compared with this one before it is used.
