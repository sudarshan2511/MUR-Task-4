"""
MUR Motorsports — Task 4: Endurance log energy (kWh) and behaviour insights.
Requires: pip install pandas numpy matplotlib
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from numpy import trapezoid as _trapz
except ImportError:
    from numpy import trapz as _trapz

try:
    from numpy import cumulative_trapezoid as _cumtrapz
except ImportError:
    _cumtrapz = None

# Column names (Motec export)
COL_TIME = "Time"
COL_BP = "Car Data Battery BatteryPower"
COL_V = "Car Data Battery PackInstantaneousVoltage"
COL_I = "Car Data Battery PackCurrent"
COL_SPEED = "Car Data Driver Speed"
COL_SOC = "Car Data Battery PackSOC"
COL_THROTTLE = "Car Data Driver ThrottlePressure"
COL_BRAKE_F = "Car Data Driver FrontBrakePressure"
COL_BRAKE_R = "Car Data Driver RearBrakePressure"
COL_RPM = "Car Data Motor MotorRPM"
COL_CMD_TQ = "Car Data Inverter InverterCMDTorque"
COL_INV_TEMP = "Car Data Inverter InverterTemp"
COL_MOT_TEMP = "Car Data Motor MotorTemp"

KWH_PER_J = 1.0 / 3.6e6
KM_H_TO_M_S = 1.0 / 3.6


def _trapz_compat(y: np.ndarray, x: np.ndarray | None = None, axis: int = -1) -> float:
    return float(_trapz(y, x, axis=axis))


def load_log(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, low_memory=False)
    for c in df.columns:
        if c == COL_TIME:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        else:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=[COL_TIME]).sort_values(COL_TIME).reset_index(drop=True)
    return df


def motec_power_to_watts(p_raw: pd.Series, v_for_scale: pd.Series) -> tuple[pd.Series, str]:
    """
    Motec often logs pack voltage/current in 0.1 V / 0.1 A; BatteryPower then matches V*I in those raw units.
    When median voltage LSB looks like 0.1 V (values ~4000), convert to watts by dividing by 100.
    """
    v_med = float(np.nanmedian(v_for_scale[np.isfinite(v_for_scale) & (v_for_scale > 50)]))
    if np.isfinite(v_med) and v_med > 500:
        note = "raw V*I scaled /100 (0.1 V and 0.1 A LSBs) to watts"
        return p_raw / 100.0, note
    return p_raw, "power treated as watts (no LSB scaling applied)"


def build_power_series(df: pd.DataFrame) -> tuple[np.ndarray, str]:
    """Returns power in watts and a description of the source."""
    bp = df[COL_BP].copy()
    vi_raw = df[COL_V] * df[COL_I]

    bp_valid = bp.notna().sum()

    if bp_valid > len(df) * 0.5:
        p_raw = bp.ffill().bfill()
        if p_raw.isna().all():
            p_raw = vi_raw
            base = "P = V*I; BatteryPower unusable"
        else:
            base = "Car Data Battery BatteryPower"
    else:
        p_raw = vi_raw
        base = "P = V*I; BatteryPower sparse"

    p_raw = p_raw.ffill(limit=50).bfill(limit=50).fillna(0.0)
    p_scaled, scale_note = motec_power_to_watts(p_raw, df[COL_V])
    desc = f"{base}; {scale_note}; gaps filled ffill/bfill"
    return p_scaled.to_numpy(dtype=np.float64), desc


def energy_kwh_joules(p_w: np.ndarray, t_s: np.ndarray) -> tuple[float, float, float]:
    """
    Trapezoidal integration. Returns (net_kwh, discharge_only_kwh, net_joules).
    """
    t_s = np.asarray(t_s, dtype=np.float64)
    p_w = np.asarray(p_w, dtype=np.float64)
    mask = np.isfinite(t_s) & np.isfinite(p_w)
    if mask.sum() < 2:
        return 0.0, 0.0, 0.0
    t_s = t_s[mask]
    p_w = p_w[mask]
    ej = _trapz_compat(p_w, t_s)
    ej_dis = _trapz_compat(np.maximum(p_w, 0.0), t_s)
    return ej * KWH_PER_J, ej_dis * KWH_PER_J, ej


def cumulative_distance_m(speed_kmh: np.ndarray, t_s: np.ndarray) -> np.ndarray:
    """Distance in metres from speed (km/h) via trapezoidal integration."""
    v = np.asarray(speed_kmh, dtype=np.float64) * KM_H_TO_M_S
    t_s = np.asarray(t_s, dtype=np.float64)
    v = np.nan_to_num(v, nan=0.0)
    if _cumtrapz is not None:
        d = _cumtrapz(v, t_s, initial=0.0)
        return d
    n = len(v)
    out = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        dt = t_s[i] - t_s[i - 1]
        if dt <= 0:
            continue
        out[i] = out[i - 1] + 0.5 * (v[i] + v[i - 1]) * dt
    return out


def lap_split_by_distance(
    speed_kmh: np.ndarray, t_s: np.ndarray
) -> tuple[int, float, bool]:
    """
    Returns (split_index_after_row, total_distance_m, used_time_fallback).
    Lap 1: rows 0..split-1, Lap 2: split..end-1 (split is first index of lap 2).
    """
    cum = cumulative_distance_m(speed_kmh, t_s)
    total = float(cum[-1]) if len(cum) else 0.0
    if total <= 1.0 or not np.isfinite(total):
        mid = len(t_s) // 2
        return mid, total, True
    half = 0.5 * total
    idx = int(np.searchsorted(cum, half))
    idx = max(1, min(idx, len(cum) - 1))
    return idx, total, False


def energy_per_segment(p_w: np.ndarray, t_s: np.ndarray, i0: int, i1: int) -> tuple[float, float]:
    """Energy for rows [i0, i1] inclusive using trapezoid on that slice."""
    sl = slice(i0, i1 + 1)
    return energy_kwh_joules(p_w[sl], t_s[sl])[:2]


def plot_insights(
    df: pd.DataFrame,
    p_w: np.ndarray,
    split_idx: int,
    total_dist_m: float,
    time_fallback: bool,
    out_dir: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = df[COL_TIME].to_numpy()
    speed = df[COL_SPEED].to_numpy()
    soc = df[COL_SOC].to_numpy() if COL_SOC in df.columns else None
    thr = df[COL_THROTTLE].to_numpy() if COL_THROTTLE in df.columns else None
    bf = df[COL_BRAKE_F].to_numpy() if COL_BRAKE_F in df.columns else None
    br = df[COL_BRAKE_R].to_numpy() if COL_BRAKE_R in df.columns else None
    cmd_tq = df[COL_CMD_TQ].to_numpy() if COL_CMD_TQ in df.columns else None

    t_split = t[split_idx] if split_idx < len(t) else t[-1]

    # Rolling mean power (0.5 s window)
    dt_med = float(np.median(np.diff(t))) if len(t) > 1 else 0.01
    win = max(3, int(round(0.5 / max(dt_med, 1e-6))))
    p_series = pd.Series(p_w)
    p_smooth = p_series.rolling(window=win, center=True, min_periods=1).mean().to_numpy()

    fig1, ax = plt.subplots(figsize=(11, 4))
    ax.plot(t, speed, color="C0", lw=0.8, label="Speed (km/h)")
    ax.axvline(t_split, color="red", ls="--", lw=1.2, label="Lap split (50% distance)" if not time_fallback else "Lap split (mid-time fallback)")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Speed (km/h)")
    ax.set_title("Speed vs time")
    ax.legend(loc="upper right")
    fig1.tight_layout()
    fig1.savefig(out_dir / "mur_speed_laps.png", dpi=150)
    plt.close(fig1)

    fig2, ax = plt.subplots(figsize=(11, 4))
    ax.plot(t, p_w, color="lightgray", lw=0.3, alpha=0.7, label="Battery power")
    ax.plot(t, p_smooth, color="C1", lw=1.0, label=f"Rolling mean (~{win * dt_med:.2f}s)")
    ax.axvline(t_split, color="red", ls="--", lw=1.0)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Power (W)")
    ax.set_title("Battery power vs time")
    ax.legend(loc="upper right")
    fig2.tight_layout()
    fig2.savefig(out_dir / "mur_power_time.png", dpi=150)
    plt.close(fig2)

    fig3, ax = plt.subplots(figsize=(8, 4))
    valid_p = p_w[np.isfinite(p_w)]
    ax.hist(valid_p, bins=60, color="steelblue", edgecolor="white", alpha=0.85)
    ax.set_xlabel("Power (W)")
    ax.set_ylabel("Samples")
    ax.set_title("Histogram of battery power")
    fig3.tight_layout()
    fig3.savefig(out_dir / "mur_power_hist.png", dpi=150)
    plt.close(fig3)

    if soc is not None and np.nanmax(soc) > 0:
        fig4, ax = plt.subplots(figsize=(11, 3.5))
        ax.plot(t, soc, color="green", lw=1.0)
        ax.axvline(t_split, color="red", ls="--", lw=1.0)
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("SOC (%)")
        ax.set_title("Battery state of charge")
        fig4.tight_layout()
        fig4.savefig(out_dir / "mur_soc.png", dpi=150)
        plt.close(fig4)

    if thr is not None and cmd_tq is not None:
        fig5, ax = plt.subplots(figsize=(6, 5))
        m = np.isfinite(thr) & np.isfinite(cmd_tq) & np.isfinite(p_w)
        sc = ax.scatter(thr[m], cmd_tq[m], c=p_w[m], cmap="viridis", s=4, alpha=0.35)
        plt.colorbar(sc, ax=ax, label="Power (W)")
        ax.set_xlabel("Throttle pressure")
        ax.set_ylabel("CMD torque")
        ax.set_title("Throttle vs commanded torque (colour = power)")
        fig5.tight_layout()
        fig5.savefig(out_dir / "mur_throttle_torque_power.png", dpi=150)
        plt.close(fig5)

    if bf is not None or br is not None:
        fig6, ax = plt.subplots(figsize=(11, 4))
        if bf is not None:
            ax.plot(t, bf, label="Front brake", lw=0.8)
        if br is not None:
            ax.plot(t, br, label="Rear brake", lw=0.8)
        ax.axvline(t_split, color="red", ls="--", lw=1.0)
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Pressure")
        ax.set_title("Brake pressures vs time")
        ax.legend()
        fig6.tight_layout()
        fig6.savefig(out_dir / "mur_brakes.png", dpi=150)
        plt.close(fig6)


def simple_regression_insight(p_w: np.ndarray, thr: np.ndarray, speed: np.ndarray) -> str:
    """OLS of power on throttle and speed (qualitative)."""
    m = np.isfinite(p_w) & np.isfinite(thr) & np.isfinite(speed)
    if m.sum() < 50:
        return "Not enough valid samples for regression."
    y = p_w[m]
    X = np.column_stack([np.ones(m.sum()), thr[m], speed[m]])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    y_hat = X @ beta
    ss_res = np.sum((y - y_hat) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return (
        f"Linear model P ~ {beta[0]:.0f} + {beta[1]:.1f}*throttle + {beta[2]:.1f}*speed (km/h); "
        f"R^2={r2:.3f} (qualitative fit only)."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Endurance log: kWh and plots")
    parser.add_argument(
        "csv",
        nargs="?",
        default=None,
        help="Path to Motec CSV (default: endurance_motec_export.csv next to this script)",
    )
    parser.add_argument(
        "-o",
        "--out-dir",
        default=None,
        help="Output directory for PNGs (default: same folder as CSV)",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    csv_path = Path(args.csv) if args.csv else script_dir / "endurance_motec_export.csv"
    if not csv_path.is_file():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.out_dir) if args.out_dir else csv_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    df = load_log(csv_path)
    t = df[COL_TIME].to_numpy()
    p_w, power_desc = build_power_series(df)

    net_kwh, dis_kwh, ej = energy_kwh_joules(p_w, t)
    speed = df[COL_SPEED].to_numpy() if COL_SPEED in df.columns else np.zeros(len(df))
    split_idx, total_dist_m, time_fallback = lap_split_by_distance(speed, t)

    n = len(t)
    i_lap2_start = split_idx
    kwh_l1, _ = energy_per_segment(p_w, t, 0, i_lap2_start - 1)
    kwh_l2, _ = energy_per_segment(p_w, t, i_lap2_start, n - 1)

    neg_frac = float(np.sum(p_w < 0) / max(len(p_w), 1))
    mean_p = float(np.nanmean(p_w))
    peak_p = float(np.nanmax(p_w))

    if COL_SPEED in df.columns:
        mean_spd = float(np.nanmean(df[COL_SPEED]))
    else:
        mean_spd = float("nan")

    print("=== Assumptions ===")
    print(f"- Power: {power_desc}")
    print("- Speed assumed km/h; distance = integral of v dt (trapezoidal).")
    if time_fallback:
        print("- Lap split: MID-TIME (50/50 by row) — distance integral unreliable.")
    else:
        print("- Lap split: index where cumulative distance reaches 50% of total (proxy for two equal laps).")
    print()
    print("=== Energy ===")
    print(f"Net energy (signed integral, regen reduces net): {net_kwh:.4f} kWh ({ej / 1e6:.3f} MJ)")
    print(f"Discharge-only (integral of max(P,0) dt):         {dis_kwh:.4f} kWh")
    print(f"Lap 1 (rows 0..{i_lap2_start - 1}):                {kwh_l1:.4f} kWh (net)")
    print(f"Lap 2 (rows {i_lap2_start}..{n - 1}):              {kwh_l2:.4f} kWh (net)")
    print(f"Sum lap1+lap2 (should match net):                  {kwh_l1 + kwh_l2:.4f} kWh")
    print()
    print("=== Run stats ===")
    print(f"Duration: {t[-1] - t[0]:.2f} s | Samples: {n}")
    print(f"Integrated distance (~): {total_dist_m / 1000:.3f} km")
    print(f"Mean speed: {mean_spd:.2f} km/h | Peak power: {peak_p:.0f} W | Mean power: {mean_p:.0f} W")
    print(f"Fraction of samples with P < 0 (regen): {neg_frac:.1%}")
    print()

    if COL_THROTTLE in df.columns and COL_SPEED in df.columns:
        print("=== Lightweight model ===")
        print(simple_regression_insight(p_w, df[COL_THROTTLE].to_numpy(), df[COL_SPEED].to_numpy()))
        print()

    print("=== Insights (from data) ===")
    insights = []
    if peak_p > 80000:
        insights.append(
            "Peak electrical power is high; short bursts dominate energy -- see power histogram and time trace."
        )
    if neg_frac > 0.05:
        insights.append("Non-trivial regen fraction — net kWh is materially lower than discharge-only kWh.")
    if COL_INV_TEMP in df.columns and COL_MOT_TEMP in df.columns:
        mx_inv = float(np.nanmax(df[COL_INV_TEMP]))
        mx_mot = float(np.nanmax(df[COL_MOT_TEMP]))
        insights.append(
            f"Thermal headroom check: peak inverter {mx_inv:.0f} C, motor {mx_mot:.0f} C (context: your limits)."
        )
    if not insights:
        insights.append("Review speed trace and power rolling mean for traction limits vs straight-line power.")
    for line in insights:
        print(f"- {line}")
    print()
    print(f"Figures written to: {out_dir.resolve()}")

    plot_insights(df, p_w, split_idx, total_dist_m, time_fallback, out_dir)


if __name__ == "__main__":
    main()
