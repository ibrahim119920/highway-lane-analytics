# Lane Rules

## What a rule means

A lane-hogging rule is a **configuration-level indication**:

```text
vehicle class + lane (name or role) + continuous dwell time >= threshold
```

If no rule matches, the lane usage is treated as **allowed usage**. A matching rule does
not prove a legal violation; it flags behaviour for review.

The tracker emits at most one indication per `(track, lane stint, rule)` by default.
`hogging.retrigger_after_seconds` can allow repeated indications during a long stint.

## Lanes and roles

Each lane has a `name`, optional `index`, optional `direction`, and a `role`:

```yaml
lanes:
  - name: nb_lane_1
    index: 1
    direction: northbound
    role: normal
  - name: nb_lane_2
    index: 2
    direction: northbound
    role: normal
  - name: nb_lane_3
    index: 3
    direction: northbound
    role: overtaking
  - name: sb_lane_1
    index: 1
    direction: southbound
    role: normal
  # ...
```

Lane `index` is scoped **per direction**: both carriageways may define lanes 1, 2, 3.
Use `lane_name` as the unique key in downstream systems.

`role` is free text, but keep it stable because rules reference it. Suggested values:

| Role | Meaning | Example use |
| --- | --- | --- |
| `normal` | General-purpose lane. | No hogging rule, or long-dwell rule only. |
| `overtaking` | Lane intended for passing. | Heavy vehicles banned; cars limited to a dwell threshold. |
| `restricted` | Lane with a class restriction. | Truck/bus prohibited; special vehicles only. |
| `shoulder` | Emergency shoulder. | Any vehicle dwell > a few seconds is an indication. |

Rules can target a role (`lane_roles`) or explicit lane names (`lanes`). Use roles when
the same rule should apply to several lanes; use names for camera-specific exceptions.

## Example rule set

The shipped profile targets the median-side lane of a left-hand-traffic carriageway
(overtake on the right):

```yaml
hogging:
  enabled: true
  retrigger_after_seconds: 0
  rules:
    - name: heavy_vehicle_in_overtaking_lane
      lane_roles: [overtaking]
      classes: [truck, bus]
      min_dwell_seconds: 0.4
      severity: high
      description: "Truk/bus berada di lajur cepat (median) - indikasi pelanggaran lajur."

    - name: prolonged_overtaking_lane
      lane_roles: [overtaking]
      classes: [car]
      min_dwell_seconds: 2.0
      severity: medium
      description: "Mobil bertahan di lajur cepat (median) hampir sepanjang lintasan pandang."
```

What this means for the shipped camera:

- A truck or bus in the overtaking lane for 0.4 continuous seconds → `high` indication.
- A car in the overtaking lane for 2.0 continuous seconds → `medium` indication.
- Any vehicle in `nb_lane_1`/`nb_lane_2` or `sb_lane_1`/`sb_lane_2` → no indication from
  these rules.

