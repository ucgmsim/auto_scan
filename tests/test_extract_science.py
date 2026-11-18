"""Tests of the science extractors on a small synthetic event (offline, fast).

The synthetic event: a hypocentre at (-43, 172, 10 km), real stations on a
spiral 8-100 km away, simulated IMs from a simple attenuation model, observed IMs
equal to the simulated ones times lognormal noise (sigma 0.3), a GMM likewise,
and broadband waveforms whose S wave arrives at R / 3.5 km/s. Each test plants
one bug and asserts the metric that should expose it.
"""

import json
import math
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd
import pytest
import xarray as xr

from auto_scan import db as scandb
from auto_scan.extract import science, science_io, science_waveforms

EVENT = "2026p000001"
HYPO = (-43.0, 172.0, 10.0)
PERIODS = [0.01, 0.1, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0]
ORIGIN = pd.Timestamp("2026-01-01T00:00:00Z")
DT = 0.01
NT = 6000


def _offset(lat: float, lon: float, east_km: float, north_km: float) -> tuple[float, float]:
    return lat + north_km / 110.57, lon + east_km / (111.32 * math.cos(math.radians(lat)))


def _stations(n: int = 24) -> pd.DataFrame:
    rows = []
    for i in range(n):
        r = 8 + 92 * i / (n - 1)
        az = 2.399963 * i  # golden angle: spread around the source
        lat, lon = _offset(HYPO[0], HYPO[1], r * math.sin(az), r * math.cos(az))
        rows.append({"sta": f"S{i:02d}", "lat": lat, "lon": lon, "rrup": r, "vs30": 200 + 40 * ((i * 7) % 20)})
    return pd.DataFrame(rows).set_index("sta")


def _model(rrup: float, period: float | None) -> float:
    """A smooth attenuation model in g (PGA for None)."""
    shape = 1.0 if period is None else 2.0 * math.exp(-((math.log(period / 0.2)) ** 2) / 4)
    return 0.05 * shape * (rrup / 10.0) ** -1.2


def _sim_table(stations: pd.DataFrame, scale: Callable[[float | None], float] = lambda p: 1.0) -> pd.DataFrame:
    out = pd.DataFrame(index=stations.index)
    out["PGA"] = [_model(r, None) * scale(None) for r in stations["rrup"]]
    out["PGV"] = out["PGA"] * 60.0 * scale(-1.0)
    for p in PERIODS:
        out[science_io.im_name(p)] = [_model(r, p) * scale(p) for r in stations["rrup"]]
    return out


