The parquet footer isn't written (live/unfinalized dataset — known single-writer issue noted in the code). I have enough from the schema and engine code. Here's the analysis.

                                                   ---

                                                      # 📊 Dataset vs. Full Rover Telemetry — Missing Context Audit

                                                                                                                   I compared our LeRobot v3 schema against the **two ground-truth telemetry sources**: the STM32 `ucp_rep_t` report packet (TIER 1 firmware) and the SDK `/data` endpoint (TIER 3).

                                                                                                                                               ## ✅ What we capture (18-dim `observation.state` + 2-dim `action`)
                                                             `linear.vel, angular.vel, battery, orientation, lat, lon, gps_signal, signal_level, vibration, lamp` (standard 10) + `voltage, current, accel.xyz, gyro.xyz` (our 6... actually 8 extras).

                                                                                                  ## 🔴 MISSING — available in hardware, NOT in our dataset

      | Field | Source | Why it matters for training |
                                                      |---|---|---|
                                                                   | **`mag[3]` (magnetometer x/y/z)** | `ucp_rep_t.mag`, `/data` `mags[]` | Absolute heading reference. We store derived `orientation.deg` but drop the raw mag vector → can't learn drift correction or recover true north. |
                                                                                                                                          | **`rpm[4]` (per-wheel RPM)** | `ucp_rep_t.rpm[4]`, `/data` `rpms[]` | **Biggest gap.** This is the *measured wheel odometry* — ground-truth proprioception. Critical for a VLA: lets the model learn "I commanded linear=0.6 but wheels stalled → I'm stuck." We only store *commanded* action, not *executed* wheel response. |
             | **`stop_switch` (e-stop status)** | `ucp_rep_t.stop_switch` | Safety state. Frames during an e-stop are anomalous — without this flag the trainer can't mask them. |
                              | **`error_code`** | `ucp_rep_t.error_code` | System fault state. Same masking value. |
                                                                                                                     | **`heading` (firmware-fused)** | `ucp_rep_t.heading` | The MCU's own IMU+mag fused heading — may differ from the SDK's `orientation`. |
                                                                                                         | **measured `angular.vel`** | (only commanded today) | Our dim 1 falls back to *commanded* angular because SDK rarely reports yaw rate — but raw `gyro.z` IS the measured yaw rate. We have it (dim 17) but it's redundant-encoded; fine. |
                                           | **full IMU history arrays** | `/data` `accels[]`,`gyros[]` (5-6 samples/frame @ higher rate) | We collapse to a single accel/gyro triplet per 10Hz frame. The SDK exposes a ~60Hz burst per poll → we throw away ~5× the IMU temporal resolution. |
                                                                                                                                           | **per-sample timestamps** in accel/gyro/mag/rpm arrays | `/data` | Sub-frame timing for the sensor bursts — lost. |

                                                                                                           ## 🟡 STRUCTURAL gaps (not a field, but a context loss)

             1. **Commanded ≠ executed.** Our `action` is only the *last command*. With no `rpm[4]`, the model can't see the action→outcome mismatch (slip, stall, collision). For a world-model / VLA this is the single most valuable missing signal.
                                                                                                  2. **`current` & `voltage` likely all-zero.** The SDK `/data` example doesn't expose `current`; `voltage` only comes from the UCP report which the cloud SDK may not surface. So dims 10-11 are probably padded zeros (couldn't confirm — parquet footer unwritten). **Worth verifying live** — if always 0, they're dead dimensions.
                                                                                                                             3. **No mission/checkpoint context.** SDK exposes `/checkpoints-list`, `latest_scanned_checkpoint`, distance-to-checkpoint. None is in the state vector → the policy is blind to goal/progress. For task-conditioned nav, GPS goal vector or checkpoint bearing would be high-value.
                                                                                                       4. **No intervention flag.** `/interventions/*` marks human-takeover periods. Those frames should be tagged (they're off-policy) — currently invisible.
                                                                                                         5. **Audio is a sidecar WAV**, not aligned into frames (LeRobot v3 limitation noted in code) — fine, but cross-modal training needs the alignment metadata, which lives only in `reasoning/events.sqlite` ECoT anchors.

                      ## 🟢 What's GOOD
                                       - First 10 dims exactly match the official `earthrover_mini_plus` / `lilkm/earthrover-navigation` layout → checkpoint transfer works.
                       - `action_age` is a smart addition (lets trainer drop smeared boundary frames).
                                                                                                      - ECoT reasoning log + per-episode export = strong VLA reasoning supervision.
                              - Superset design is LeRobot-safe (`[:10]` slice for standard models).

                                                                                                    ---

                                                                                                       ## Recommendation (priority order)
                                                                                                                                         1. **Add `rpm[4]`** → measured odometry. Highest training value. (SDK `/data` `rpms[]`.)
                                                                            2. **Add `mag[3]`** → raw heading reference.
                                                                                                                        3. **Add `stop_switch` + `error_code`** → frame-quality masking flags.
                                         4. **Verify `current`/`voltage`** are non-zero live; drop if dead.
                                                                                                           5. **Add goal context** (checkpoint bearing/distance or GPS goal delta) for task-conditioning.
                                                    6. Optionally **expand IMU** to keep the sub-frame burst arrays instead of collapsing to one triplet.

    Want me to:
               - **(a)** write a live probe that hits `/data` and dumps the *actual* exposed fields (to confirm rpm/mag/current are reachable before I extend the schema), or
                        - **(b)** draft the `_telemetry_to_state_vec` + `STATE_NAMES` patch to add rpm/mag/stop_switch (bumping state to ~26-dim, keeping `[:10]` compatibility)? 