> These thresholds are deliberately small because the camera is short-baseline; see
> [Visibility window](#visibility-window-matters-more-than-the-clock) below.

### Restriction examples

Prohibit a class from a specific lane:

```yaml
- name: truck_in_left_lane
  lanes: [nb_lane_1]
  classes: [truck]
  min_dwell_seconds: 2.0
  severity: high
```

Flag any vehicle on the shoulder:

```yaml
- name: shoulder_occupancy
  lane_roles: [shoulder]
  classes: []
  min_dwell_seconds: 5.0
  severity: high
```

`classes: []` (or omitting it) means all classes.

### Multiple matching rules

If several rules match, all are evaluated and can emit independently. The annotator shows
the most severe active rule. Avoid overlapping rules with contradictory thresholds unless
that is intentional.

## Visibility window matters more than the clock

A dwell threshold only means something relative to how long the camera can see a vehicle.
Estimate the window before choosing thresholds:

1. Measure the lane width in pixels near the bottom and near the top of the analysis zone.
   The ratio is the distance ratio (for a flat road): `d_far / d_near ≈ w_near / w_far`.
2. Multiply by a plausible near distance (camera height and pitch) to get the zone length.
3. Divide by typical speed to get the observation window.

For the shipped Tj. Priok camera the zone runs from the far detection limit to the bottom
of the frame. At highway speed a vehicle is visible for roughly 0.5-3 s depending on the
lane and direction. On the five-minute sample clip the measured dwell was:

| lane | stints | min | median | max |
| --- | --- | --- | --- | --- |
| `nb_lane_1` | 10 | 1.67 s | 3.11 s | 6.89 s |
| `nb_lane_2` | 23 | 0.11 s | 1.00 s | 6.78 s |
| `nb_lane_3` | 12 | 0.22 s | 1.83 s | 3.00 s |
| `sb_lane_1` | 13 | 0.11 s | 1.00 s | 4.22 s |
| `sb_lane_2` | 14 | 0.11 s | 1.28 s | 2.78 s |
| `sb_lane_3` | 14 | 0.11 s | 0.50 s | 2.78 s |

So a 2.0 s threshold in `nb_lane_3`/`sb_lane_3` means "stayed in the overtaking lane for
most of the pass", not "hogged for a long time". A camera that sees 300 m of road would
need thresholds 10-20x larger for the same behaviour.

Also keep the polygons inside the frame. At the very bottom of a perspective view a
near-field box can be wider than a lane and its bottom-centre anchor becomes unstable;
the shipped profile stops at the frame edge (`y=1.0`) but the lane-1 lines stop earlier,
where those lanes leave the image.


## Choosing thresholds

A reasonable calibration process:

1. Run the pipeline with hogging disabled or with generous thresholds.
2. Inspect `lane_stints.csv` and `lane_usage.csv`.
3. Look at the dwell distribution for each lane × class.
4. Set the threshold above normal usage but below the behaviour you want to flag.
5. Re-run and review `hogging_events.csv` against the video.

Example: if cars normally spend 3-8 seconds in the overtaking lane while passing, a
threshold of 12 seconds flags only long occupancy. If trucks are not allowed at all, a
short threshold such as 2-3 seconds avoids flagging a brief lane-straddle during a lane
change.

### Hysteresis and boundary cases

- `occupancy.min_lane_frames` (default 2) requires two consecutive observations before a
  lane change is committed. Increase it for noisy detectors or wide lane dividers.
- `occupancy.anchor: bottom_center` reduces divider flicker in perspective views.
- Dwell starts at the first observation of the new lane, not at the confirmation frame.
- A vehicle already transitioning to another lane is not evaluated for hogging during the
  pending transition.

### Retriggering

```yaml
hogging:
  retrigger_after_seconds: 30
```

With `0`, one indication per stint/rule. With a positive value, a vehicle that remains in
the lane can be flagged again after the interval. Use this for long-dwell alerting; keep
`0` for clean event counts.

## Avoiding false positives

| Risk | Mitigation |
| --- | --- |
| Vehicle straddles a divider | `bottom_center` anchor, `min_lane_frames` ≥ 2, small gap between polygons. |
| Stop-and-go congestion | Overtaking rules may not make sense in congestion; disable per camera or raise thresholds. |
| Roadworks / temporary lane shifts | Recalibrate polygons for that period. |
| Occlusion / ID switch | Review `lane_stints.csv`; a track restart looks like a new vehicle. |
| Wrong lane role | Verify `lanes[].role` matches the rules and the actual road. |
| Polygon overlap | Overlaps are allowed, but the smallest area wins; keep divider gaps clean. |
| Detection noise | Raise `model.conf`, use a larger model, or raise `min_lane_frames`. |

## Legal and ethical note

Lane-hogging output is an **indication** for analytics and research. It is not a legal
determination and must not be used as the sole basis for enforcement. Thresholds and lane
roles must be reviewed against the road's signage and applicable regulations. Do not use
this project for face recognition or individual identification.