def write_imdb(
    path: Path, stations: pd.DataFrame, sim: pd.DataFrame, gmm: pd.DataFrame | None, magnitude: float = 5.0
) -> None:
    """A minimal IM database in the imdb schema (only the tables and columns read)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE events (event_int_id INTEGER, event_id VARCHAR);
        CREATE TABLE realisations (rel_int_id INTEGER, rel_id VARCHAR, event_int_id INTEGER, magnitude FLOAT,
            rake FLOAT, hypo_lat FLOAT, hypo_lon FLOAT, hypo_depth FLOAT);
        CREATE TABLE periods (period_index INTEGER, period DOUBLE);
        CREATE TABLE sites (site_int_id INTEGER, site_id VARCHAR, lat FLOAT, lon FLOAT, vs30 FLOAT,
            is_real BOOLEAN);
        CREATE TABLE site_event (site_int_id INTEGER, event_int_id INTEGER, rrup FLOAT, rjb FLOAT);
        CREATE TABLE records (record_int_id BIGINT, event_int_id INTEGER, rel_int_id INTEGER,
            site_int_id INTEGER, component VARCHAR, kind VARCHAR, gmm_key VARCHAR);
        CREATE TABLE psa_ims (record_int_id BIGINT, pSA FLOAT[], pSA_sigma FLOAT[]);
        CREATE TABLE scalars_ims (record_int_id BIGINT, PGA FLOAT, PGV FLOAT);
    """)
    con.execute("INSERT INTO events VALUES (0, ?)", [EVENT])
    con.execute("INSERT INTO realisations VALUES (0, ?, 0, ?, 90, ?, ?, ?)", [EVENT, magnitude, *HYPO])
    con.executemany("INSERT INTO periods VALUES (?, ?)", [[i + 1, p] for i, p in enumerate(PERIODS)])
    rid = 0
    for k, (sta, row) in enumerate(stations.iterrows()):
        con.execute(
            "INSERT INTO sites VALUES (?, ?, ?, ?, ?, ?)",
            [k, sta, row["lat"], row["lon"], row["vs30"], bool(row.get("is_real", True))],
        )
        con.execute("INSERT INTO site_event VALUES (?, 0, ?, ?)", [k, row["rrup"], row["rrup"]])
        for kind, table in (("simulated", sim), ("gmm", gmm)):
            if table is None or sta not in table.index:
                continue
            values = table.loc[sta]
            con.execute(
                "INSERT INTO records VALUES (?, 0, 0, ?, 'rotd50', ?, ?)",
                [rid, k, kind, "NSHM2022" if kind == "gmm" else None],
            )
            con.execute(
                "INSERT INTO psa_ims VALUES (?, ?, NULL)",
                [rid, [float(values[science_io.im_name(p)]) for p in PERIODS]],
            )
            if kind == "simulated":
                con.execute(
                    "INSERT INTO scalars_ims VALUES (?, ?, ?)", [rid, float(values["PGA"]), float(values["PGV"])]
                )
            rid += 1
    con.close()


def write_observed(
    directory: Path, stations: pd.DataFrame, obs: pd.DataFrame, bad: dict[str, dict[str, Any]] | None = None
) -> None:
    """A flatfile directory: rotd50 table, phase arrivals and source table."""
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    for sta, row in stations.iterrows():
        rec = {
            "record_id": f"{EVENT}_{sta}_HN_20",
            "sta": sta,
            "loc": 20,
            "chan": "HN",
            "r_rup": row["rrup"],
            "Vs30": row["vs30"],
            "sta_lat": row["lat"],
            "sta_lon": row["lon"],
            "is_ground_level": True,
            "score_X": 0.99,
            "score_Y": 0.98,
            "multi_X": 0.01,
            "multi_Y": 0.01,
            "fmin_X": 0.05,
            "fmin_Y": 0.06,
            "fmax_X": 80.0,
            "fmax_Y": 80.0,
            **obs.loc[sta].to_dict(),
        }
        rec.update((bad or {}).get(str(sta), {}))
        rows.append(rec)
    pd.DataFrame(rows).to_csv(directory / "ground_motion_im_table_rotd50_flat.csv", index=False)
    phases = []
    for sta, row in stations.iterrows():
        r_hyp = math.hypot(row["rrup"], HYPO[2])
        phases.append(
            {
                "record_id": f"{EVENT}_{sta}_HN_20",
                "p_wave_datetime": (ORIGIN + pd.Timedelta(seconds=r_hyp / 6.0)).isoformat(),
                "p_wave_prob": 0.9,
                "s_wave_datetime": (ORIGIN + pd.Timedelta(seconds=r_hyp / 3.5)).isoformat(),
                "s_wave_prob": 0.8,
                "evid_datetime": ORIGIN.isoformat(),
            }
        )
    pd.DataFrame(phases).to_csv(directory / "phase_arrival_table.csv", index=False)
    pd.DataFrame(
        [{"evid": EVENT, "lat": HYPO[0], "lon": HYPO[1], "depth": HYPO[2], "mag": 5.4, "mag_type": "ML"}]
    ).to_csv(directory / "earthquake_source_table.csv", index=False)


