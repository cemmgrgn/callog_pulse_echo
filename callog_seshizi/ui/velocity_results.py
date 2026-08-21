"""VelocityPage'in tablo, kayıt ve dışa aktarma bölümü.

velocity_page.py'den ayrıldı; davranış değişmiyor, yalnızca dosya boyutu
yönetilebilir kalıyor — waveform_page/waveform_results ayrımıyla aynı gerekçe.
"""

import csv
import json
import os

from callog_common import db
from callog_common import drivers
from callog_common import testmodes
from callog_common import waveform
from callog_common.qt import Qt
from callog_common.qt import QtWidgets
from callog_common.ui.util import empty_state
from callog_common.ui.util import fit_table
from .. import ultrasonic


class _VelocityResultsMixin:

    def _tables_box(self):
        box = QtWidgets.QGroupBox("Ayrıntı")
        tabs = QtWidgets.QTabWidget()

        self.estimate_table = QtWidgets.QTableWidget(0, 6)
        self.estimate_table.setHorizontalHeaderLabels(
            ["Yankı çifti", "Yöntem", "Tur", "Δt", "Yol (mm)", "c (m/s)"])
        self.estimate_table.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        self.estimate_table.setMinimumHeight(90)
        tabs.addTab(self.estimate_table, "Kestirimler")

        self.packet_table = QtWidgets.QTableWidget(0, 6)
        self.packet_table.setHorizontalHeaderLabels(
            ["Yankı", "Başlangıç", "Süre", "Çekirdek", "Genlik (V)",
             "SNR (dB)"])
        self.packet_table.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        tabs.addTab(self.packet_table, "Paketler")

        self.summary_table = QtWidgets.QTableWidget(0, 2)
        self.summary_table.setHorizontalHeaderLabels(["Büyüklük", "Değer"])
        self.summary_table.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        tabs.addTab(self.summary_table, "Özet")

        self.ml_table = QtWidgets.QTableWidget(0, 2)
        self.ml_table.setHorizontalHeaderLabels(["Model Metriği / Parametre", "Değer"])
        self.ml_table.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        tabs.addTab(self.ml_table, "Makine Öğrenmesi (ML)")

        self.measure_table = QtWidgets.QTableWidget(0, 7)
        self.measure_table.setHorizontalHeaderLabels(
            ["#", "Zaman", "d (mm)", "c (m/s)", "Yankı", "Sıcaklık", "Dosya"])
        self.measure_table.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        self.measure_table.setSelectionBehavior(
            QtWidgets.QTableWidget.SelectRows)
        tabs.addTab(self.measure_table, "Kaydedilen ölçümler")

        lay = QtWidgets.QVBoxLayout(box)
        lay.addWidget(tabs)
        return box

    # --- tablo doldurma ---------------------------------------------------
    def _fill_tables(self):
        result = self._result
        self.estimate_table.setRowCount(0)
        self.packet_table.setRowCount(0)
        self.summary_table.setRowCount(0)
        self.ml_table.setRowCount(0)
        if not (result and result.get("found")):
            empty_state(self.estimate_table,
                        "Ölçülebilir yankı çifti yok.")
            empty_state(self.ml_table, "ML çözümlemesi bulunamadı.")
            return

        for row in ultrasonic.estimate_rows(result):
            i = self.estimate_table.rowCount()
            self.estimate_table.insertRow(i)
            cells = [row["pair"], row["feature"], str(row["round_trips"]),
                     _s(row["dt"]), "%.3f" % row["path_mm"],
                     "%.1f" % row["velocity"]]
            for col, text in enumerate(cells):
                self.estimate_table.setItem(
                    i, col, QtWidgets.QTableWidgetItem(text))
        fit_table(self.estimate_table, stretch_column=0)

        for packet in result.get("packets", []):
            i = self.packet_table.rowCount()
            self.packet_table.insertRow(i)
            cells = [str(packet["index"]), _s(packet["start_time"]),
                     _s(packet["duration"]),
                     _s(packet.get("core_duration")),
                     "%.4f" % packet["peak_amplitude"],
                     "%.1f" % packet["snr_db"] if packet.get("snr_db")
                     else "—"]
            for col, text in enumerate(cells):
                self.packet_table.setItem(
                    i, col, QtWidgets.QTableWidgetItem(text))
        fit_table(self.packet_table, stretch_column=1)

        budget = ultrasonic.uncertainty(
            result, u_thickness_m=self.u_thickness_spin.value() / 1e6,
            type_a_velocity=self._history_u_a())
        for label, value in ultrasonic.summary_rows(result, budget):
            i = self.summary_table.rowCount()
            self.summary_table.insertRow(i)
            self.summary_table.setItem(i, 0, QtWidgets.QTableWidgetItem(label))
            self.summary_table.setItem(i, 1, QtWidgets.QTableWidgetItem(value))
        fit_table(self.summary_table, stretch_column=1)

        pred_th = result.get("predicted_thickness_mm")
        if pred_th is not None:
            ml_rows = [
                ("Aktif Çözümleme Modeli", str(result.get("ml_model_name", "Tuned ML"))),
                ("Kestirilen Kalınlık (ML)", "%.3f mm" % pred_th),
                ("Nominal / Girilen Kalınlık", "%.3f mm" % (self._thickness_m() * 1000.0)),
                ("Mutlak Kalınlık Hatası", "%.3f mm" % result.get("abs_error_mm", 0.0)),
                ("Bağıl Sapma Oranı (% MAPE)", "%% %.2f" % result.get("pct_error", 0.0)),
                ("Kestirilen Ses Hızı", "%.1f m/s" % result.get("velocity", 0.0)),
                ("DSP Faz Koheransı", "%.4f" % (result.get("dsp_coherence") or 0.0)),
                ("Sinyal Kalitesi / Anomali", "⚠️ ANOMALİ / BOZUK SİNYAL" if result.get("is_anomaly") else "✅ Normal / Geçerli Sinyal"),
                ("Çıkarım Süresi (Latency)", "%.2f ms" % result.get("inference_time_ms", 0.0)),
            ]
            feat = result.get("features", {})
            if feat:
                ml_rows.extend([
                    ("Zarf Ağırlık Merkezi (t_centroid)", "%.2f µs" % (feat.get("env_centroid_t", 0.0) * 1e6)),
                    ("Baskın Frekans", "%.2f MHz" % feat.get("dominant_freq_mhz", 0.0)),
                    ("Spektral Merkez Frekansı", "%.2f MHz" % feat.get("spectral_centroid_mhz", 0.0)),
                    ("Zarf Maksimum Genliği", "%.4f V" % feat.get("env_max", 0.0)),
                    ("Otokorelasyon Periyodu (tau)", "%.2f µs" % (feat.get("autocorr_tau_s", 0.0) * 1e6)),
                    ("Otokorelasyon Kalınlık Kestirimi", "%.2f mm" % feat.get("autocorr_thickness_est_mm", 0.0)),
                ])
            for label, val in ml_rows:
                i = self.ml_table.rowCount()
                self.ml_table.insertRow(i)
                self.ml_table.setItem(i, 0, QtWidgets.QTableWidgetItem(label))
                self.ml_table.setItem(i, 1, QtWidgets.QTableWidgetItem(val))
            fit_table(self.ml_table, stretch_column=1)

    # --- kayıt -------------------------------------------------------------
    def _save_measurement(self):
        result = self._result
        if not (result and result.get("found")) or self._frame is None:
            return
        inst = self._current_instrument()
        if inst is None:
            return
        if self._series_id is None:
            self._series_id = waveform.new_series_id()

        times, values = self._frame
        budget = ultrasonic.uncertainty(
            result, u_thickness_m=self.u_thickness_spin.value() / 1e6,
            type_a_velocity=self._history_u_a())

        # Çözümlemenin yanına ölçüm koşulları da yazılıyor: ham CSV tek
        # başına hangi basamakta, hangi probla ölçüldüğünü söylemez ve
        # sonuç yeniden üretilemez.
        analysis = dict(result)
        analysis["uncertainty"] = budget
        analysis["probe"] = self.probe_combo.currentData()
        analysis["material"] = self.material_combo.currentData()
        analysis["skip_first_packet"] = self.skip_first_chk.isChecked()
        analysis["frames_in_mean"] = len(self._history)
        # Ekrandaki imleç ve otomatik ölçümler de kayda giriyor: operatör
        # imleçle elle bir Δt okuduysa, sonucun neye bakılarak kabul
        # edildiği kayıttan anlaşılabilmeli.
        analysis["scope_measurements"] = self.scope.measurement_results()
        analysis["mean_of_frames"] = (sum(self._history) / len(self._history)
                                      if self._history else None)

        index = len(waveform.series_captures(self._series_id)) + 1
        try:
            capture_id = waveform.save(
                times, {"CH1_V": values},
                instrument_id=inst["id"],
                operator_id=self.state.user["id"],
                dut_id=self.dut_combo.currentData(),
                prefix="seshizi",
                test_mode=seshizi_modes.SOUND_VELOCITY,
                setup=self._setup_snapshot(),
                analysis=analysis,
                is_simulated=drivers.is_simulated(inst["driver"]),
                series_id=self._series_id, series_index=index,
                thickness_mm=self.thickness_spin.value(),
                thickness_u_mm=self.u_thickness_spin.value() / 1000.0,
                temperature_c=self.temp_spin.value())
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Kaydedilemedi", str(exc))
            return

        self.status_label.setText(
            "Ölçüm kaydedildi (#%d, seri %s)." % (capture_id, self._series_id))
        self._reload_measurements()
        self._update_buttons()

    def _setup_snapshot(self):
        """Cihazda fiilen geçerli olan ayarlar — kayda geçen bunlar."""
        snapshot = {
            "points": self.points_spin.value(),
            "high_resolution": self.hires_chk.isChecked(),
            "averaging": self.avg_spin.value(),
            "echoes": self.echoes_spin.value(),
        }
        if self.driver is not None:
            try:
                snapshot.update(self.driver.read_setup("CHANnel1"))
            except Exception:
                pass
            try:
                if hasattr(self.driver, "sample_rate"):
                    snapshot["sample_rate"] = self.driver.sample_rate()
            except Exception:
                pass
        return snapshot

    def _reload_measurements(self):
        self.measure_table.setRowCount(0)
        rows = db.query(
            "SELECT * FROM waveform_captures"
            " WHERE test_mode = ? ORDER BY id DESC LIMIT 200",
            (seshizi_modes.SOUND_VELOCITY,))
        for r in rows:
            i = self.measure_table.rowCount()
            self.measure_table.insertRow(i)
            analysis = waveform.analysis_of(r) or {}
            cells = [
                str(r["series_index"] or r["id"]),
                (r["captured_at"] or "").replace("T", " ")[:19],
                "%.3f" % r["thickness_mm"] if r["thickness_mm"] else "—",
                "%.1f" % analysis["velocity"] if analysis.get("velocity")
                else "—",
                str(len(analysis.get("packets", []))),
                "%.1f °C" % r["temperature_c"] if r["temperature_c"] else "—",
                os.path.basename(r["file_path"]),
            ]
            for col, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if col == 0:
                    item.setData(Qt.UserRole, r["id"])
                self.measure_table.setItem(i, col, item)
        fit_table(self.measure_table, stretch_column=6)
        empty_state(self.measure_table,
                    "Henüz kaydedilmiş ses hızı ölçümü yok.")

    # --- dışa aktarma -------------------------------------------------------
    def _export(self):
        """Seriyi tek klasöre çıkarır: ham CSV'ler + özet tablo + JSON.

        İnceleme için gönderilecek paket bu. Özet CSV insanın bakacağı,
        JSON çözümlemenin tamamını taşıyan biçim; ham CSV'ler olmadan sonuç
        bağımsız olarak yeniden hesaplanamaz, o yüzden üçü birlikte gidiyor.
        """
        if not self._series_id:
            return
        rows = waveform.series_captures(self._series_id)
        if not rows:
            QtWidgets.QMessageBox.information(
                self, "Dışa aktarma", "Bu seride kaydedilmiş ölçüm yok.")
            return

        target = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Dışa aktarılacak klasör")
        if not target:
            return
        folder = os.path.join(target, "seshizi_%s" % self._series_id)
        os.makedirs(folder, exist_ok=True)

        summary_path = os.path.join(folder, "ozet.csv")
        with open(summary_path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow([
                "sira", "zaman", "kalinlik_mm", "u_kalinlik_mm", "sicaklik_C",
                "hiz_m_s", "std_m_s", "U_k2_m_s", "yanki", "kestirim",
                "tutarlilik", "merkez_frekans_Hz", "ornekleme_araligi_s",
                "prob", "malzeme", "yanki_korelasyonu", "ilk_paket_atlandi",
                "simulasyon", "dosya", "sha256"])
            for r in rows:
                a = waveform.analysis_of(r) or {}
                budget = a.get("uncertainty") or {}
                writer.writerow([
                    r["series_index"], r["captured_at"], r["thickness_mm"],
                    r["thickness_u_mm"], r["temperature_c"],
                    _num(a.get("velocity")), _num(a.get("velocity_std")),
                    _num(budget.get("expanded")),
                    len(a.get("packets", [])), a.get("n_estimates"),
                    _num(a.get("coherence")),
                    _num(1.0 / a["carrier_period_s"])
                    if a.get("carrier_period_s") else "",
                    _num(a.get("sample_interval")),
                    a.get("probe"), a.get("material"), a.get("coherence"),
                    a.get("skip_first_packet"),
                    "evet" if r["is_simulated"] else "hayir",
                    os.path.basename(r["file_path"]), r["sha256"]])

        detail = []
        for r in rows:
            detail.append({
                "sira": r["series_index"],
                "zaman": r["captured_at"],
                "kalinlik_mm": r["thickness_mm"],
                "sicaklik_C": r["temperature_c"],
                "kurulum": json.loads(r["setup_json"] or "{}"),
                "cozumleme": waveform.analysis_of(r),
                "dosya": os.path.basename(r["file_path"]),
                "sha256": r["sha256"],
            })
        with open(os.path.join(folder, "cozumleme.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(detail, fh, ensure_ascii=False, indent=2, default=str)

        copied = 0
        for r in rows:
            source = r["file_path"]
            if not os.path.isfile(source):
                continue
            with open(source, "rb") as src:
                with open(os.path.join(folder, os.path.basename(source)),
                          "wb") as dst:
                    dst.write(src.read())
            copied += 1

        self.status_label.setText(
            "%d ölçüm dışa aktarıldı: %s" % (copied, folder))
        QtWidgets.QMessageBox.information(
            self, "Dışa aktarıldı",
            "%d ölçüm şu klasöre yazıldı:\n\n%s\n\n"
            "İçinde ham CSV'ler, ozet.csv ve cozumleme.json var."
            % (copied, folder))


def _num(value):
    return "" if value is None else ("%.6g" % value)


def _s(value):
    if value is None:
        return "—"
    for factor, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs(value) >= factor:
            return "%.4g %ss" % (value / factor, prefix)
    return "%.3g s" % value
