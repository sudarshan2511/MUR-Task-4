"""
Advanced endurance analysis for MUR Task 4.

Focus areas:
1) Energy breakdown by operating mode
2) Regen effectiveness
3) Data quality audit
4) Wh/km by speed band
5) Event detection (hard accel / hard brake / regen windows)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

COL_TIME = "Time"
COL_BP = "Car Data Battery BatteryPower"
COL_V = "Car Data Battery PackInstantaneousVoltage"
COL_I = "Car Data Battery PackCurrent"
COL_SPEED = "Car Data Driver Speed"
COL_THROTTLE = "Car Data Driver ThrottlePressure"
COL_BRAKE_F = "Car Data Driver FrontBrakePressure"
COL_BRAKE_R = "Car Data Driver RearBrakePressure"

KWH_PER_J = 1.0 / 3.6e6
KM_H_TO_M_S = 1.0 / 3.6


def trapz_compat(y: np.ndarray, x: np.ndarray) -> float:
    try:
        return float(np.trapezoid(y, x))
    except AttributeError:
        return float(np.trapz(y, x))


def load_log(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, low_memory=False)
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=[COL_TIME]).sort_values(COL_TIME).reset_index(drop=True)
    return df


def power_series_watts(df: pd.DataFrame) -> tuple[np.ndarray, str]:
    bp = df[COL_BP] if COL_BP in df.columns else pd.Series(np.nan, index=df.index)
    vi_raw = (
        df[COL_V] * df[COL_I]
        if COL_V in df.columns and COL_I in df.columns
        else pd.Series(np.nan, index=df.index)
    )
    if bp.notna().sum() > len(df) * 0.5:
        p_raw = bp.ffill().bfill()
        base = "BatteryPower"
    else:
        p_raw = vi_raw.ffill().bfill()
        base = "V*I fallback"

    p_raw = p_raw.ffill(limit=50).bfill(limit=50).fillna(0.0)

    scale_note = "no scaling"
    if COL_V in df.columns:
        v = df[COL_V].to_numpy(dtype=float)
        v_med = float(np.nanmedian(v[np.isfinite(v) & (v > 50)]))
        if np.isfinite(v_med) and v_med > 500:
            p_raw = p_raw / 100.0
            scale_note = "scaled /100 from 0.1V*0.1A units"

    return p_raw.to_numpy(dtype=float), f"{base}; {scale_note}"


def contiguous_segments(mask: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return []
    splits = np.where(np.diff(idx) > 1)[0]
    starts = np.r_[idx[0], idx[splits + 1]]
    ends = np.r_[idx[splits], idx[-1]]
    segs = []
    for s, e in zip(starts, ends):
        if (e - s + 1) >= min_len:
            segs.append((int(s), int(e)))
    return segs


def kwh_for_mask(t: np.ndarray, p: np.ndarray, mask: np.ndarray) -> float:
    y = np.where(mask, p, 0.0)
    return trapz_compat(y, t) * KWH_PER_J


def build_mode_masks(df: pd.DataFrame, p_w: np.ndarray) -> dict[str, np.ndarray]:
    speed = (
        df[COL_SPEED].to_numpy(dtype=float)
        if COL_SPEED in df.columns
        else np.zeros(len(df), dtype=float)
    )
    thr = (
        df[COL_THROTTLE].to_numpy(dtype=float)
        if COL_THROTTLE in df.columns
        else np.zeros(len(df), dtype=float)
    )
    bf = (
        df[COL_BRAKE_F].to_numpy(dtype=float)
        if COL_BRAKE_F in df.columns
        else np.zeros(len(df), dtype=float)
    )
    br = (
        df[COL_BRAKE_R].to_numpy(dtype=float)
        if COL_BRAKE_R in df.columns
        else np.zeros(len(df), dtype=float)
    )
    brake = np.nan_to_num(np.maximum(bf, br), nan=0.0)
    speed = np.nan_to_num(speed, nan=0.0)
    thr = np.nan_to_num(thr, nan=0.0)

    hard_brake = (brake > 60) & (speed > 10)
    braking = (brake > 20) & (speed > 5)
    regen = p_w < -1000
    accel = (thr > 20) & (p_w > 3000) & ~braking
    cruise = (speed > 15) & (np.abs(p_w) <= 3000) & ~braking & ~accel
    idle = speed < 2
    other = ~(hard_brake | braking | regen | accel | cruise | idle)

    return {
        "hard_brake": hard_brake,
        "braking": braking & ~hard_brake,
        "regen": regen & ~(hard_brake | braking),
        "accel": accel,
        "cruise": cruise,
        "idle": idle,
        "other": other,
    }


def data_quality_report(df: pd.DataFrame) -> dict[str, object]:
    out: dict[str, object] = {}
    n = len(df)
    out["rows"] = n
    if n < 2:
        return out

    t = df[COL_TIME].to_numpy(dtype=float)
    dt = np.diff(t)
    out["time_non_monotonic_count"] = int(np.sum(dt <= 0))
    out["dt_mean_s"] = float(np.nanmean(dt))
    out["dt_std_s"] = float(np.nanstd(dt))
    out["dt_p99_s"] = float(np.nanpercentile(dt, 99))

    cols_check = [COL_TIME, COL_SPEED, COL_BP, COL_V, COL_I, COL_THROTTLE, COL_BRAKE_F, COL_BRAKE_R]
    missing_fraction = {}
    for c in cols_check:
        if c in df.columns:
            missing_fraction[c] = float(df[c].isna().mean())
    out["missing_fraction"] = missing_fraction

    # Stuck-sensor check: longest run of exact same value on key channels
    stuck = {}
    for c in [COL_SPEED, COL_BP, COL_THROTTLE]:
        if c not in df.columns:
            continue
        s = df[c].to_numpy(dtype=float)
        same = np.isclose(s[1:], s[:-1], equal_nan=False)
        max_run = 1
        run = 1
        for flag in same:
            if flag:
                run += 1
            else:
                if run > max_run:
                    max_run = run
                run = 1
        if run > max_run:
            max_run = run
        stuck[c] = int(max_run)
    out["max_constant_run_samples"] = stuck
    return out


def event_table(df: pd.DataFrame, p_w: np.ndarray) -> pd.DataFrame:
    t = df[COL_TIME].to_numpy(dtype=float)
    speed = np.nan_to_num(df[COL_SPEED].to_numpy(dtype=float), nan=0.0) if COL_SPEED in df.columns else np.zeros(len(df))
    thr = np.nan_to_num(df[COL_THROTTLE].to_numpy(dtype=float), nan=0.0) if COL_THROTTLE in df.columns else np.zeros(len(df))
    bf = np.nan_to_num(df[COL_BRAKE_F].to_numpy(dtype=float), nan=0.0) if COL_BRAKE_F in df.columns else np.zeros(len(df))
    br = np.nan_to_num(df[COL_BRAKE_R].to_numpy(dtype=float), nan=0.0) if COL_BRAKE_R in df.columns else np.zeros(len(df))
    brake = np.maximum(bf, br)

    dt_med = float(np.median(np.diff(t))) if len(t) > 1 else 0.01
    min_len = max(3, int(round(0.5 / max(dt_med, 1e-6))))

    masks = {
        "hard_accel": (thr > 70) & (p_w > 12000) & (brake < 10),
        "hard_brake": (brake > 60) & (speed > 10),
        "regen_window": p_w < -1500,
    }

    rows = []
    for label, mask in masks.items():
        for s, e in contiguous_segments(mask, min_len=min_len):
            duration = float(t[e] - t[s])
            rows.append(
                {
                    "event_type": label,
                    "start_s": float(t[s]),
                    "end_s": float(t[e]),
                    "duration_s": duration,
                    "mean_speed_kmh": float(np.nanmean(speed[s : e + 1])),
                    "mean_power_w": float(np.nanmean(p_w[s : e + 1])),
                    "peak_power_w": float(np.nanmax(p_w[s : e + 1])),
                }
            )
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["event_type", "start_s"]).reset_index(drop=True)
    return out


def speed_band_efficiency(df: pd.DataFrame, p_w: np.ndarray) -> pd.DataFrame:
    if COL_SPEED not in df.columns:
        return pd.DataFrame()
    t = df[COL_TIME].to_numpy(dtype=float)
    speed = np.nan_to_num(df[COL_SPEED].to_numpy(dtype=float), nan=0.0)
    speed_m_s = speed * KM_H_TO_M_S
    bins = [0, 10, 20, 30, 40, 60, 80, 120, 999]
    labels = ["0-10", "10-20", "20-30", "30-40", "40-60", "60-80", "80-120", "120+"]
    band_idx = pd.cut(speed, bins=bins, labels=labels, right=False)

    rows = []
    for label in labels:
        mask = np.asarray(band_idx == label)
        if mask.sum() < 2:
            continue
        e_kwh = kwh_for_mask(t, np.maximum(p_w, 0.0), mask)
        d_m = trapz_compat(np.where(mask, speed_m_s, 0.0), t)
        d_km = d_m / 1000.0
        wh_per_km = (e_kwh * 1000.0 / d_km) if d_km > 1e-6 else np.nan
        rows.append(
            {
                "speed_band_kmh": label,
                "time_s": float(mask.sum() * np.median(np.diff(t))),
                "distance_km": d_km,
                "discharge_energy_kwh": e_kwh,
                "wh_per_km": wh_per_km,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Advanced endurance KPI analysis")
    parser.add_argument("csv", nargs="?", default=None, help="Input CSV path")
    parser.add_argument("-o", "--out-dir", default=None, help="Output directory")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    csv_path = Path(args.csv) if args.csv else script_dir / "endurance_motec_export.csv"
    if not csv_path.is_file():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out_dir) if args.out_dir else csv_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    df = load_log(csv_path)
    t = df[COL_TIME].to_numpy(dtype=float)
    p_w, power_note = power_series_watts(df)

    # Top 1: mode energy breakdown
    masks = build_mode_masks(df, p_w)
    mode_rows = []
    for mode, mask in masks.items():
        e_net = kwh_for_mask(t, p_w, mask)
        e_dis = kwh_for_mask(t, np.maximum(p_w, 0.0), mask)
        time_s = float(np.sum(mask) * np.median(np.diff(t)))
        mode_rows.append(
            {"mode": mode, "time_s": time_s, "net_kwh": e_net, "discharge_kwh": e_dis}
        )
    mode_df = pd.DataFrame(mode_rows).sort_values("discharge_kwh", ascending=False)
    mode_df.to_csv(out_dir / "mur_mode_energy_breakdown.csv", index=False)

    # Top 2: regen effectiveness
    net_kwh = trapz_compat(p_w, t) * KWH_PER_J
    dis_kwh = trapz_compat(np.maximum(p_w, 0.0), t) * KWH_PER_J
    regen_kwh = trapz_compat(np.minimum(p_w, 0.0), t) * KWH_PER_J  # negative
    regen_abs_kwh = abs(regen_kwh)
    regen_effectiveness = (regen_abs_kwh / dis_kwh) if dis_kwh > 1e-9 else np.nan

    regen_summary = {
        "power_source": power_note,
        "net_kwh": net_kwh,
        "discharge_kwh": dis_kwh,
        "regen_kwh_signed": regen_kwh,
        "regen_kwh_abs": regen_abs_kwh,
        "regen_as_percent_of_discharge": float(regen_effectiveness * 100.0)
        if np.isfinite(regen_effectiveness)
        else None,
        "fraction_samples_p_lt_0": float(np.mean(p_w < 0)),
    }
    with open(out_dir / "mur_regen_summary.json", "w", encoding="utf-8") as f:
        json.dump(regen_summary, f, indent=2)

    # Top 3: data quality
    dq = data_quality_report(df)
    with open(out_dir / "mur_data_quality_report.json", "w", encoding="utf-8") as f:
        json.dump(dq, f, indent=2)

    # Honorable tier A: Wh/km by speed band
    band_df = speed_band_efficiency(df, p_w)
    band_df.to_csv(out_dir / "mur_speed_band_efficiency.csv", index=False)

    # Honorable tier B: event detection
    ev_df = event_table(df, p_w)
    ev_df.to_csv(out_dir / "mur_events.csv", index=False)

    # Console summary
    print("=== Advanced KPI Analysis Completed ===")
    print(f"Input: {csv_path}")
    print(f"Power source/scaling: {power_note}")
    print()
    print("Top-3 outputs:")
    print(f"- Mode energy breakdown: {out_dir / 'mur_mode_energy_breakdown.csv'}")
    print(f"- Regen summary:         {out_dir / 'mur_regen_summary.json'}")
    print(f"- Data quality report:   {out_dir / 'mur_data_quality_report.json'}")
    print()
    print("Honorable tier outputs:")
    print(f"- Speed-band efficiency: {out_dir / 'mur_speed_band_efficiency.csv'}")
    print(f"- Event detection:       {out_dir / 'mur_events.csv'}")
    print()
    print("Headline values:")
    print(f"- Net kWh: {net_kwh:.4f}")
    print(f"- Discharge kWh: {dis_kwh:.4f}")
    print(f"- Regen abs kWh: {regen_abs_kwh:.4f}")
    if np.isfinite(regen_effectiveness):
        print(f"- Regen/Discharge: {regen_effectiveness * 100.0:.2f}%")
    print(f"- Detected events: {len(ev_df)}")


if __name__ == "__main__":
    main()