def write_realisation(directory: Path, flo: float, half_width_deg: float = 1.5, magnitude: float = 5.0) -> None:
    """A realisation with a single-plane source centred on HYPO, a square domain and bb.flo."""
    directory.mkdir(parents=True, exist_ok=True)
    lat, lon, depth = HYPO
    d = 0.01
    corners = [
        {"latitude": lat - d, "longitude": lon - d, "depth": (depth - 1) * 1000},
        {"latitude": lat + d, "longitude": lon + d, "depth": (depth - 1) * 1000},
        {"latitude": lat + d, "longitude": lon + d, "depth": (depth + 1) * 1000},
        {"latitude": lat - d, "longitude": lon - d, "depth": (depth + 1) * 1000},
    ]
    h = half_width_deg
    rel = {
        "sources": {"source_geometries": {EVENT: {"type": "fault", "corners": corners}}},
        "rupture_propagation": {"hypocentre": {"s": 0.5, "d": 0.5}},
        "magnitudes": {"magnitudes": {EVENT: magnitude}},
        "bb": {"flo": flo},
        "domain": {
            "domain": [
                {"latitude": lat - h, "longitude": lon - h},
                {"latitude": lat - h, "longitude": lon + h},
                {"latitude": lat + h, "longitude": lon + h},
                {"latitude": lat + h, "longitude": lon - h},
            ]
        },
        "sw4": {"commands": [{"name": "supergrid", "parameters": {"width": 12000.0}}]},
    }
    (directory / "realisation.json").write_text(json.dumps(rel))


def synthetic_traces(
    stations: pd.DataFrame,
    flo: float,
    seed: int = 1,
    hf_gain: float = 1.0,
    shift_s: float = 0.0,
    decay_s: float = 3.0,
    vertical_gain: float = 0.33,
) -> np.ndarray:
    """(station, component, time) acceleration: enveloped white noise from the S arrival."""
    rng = np.random.default_rng(seed)
    t = np.arange(NT) * DT
    freq = np.fft.rfftfreq(NT, DT)
    out = np.zeros((len(stations), 3, NT))
    shift = round(shift_s / DT)
    for i, (_, row) in enumerate(stations.iterrows()):
        ts = math.hypot(row["rrup"], HYPO[2]) / 3.5
        tau = np.clip(t - ts, 0, None)
        envelope = tau * np.exp(-tau / decay_s)
        envelope *= _model(row["rrup"], None) / (3 * envelope.max())
        for c in range(3):
            noise = rng.standard_normal(NT) * envelope
            spectrum = np.fft.rfft(noise)
            spectrum[freq > flo] *= hf_gain
            trace = np.roll(np.fft.irfft(spectrum, NT), shift)
            if shift > 0:
                trace[:shift] = 0.0
            elif shift < 0:
                trace[shift:] = 0.0
            out[i, c] = trace * (vertical_gain if c == 2 else 1.0)
    return out


def write_waveforms(run_dir: Path, stations: list[str], acc: np.ndarray) -> None:
    path = run_dir / "simdb" / f"{EVENT}_waveforms.nc"
    path.parent.mkdir(parents=True, exist_ok=True)
    ds = xr.Dataset(
        {"acc": (("station", "component", "time"), acc.astype("float32"))},
        coords={"station": stations, "component": ["000", "090", "ver"], "time": np.arange(acc.shape[2]) * DT},
        attrs={"dt": DT, "units": "g"},
    )
    ds.to_netcdf(path, group=EVENT, engine="h5netcdf")


