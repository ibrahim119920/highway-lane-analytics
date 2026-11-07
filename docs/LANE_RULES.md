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
  - name: lane_left
    index: 1
    role: normal
  - name: lane_center
    index: 2
    role: normal
  - name: lane_right
    index: 3
    role: overtaking
```

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

The default configuration uses right-hand traffic assumptions (keep left, overtake on the
right). Adapt the lane order and roles to the actual road.

```yaml
hogging:
  enabled: true
  retrigger_after_seconds: 0
  rules:
    - name: heavy_vehicle_in_overtaking_lane
      lane_roles: [overtaking]
      classes: [truck, bus]
      min_dwell_seconds: 3.0
      severity: high
      description: "Truk/bus menggunakan lajur overtaking melebihi batas waktu."

    - name: prolonged_overtaking_lane
      lane_roles: [overtaking]
      classes: [car]
      min_dwell_seconds: 12.0
      severity: medium
      description: "Mobil bertahan di lajur overtaking melebihi batas waktu."
```

What this means:

- A truck or bus in the overtaking lane for 3 continuous seconds → `high` indication.
- A car in the overtaking lane for 12 continuous seconds → `medium` indication.
- A car in the overtaking lane for 8 seconds → allowed, no event.
- Any vehicle in `lane_left` or `lane_center` → no indication from these rules.

### Restriction examples

Prohibit a class from a specific lane:

```yaml
- name: truck_in_left_lane
  lanes: [lane_left]
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
