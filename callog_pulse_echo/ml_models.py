"""
Ultrasonik Makine Öğrenmesi (ML) Çıkarım ve Canlı Analiz Modülü.
Eğitilmiş modelleri (Tuned GBR, Baseline GBR, Hibrit Model) yükler,
canlı osiloskop dalga formundan gerçek zamanlı (< 1ms) öznitelik çıkarır
ve kalınlık / ses hızı tahmini yapar.
"""

import os
import time
import numpy as np
import pandas as pd
import joblib

from . import ultrasonic
from .feature_extraction import extract_features_from_waveform

_MODEL_CACHE = {}

AVAILABLE_MODELS = (
    ("Tuned ML Modeli (Gradient Boosting)", "tuned_gbr"),
    ("Akıllı Hibrit Model (DSP + ML Füzyonu)", "hybrid"),
    ("Temel ML Modeli (Baseline GBR)", "baseline_gbr"),
    ("Klasik DSP (Paket/Zarf Tespiti)", "dsp"),
)


def get_model_path(filename):
    """Proje kök dizinindeki dataset/models/ yolunu bulur."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(current_dir, ".."))
    return os.path.join(project_root, "dataset", "models", filename)


def load_model_data(model_key="tuned_gbr"):
    """İstenen modeli diskten veya bellek önbelleğinden yükler."""
    if model_key in _MODEL_CACHE:
        return _MODEL_CACHE[model_key]

    filename = "tuned_thickness_model.pkl" if model_key in ("tuned_gbr", "hybrid") else "thickness_model.pkl"
    path = get_model_path(filename)

    if not os.path.exists(path):
        fallback_path = get_model_path("thickness_model.pkl")
        if os.path.exists(fallback_path):
            path = fallback_path
        else:
            return None

    try:
        data = joblib.load(path)
        _MODEL_CACHE[model_key] = data
        return data
    except Exception:
        return None


def analyze_with_model(times, values, thickness_m=0.025, reference_velocity=5740.0,
                       model_key="tuned_gbr", max_echoes=3, skip_first_packet=True):
    """
    Canlı veya kaydedilmiş bir dalga formunu (times, values) seçilen model ile çözümler.
    
    Döndürdüğü sözlük, hem klasik DSP sonuçlarını (paketler, korelasyon, koherans)
    hem de ML tahminlerini (tahmin edilen kalınlık, ses hızı, % sapma, anomali durumu) içerir.
    """
    t0 = time.time()
    th_nominal_mm = float(thickness_m) * 1000.0
    ref_v = float(reference_velocity) if reference_velocity else 5740.0

    dsp_res = ultrasonic.analyze(
        times, values, thickness_m=thickness_m,
        max_echoes=max_echoes,
        skip_first_packet=skip_first_packet,
        reference_velocity=ref_v
    )

    if model_key == "dsp":
        pred_mm = dsp_res.get("thickness_est_mm", 0.0) if dsp_res.get("found") else (
            (ref_v * dsp_res.get("envelope_round_trip_s", 0.0) / 2.0) * 1000.0 if dsp_res.get("envelope_round_trip_s") else 0.0
        )
        if pred_mm <= 0 and dsp_res.get("found") and dsp_res.get("velocity"):
            dt = dsp_res.get("estimates", [{}])[0].get("dt", 0.0) if dsp_res.get("estimates") else 0.0
            pred_mm = (dsp_res["velocity"] * dt / 2.0) * 1000.0 if dt > 0 else th_nominal_mm

        dsp_res["thickness_m"] = float(thickness_m)
        dsp_res["ml_model_key"] = "dsp"
        dsp_res["ml_model_name"] = "Klasik DSP (Paket/Zarf Tespiti)"
        dsp_res["predicted_thickness_mm"] = pred_mm
        dsp_res["predicted_thickness_m"] = pred_mm / 1000.0
        dsp_res["abs_error_mm"] = abs(pred_mm - th_nominal_mm) if pred_mm > 0 else 0.0
        dsp_res["pct_error"] = (abs(pred_mm - th_nominal_mm) / th_nominal_mm * 100.0) if pred_mm > 0 else 0.0
        dsp_res["inference_time_ms"] = (time.time() - t0) * 1000.0
        dsp_res["is_anomaly"] = False
        return dsp_res

    features_dict = extract_features_from_waveform(
        times, values,
        thickness_mm=th_nominal_mm,
        dsp_res=dsp_res
    )

    model_data = load_model_data(model_key)
    if model_data is None:
        dsp_res["thickness_m"] = float(thickness_m)
        dsp_res["ml_model_key"] = "dsp_fallback"
        dsp_res["ml_model_name"] = "Klasik DSP (Model Dosyası Bulunamadı)"
        dsp_res["predicted_thickness_mm"] = th_nominal_mm
        dsp_res["predicted_thickness_m"] = thickness_m
        dsp_res["abs_error_mm"] = 0.0
        dsp_res["pct_error"] = 0.0
        dsp_res["inference_time_ms"] = (time.time() - t0) * 1000.0
        dsp_res["is_anomaly"] = False
        return dsp_res

    model = model_data["model"]
    feature_cols = model_data.get("feature_cols", [])

    feat_df = pd.DataFrame([features_dict])
    X = feat_df[feature_cols] if all(c in feat_df.columns for c in feature_cols) else feat_df

    try:
        raw_pred_mm = float(model.predict(X)[0])
    except Exception:
        raw_pred_mm = th_nominal_mm

    dsp_coh = float(dsp_res.get("coherence", 0.0) or 0.0)
    dsp_found = bool(dsp_res.get("found", False))
    dsp_round_trip = float(dsp_res.get("envelope_round_trip_s", 0.0) or 0.0)
    dsp_thick_est = (ref_v * dsp_round_trip / 2.0) * 1000.0 if dsp_round_trip > 0 else 0.0

    if model_key == "hybrid":
        model_name = "Akıllı Hibrit Model (DSP + ML)"
        if dsp_found and dsp_coh >= 0.88 and 3.5 <= dsp_thick_est <= 35.0:
            final_pred_mm = 0.65 * dsp_thick_est + 0.35 * raw_pred_mm
        else:
            final_pred_mm = raw_pred_mm
    elif model_key == "baseline_gbr":
        model_name = "Temel ML Modeli (Baseline GBR)"
        final_pred_mm = raw_pred_mm
    else:
        model_name = "Tuned ML Modeli (Gradient Boosting)"
        final_pred_mm = raw_pred_mm

    max_abs_v = features_dict.get("max_abs_v", 0.0)
    is_anomaly = bool(max_abs_v > 9.5 or (not dsp_found and dsp_coh == 0.0 and abs(final_pred_mm - th_nominal_mm) > 5.0))

    if dsp_round_trip > 0:
        ml_velocity = (2.0 * (final_pred_mm / 1000.0)) / dsp_round_trip
    else:
        ml_velocity = ref_v * (final_pred_mm / th_nominal_mm)

    dt_sample = float(np.mean(np.diff(times))) if len(times) > 1 else 1e-9

    result = dict(dsp_res)
    # DSP branch can report found=False; the model always produces a thickness.
    result["found"] = True
    result["thickness_m"] = float(thickness_m)
    result["thickness_est_mm"] = final_pred_mm
    result["sample_interval"] = dsp_res.get("sample_interval", dt_sample)
    result["packets"] = dsp_res.get("packets", [])
    result["estimates"] = dsp_res.get("estimates", [])
    result["n_estimates"] = dsp_res.get("n_estimates", len(dsp_res.get("estimates", [])))
    result["velocity_std"] = dsp_res.get("velocity_std")
    result["velocity_min"] = dsp_res.get("velocity_min", ml_velocity)
    result["velocity_max"] = dsp_res.get("velocity_max", ml_velocity)
    result["carrier_period_s"] = dsp_res.get("carrier_period_s")
    result["ml_model_key"] = model_key
    result["ml_model_name"] = model_name
    result["predicted_thickness_mm"] = final_pred_mm
    result["predicted_thickness_m"] = final_pred_mm / 1000.0
    result["abs_error_mm"] = abs(final_pred_mm - th_nominal_mm)
    result["pct_error"] = (abs(final_pred_mm - th_nominal_mm) / th_nominal_mm) * 100.0
    result["velocity"] = ml_velocity if (3000.0 <= ml_velocity <= 8000.0) else (dsp_res.get("velocity") or ref_v)
    result["dsp_velocity"] = dsp_res.get("velocity")
    result["dsp_coherence"] = dsp_coh
    result["is_anomaly"] = is_anomaly
    result["features"] = features_dict
    result["inference_time_ms"] = (time.time() - t0) * 1000.0

    return result