def make_event(
    root: Path,
    resolutions: tuple[str, ...] = ("1hz",),
    sim_scale: Callable[[float | None], float] = lambda p: 1.0,
    fine_scale: float = 1.0,
    waveforms: bool = True,
    trace_kwargs: dict[str, Any] | None = None,
    fine_shift_s: float = 0.0,
    bad_obs: dict[str, dict[str, Any]] | None = None,
) -> Path:
    """Write a synthetic event in the cache layout and return its directory."""
    event_dir = root / f"1-{EVENT}"
    stations = _stations()
    rng = np.random.default_rng(7)
    truth = _sim_table(stations)
    obs = truth * np.exp(0.3 * rng.standard_normal(truth.shape))
    write_observed(event_dir / "observed", stations, obs, bad_obs)
    write_realisation(event_dir, 1.0)
    for k, res in enumerate(resolutions):
        flo = science_io.RESOLUTION_HZ[res]
        run_dir = event_dir / res
        write_realisation(run_dir, flo)
        sim = _sim_table(stations, sim_scale) * np.exp(0.05 * rng.standard_normal(truth.shape))
        if k > 0:
            sim = sim * fine_scale
        gmm = truth * np.exp(0.3 * rng.standard_normal(truth.shape))
        write_imdb(run_dir / "intensity_measures.duckdb", stations, sim, gmm)
        if waveforms:
            kwargs = dict(trace_kwargs or {})
            if k > 0:
                kwargs["shift_s"] = kwargs.get("shift_s", 0.0) + fine_shift_s
            write_waveforms(run_dir, list(stations.index), synthetic_traces(stations, flo, **kwargs))
    return event_dir


def run_all(event_dir: Path) -> sqlite3.Connection:
    """Run every science extractor on a synthetic event; return the database."""
    con = scandb.connect(":memory:")
    con.execute(
        "INSERT INTO events (event_id, cycle, event_dir, origin_time, magnitude) VALUES (?, 1, ?, ?, 5.0)",
        (EVENT, event_dir.name, ORIGIN.isoformat()),
    )
    for res in science_io.RESOLUTION_HZ:
        if (event_dir / res).is_dir():
            con.execute("INSERT INTO runs (event_id, resolution) VALUES (?, ?)", (EVENT, res))
            science.residuals(con, EVENT, res, event_dir / res)
            science_waveforms.waveform_sanity(con, EVENT, res, event_dir / res)
    science.cross_resolution(con, EVENT, event_dir)
    science.source_and_coverage(con, EVENT, event_dir)
    science_waveforms.cross_resolution_waveforms(con, EVENT, event_dir)
    return con


def metric(con: sqlite3.Connection, name: str, res: str = "1hz") -> float:
    value = scandb.get_metric(con, EVENT, res, name)
    assert value is not None, f"{res} {name} missing"
    return value


def event_metric(con: sqlite3.Connection, name: str) -> float:
    return metric(con, name, "")


# --------------------------------------------------------------------------- tests


def test_clean_event_metrics(tmp_path: Path) -> None:
    con = run_all(make_event(tmp_path, resolutions=("0_5hz", "1hz")))
    assert metric(con, "science.bb.flo") == 1.0
    assert metric(con, "science.residual.n_stations") == 24
    for im in ("PGA", "pSA_1.0", "pSA_5.0"):
        assert abs(metric(con, f"science.residual.median.{im}")) < 0.3
        assert 0.15 < metric(con, f"science.residual.std.{im}") < 0.5
        assert abs(metric(con, f"science.gmm_all.median.{im}")) < 0.3 if im != "PGA" else True
    assert abs(metric(con, "science.residual.slope_lnrrup.pSA_1.0")) < 0.4
    assert abs(metric(con, "science.residual.band_median.lf")) < 0.3
    rows = con.execute("SELECT count(*), sum(quality_ok) FROM station_residuals WHERE resolution = '1hz'").fetchone()
    assert rows[0] == 24 * (2 + len(PERIODS)) and rows[1] > 0.8 * rows[0]
    assert abs(event_metric(con, "science.xres.0_5hz-1hz.median")) < 0.1
    assert event_metric(con, "science.xres.0_5hz-1hz.n") > 10
    assert event_metric(con, "science.xres.0_5hz-1hz.wave.xcorr0.median") == pytest.approx(1.0, abs=0.05)
    assert abs(event_metric(con, "science.xres.0_5hz-1hz.wave.lag.median")) < 0.05
    assert metric(con, "science.wave.n_invalid") == 0
    assert metric(con, "science.wave.frac_early") == 0
    assert metric(con, "science.wave.frac_truncated") == 0
    assert 2 < metric(con, "science.wave.hv.median") < 4
    assert abs(metric(con, "science.wave.s_offset.intercept")) < 1.0
    assert abs(metric(con, "science.crossover.step.median")) < 0.3
    assert scandb.get_metric(con, EVENT, "", "science.source.hypo_offset_km") == pytest.approx(0.0, abs=0.5)
    assert scandb.get_metric(con, EVENT, "", "science.source.mag_spread") == pytest.approx(0.0, abs=1e-5)
    assert scandb.get_metric(con, EVENT, "", "science.source.mag_diff_obs") is None  # ML is not compared
    assert scandb.get_metric(con, EVENT, "", "science.coverage.n_good_inside_unsimulated") == 0


def test_idempotent(tmp_path: Path) -> None:
    event_dir = make_event(tmp_path)
    con = run_all(event_dir)
    first = con.execute("SELECT count(*) FROM metrics").fetchone()[0]
    science.residuals(con, EVENT, "1hz", event_dir / "1hz")
    science_waveforms.waveform_sanity(con, EVENT, "1hz", event_dir / "1hz")
    assert con.execute("SELECT count(*) FROM metrics").fetchone()[0] == first


def test_units_bug_moves_obs_and_gmm_residuals(tmp_path: Path) -> None:
    # Simulation 981x too large (cm/s^2 written as g): every comparison sees it.
    con = run_all(make_event(tmp_path, sim_scale=lambda p: 981.0, waveforms=False))
    assert metric(con, "science.residual.band_median.hf") < -6
    assert metric(con, "science.gmm_all.band_median.lf") < -6
    assert abs(metric(con, "science.obs_gmm_residual.median.pSA_1.0")) < 0.5


def test_quality_filter_rejects_junk_records(tmp_path: Path) -> None:
    bad = {
        "S03": {"PGA": 4e-5, "score_X": float("nan"), "score_Y": float("nan")},  # no classifier score
        "S05": {"is_ground_level": False},
        "S07": {"fmin_X": 2.0},  # long periods unusable
    }
    con = run_all(make_event(tmp_path, bad_obs=bad, waveforms=False))
    q = dict(
        con.execute(
            "SELECT station || ':' || im, quality_ok FROM station_residuals WHERE resolution = '1hz'"
        ).fetchall()
    )
    assert q["S03:PGA"] == 0 and q["S05:PGA"] == 0
    assert q["S07:PGA"] == 1 and q["S07:pSA_1.0"] == 0 and q["S07:pSA_0.1"] == 1
    assert q["S08:pSA_10.0"] == 1  # fmin 0.06: usable to 1/(1.25*0.06) = 13 s
    assert abs(metric(con, "science.residual.median.PGA")) < 0.3


def test_record_choice_prefers_passing_hn(tmp_path: Path) -> None:
    directory = tmp_path / "observed"
    directory.mkdir()
    base = {
        "PGA": 0.01,
        "PGV": 1.0,
        "score_X": 0.9,
        "score_Y": 0.9,
        "multi_X": 0,
        "multi_Y": 0,
        "is_ground_level": True,
    }
    pd.DataFrame(
        [
            {**base, "record_id": "e_AAA_HN_21", "sta": "AAA", "loc": 21, "chan": "HN", "score_X": 0.01},
            {**base, "record_id": "e_AAA_HH_10", "sta": "AAA", "loc": 10, "chan": "HH", "PGA": 0.02},
            {**base, "record_id": "e_AAA_HN_20", "sta": "AAA", "loc": 20, "chan": "HN", "PGA": 0.03},
        ]
    ).to_csv(directory / "ground_motion_im_table_rotd50_flat.csv", index=False)
    observed = science_io.read_observed(directory)
    assert observed is not None
    assert observed.table.loc["AAA", "record_id"] == "e_AAA_HN_20"


def test_cross_resolution_detects_scaled_fine_run(tmp_path: Path) -> None:
    con = run_all(make_event(tmp_path, resolutions=("0_5hz", "1hz"), fine_scale=2.0, waveforms=False))
    assert event_metric(con, "science.xres.0_5hz-1hz.median") == pytest.approx(math.log(2), abs=0.1)


def test_cross_resolution_lag(tmp_path: Path) -> None:
    con = run_all(make_event(tmp_path, resolutions=("0_5hz", "1hz"), fine_shift_s=2.0))
    assert event_metric(con, "science.xres.0_5hz-1hz.wave.lag.median") == pytest.approx(2.0, abs=0.1)


def test_invalid_and_hv_and_truncation(tmp_path: Path) -> None:
    event_dir = make_event(tmp_path, trace_kwargs={"vertical_gain": 3.0, "decay_s": 200.0})
    path = event_dir / "1hz" / "simdb" / f"{EVENT}_waveforms.nc"
    with xr.open_dataset(path, group=EVENT, engine="h5netcdf") as ds:
        ds = ds.load()
    acc = ds["acc"].values.copy()
    acc[0, 0, 100] = np.nan
    acc[1, 2, :] = 0.0
    acc[2, 1, 2000] = 9.0
    path.unlink()
    write_waveforms(event_dir / "1hz", [str(s) for s in ds["station"].values], acc)
    con = run_all(event_dir)
    assert metric(con, "science.wave.n_invalid") == 3
    assert metric(con, "science.wave.hv.median") < 1.0
    assert metric(con, "science.wave.frac_truncated") > 0.5


def test_timing_offset_and_early_energy(tmp_path: Path) -> None:
    con = run_all(make_event(tmp_path, trace_kwargs={"shift_s": 5.0}))
    assert metric(con, "science.wave.s_offset.intercept") == pytest.approx(5.0, abs=1.0)
    assert metric(con, "science.wave.frac_early") == 0
    early = run_all(make_event(tmp_path / "b", trace_kwargs={"shift_s": -15.0}))
    assert metric(early, "science.wave.frac_early") > 0.5


def test_crossover_step_detects_hf_gain(tmp_path: Path) -> None:
    con = run_all(make_event(tmp_path, trace_kwargs={"hf_gain": 10.0}))
    assert metric(con, "science.crossover.step.median") == pytest.approx(math.log(10), abs=0.4)


def test_crossover_step_function_is_zero_for_white_noise() -> None:
    rng = np.random.default_rng(3)
    steps = [science_waveforms.crossover_step(rng.standard_normal(8000), 0.01, 0.5) for _ in range(20)]
    assert abs(float(np.median(steps))) < 0.2


def test_source_mismatch_and_magnitude_spread(tmp_path: Path) -> None:
    event_dir = make_event(tmp_path, waveforms=False)
    write_realisation(event_dir / "1hz", 1.0, magnitude=5.5)
    pd.DataFrame(
        [{"evid": EVENT, "lat": HYPO[0] + 0.5, "lon": HYPO[1], "depth": 5.0, "mag": 4.4, "mag_type": "Mw"}]
    ).to_csv(event_dir / "observed" / "earthquake_source_table.csv", index=False)
    con = run_all(event_dir)
    assert scandb.get_metric(con, EVENT, "", "science.source.hypo_offset_km") == pytest.approx(55.6, abs=1)
    assert scandb.get_metric(con, EVENT, "", "science.source.depth_diff_km") == pytest.approx(5.0)
    assert scandb.get_metric(con, EVENT, "", "science.source.mag_spread") == pytest.approx(0.5, abs=1e-3)
    assert scandb.get_metric(con, EVENT, "", "science.source.mag_diff_obs") == pytest.approx(0.6, abs=1e-3)


def test_coverage_counts_outside_and_unsimulated(tmp_path: Path) -> None:
    event_dir = make_event(tmp_path, waveforms=False)
    write_realisation(event_dir / "1hz", 1.0, half_width_deg=0.6)  # ~67 x 48 km half-widths
    con = run_all(event_dir)
    outside = scandb.get_metric(con, EVENT, "", "science.coverage.n_good_outside") or 0
    assert outside > 0
    # Every station is in the IM database, so none inside the domain is unsimulated.
    assert scandb.get_metric(con, EVENT, "", "science.coverage.n_good_inside_unsimulated") == 0


def test_missing_files_write_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    event_dir = tmp_path / f"1-{EVENT}"
    (event_dir / "1hz").mkdir(parents=True)
    (event_dir / "1hz" / "realisation.json").write_text("{not json")
    con = run_all(event_dir)
    written = {row[0] for row in con.execute("SELECT metric FROM metrics")}
    # Only the magnitude recorded in the events table remains to report.
    assert written <= {"science.source.mag_sim", "science.source.mag_spread"}
    assert "unreadable JSON" in capsys.readouterr().err


def test_simdb_json_fallback_only_for_finest(tmp_path: Path) -> None:
    event_dir = make_event(tmp_path, resolutions=("0_5hz", "1hz"), waveforms=False)
    for res in ("0_5hz", "1hz"):
        (event_dir / res / "intensity_measures.duckdb").unlink()
    stations = _stations()
    sim = _sim_table(stations)
    sites = [
        {
            "site_id": sta,
            "lat": row["lat"],
            "lon": row["lon"],
            "rrup": row["rrup"],
            "vs30": row["vs30"],
            "ims": {
                "rotd50": {
                    "PGA": sim.loc[sta, "PGA"],
                    "PGV": sim.loc[sta, "PGV"],
                    "pSA": [sim.loc[sta, science_io.im_name(p)] for p in PERIODS],
                }
            },
        }
        for sta, row in stations.iterrows()
    ]
    (event_dir / "simdb_station_ims.json").write_text(
        json.dumps({"event_id": EVENT, "periods": PERIODS, "sites": sites})
    )
    con = run_all(event_dir)
    assert metric(con, "science.residual.n_stations", "1hz") == 24
    assert scandb.get_metric(con, EVENT, "0_5hz", "science.residual.n_stations") is None
    detail = json.loads(
        con.execute("SELECT detail FROM metrics WHERE metric = 'science.residual.n_stations'").fetchone()[0]
    )
    assert detail["sim_source"] == "simdb_station_ims.json"


def test_ols_and_polygon_helpers() -> None:
    slope, se, intercept, _ = science_io.ols_slope([1, 2, 3, 4, 5], [3, 5, 7, 9, 11])
    assert slope == pytest.approx(2.0) and intercept == pytest.approx(1.0) and se == pytest.approx(0.0)
    inside, distance = science_io.polygon_edge_distance(
        np.array([-43.0, -41.0]),
        np.array([172.0, 172.0]),
        [(-43.5, 171.5), (-43.5, 172.5), (-42.5, 172.5), (-42.5, 171.5)],
    )
    assert inside.tolist() == [True, False]
    assert distance[0] == pytest.approx(40.6, abs=1.0)


def test_cross_resolution_survives_deleted_databases(tmp_path: Path) -> None:
    event_dir = make_event(tmp_path, resolutions=("0_5hz", "1hz"), fine_scale=2.0, waveforms=False)
    con = scandb.connect(":memory:")
    for res in ("0_5hz", "1hz"):
        science.residuals(con, EVENT, res, event_dir / res)
        (event_dir / res / "intensity_measures.duckdb").unlink()  # as the collector does
    science.cross_resolution(con, EVENT, event_dir)
    assert event_metric(con, "science.xres.0_5hz-1hz.median") == pytest.approx(math.log(2), abs=0.1)
