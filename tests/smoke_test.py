"""Smoke test that needs no Qt or real hardware.

Run:  python tests/smoke_test.py

Uses only the standard library; works even without PyQt/pyvisa/reportlab
installed. Verifies the core logic (database, immutability, hash chain,
statistics, simulation driver, certificate calculation).
"""

import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Use a temp file to isolate the test from the real database
_tmp = tempfile.mkdtemp(prefix="callog-test-")
from callog_common import db  # noqa: E402

db.DATA_DIR = _tmp
db.DB_PATH = os.path.join(_tmp, "test.db")

from callog_common import audit, auth, certificate  # noqa: E402
from callog_common.drivers.simulated import SimulatedDMM  # noqa: E402
from callog_common.stats import Statistics  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  [OK]   %s" % name)
    else:
        FAILED.append((name, detail))
        print("  [HATA] %s  %s" % (name, detail))


def main():
    print("\n=== CalLog duman testi ===\n")
    print("Gecici veritabani: %s\n" % db.DB_PATH)

    # --- 1. Database and seed data ------------------------------------
    print("1. Veritabani")
    conn = db.connect()
    check("sema kuruldu", conn is not None)
    insts = db.query("SELECT * FROM instruments")
    check("baslangic cihazlari eklendi (%d)" % len(insts), len(insts) >= 2)
    sim = db.query_one("SELECT * FROM instruments WHERE driver = 'simulated'")
    check("simulasyon cihazi var", sim is not None)

    # --- 2. User and authentication -----------------------------------
    print("\n2. Kimlik dogrulama")
    uid = auth.create_user("cgirgin", "Cem Girgin", "parola123", "operator")
    check("kullanici olusturuldu", uid is not None)
    check("dogru parola kabul edildi",
          auth.authenticate("cgirgin", "parola123") is not None)
    check("yanlis parola reddedildi",
          auth.authenticate("cgirgin", "yanlis") is None)
    check("buyuk/kucuk harf duyarsiz kullanici adi",
          auth.authenticate("CGirgin", "parola123") is not None)
    row = db.query_one("SELECT pwd_hash FROM users WHERE id = ?", (uid,))
    check("parola duz metin saklanmiyor", "parola123" not in row["pwd_hash"])

    approver = auth.create_user("lsorumlu", "Lab Sorumlusu", "parola456", "approver")

    # --- 3. Audit log hash chain ---------------------------------------
    print("\n3. Denetim kaydi")
    ok, bad, n = audit.verify_chain()
    check("hash zinciri saglam (%d kayit)" % n, ok, "bozuk satir: %s" % bad)

    # Deliberately corrupt the chain and verify the corruption is detected
    conn.execute("PRAGMA writable_schema = ON")   # tamper by working around the trigger
    conn.execute("DROP TRIGGER trg_audit_no_update")
    conn.commit()
    target = db.query_one("SELECT id FROM audit_log ORDER BY id LIMIT 1")
    conn.execute("UPDATE audit_log SET action = 'KURCALANDI' WHERE id = ?",
                 (target["id"],))
    conn.commit()
    ok2, bad2, _ = audit.verify_chain()
    check("kurcalama tespit edildi", (not ok2) and bad2 == target["id"])

    # Revert the corrupted record and reinstall the trigger
    conn.execute("UPDATE audit_log SET action = 'user.create' WHERE id = ?",
                 (target["id"],))
    conn.executescript(
        "CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON audit_log"
        " BEGIN SELECT RAISE(ABORT, 'audit_log tablosu degistirilemez'); END;")
    conn.commit()
    ok3, _, _ = audit.verify_chain()
    check("geri alindiktan sonra zincir yeniden saglam", ok3)

    # --- 4. Simulation driver -------------------------------------------
    print("\n4. Simulasyon surucusu")
    drv = SimulatedDMM("SIM", nominal=10.0)
    drv.connect()
    check("kimlik dondu", "8846A" in drv.identify())
    check("simulasyon bayragi", drv.is_simulated is True)
    drv.configure("VDC")
    values = []
    for _ in range(200):
        v, raw = drv.read_one()
        values.append(v)
    check("200 okuma uretildi", len(values) == 200)
    check("degerler nominale yakin (10 V +/- %%1)",
          all(9.9 < v < 10.1 for v in values),
          "min=%.6f max=%.6f" % (min(values), max(values)))

    # --- 5. Statistics ---------------------------------------------------
    print("\n5. Istatistik (Welford)")
    st = Statistics()
    for v in (1.0, 2.0, 3.0, 4.0, 5.0):
        st.add(v)
    check("n dogru", st.n == 5)
    check("ortalama dogru (3.0)", abs(st.mean - 3.0) < 1e-12, "%.15f" % st.mean)
    check("std sapma dogru (1.5811388)", abs(st.std - 1.5811388300841898) < 1e-12,
          "%.15f" % st.std)
    check("u_a dogru (s/sqrt(n))", abs(st.u_a - st.std / (5 ** 0.5)) < 1e-15)
    check("aralik dogru (4.0)", abs(st.span - 4.0) < 1e-12)

    # --- 5b. Conformity criterion -----------------------------------
    print("\n5b. Uygunluk kriteri")
    from callog_common.stats import verdict_ok
    # nominal 10, tolerance 0.01, mean 10.004, small u_a
    check("ortalama kriteri: bant icinde UYGUN",
          verdict_ok("mean", 10.0, 0.01, 10.004, 0.001, 9.995, 10.02) is True)
    check("minmax kriteri: bir okuma disarida UYGUN DEGIL",
          verdict_ok("minmax", 10.0, 0.01, 10.004, 0.001, 9.995, 10.02) is False)
    check("minmax kriteri: hepsi icerideyse UYGUN",
          verdict_ok("minmax", 10.0, 0.01, 10.004, 0.001, 9.995, 10.008) is True)
    check("ortalama kriterinde U hesaba katiliyor",
          verdict_ok("mean", 10.0, 0.01, 10.009, 0.002, 10.0, 10.02) is False,
          "0.009 + 2*0.002 = 0.013 > 0.01 olmali")
    check("tolerans isareti yok sayiliyor (mutlak)",
          verdict_ok("mean", 10.0, -0.01, 10.004, 0.001, 9.995, 10.008)
          == verdict_ok("mean", 10.0, 0.01, 10.004, 0.001, 9.995, 10.008))
    check("nominal yoksa karar verilmiyor",
          verdict_ok("mean", None, 0.01, 10.0, 0.0, 10.0, 10.0) is None)
    check("tolerans yoksa karar verilmiyor",
          verdict_ok("mean", 10.0, None, 10.0, 0.0, 10.0, 10.0) is None)

    # --- 6. Session and reading records ----------------------------------
    print("\n6. Oturum ve okumalar")
    dut_id = db.execute(
        "INSERT INTO duts (company, manufacturer, model, serial_no, device_type,"
        " created_at) VALUES (?,?,?,?,?,?)",
        ("Ornek Hastane", "Fluke", "175", "SN-TEST-001", "Multimetre", db.utc_now()))
    sess_id = db.execute(
        "INSERT INTO sessions (uuid, operator_id, dut_id, instrument_id, function,"
        " unit, nominal, tolerance, started_at, status, is_simulated,"
        " env_temp, env_rh, env_pressure, env_source)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("test-uuid-1", uid, dut_id, sim["id"], "VDC", "V", 10.0, 0.01,
         db.utc_now(), "running", 0, 23.2, 55.0, 99.3, "manual"))

    conn.executemany(
        "INSERT INTO readings (session_id, seq, ts_utc, value, unit, raw)"
        " VALUES (?,?,?,?,?,?)",
        [(sess_id, i + 1, db.utc_now(), values[i], "V", "%.8E" % values[i])
         for i in range(100)])
    conn.commit()
    cnt = db.query_one("SELECT COUNT(*) AS n FROM readings WHERE session_id = ?",
                       (sess_id,))["n"]
    check("100 okuma kaydedildi", cnt == 100)

    # --- 7. Immutability protection --------------------------------------
    print("\n7. Degismezlik (ham veri degistirilemez)")
    try:
        conn.execute("UPDATE readings SET value = 999 WHERE session_id = ?", (sess_id,))
        conn.commit()
        check("UPDATE engellendi", False, "guncelleme gecti!")
    except Exception as exc:
        check("UPDATE engellendi", "degistirilemez" in str(exc)
              or "değiştirilemez" in str(exc), str(exc))
    try:
        conn.execute("DELETE FROM readings WHERE session_id = ?", (sess_id,))
        conn.commit()
        check("DELETE engellendi", False, "silme gecti!")
    except Exception as exc:
        check("DELETE engellendi", "silinemez" in str(exc), str(exc))

    # Outlier value: not deleted, excluded via a separate table
    r = db.query_one("SELECT id FROM readings WHERE session_id = ? AND seq = 1",
                     (sess_id,))
    db.execute(
        "INSERT INTO reading_exclusions (reading_id, user_id, reason, ts_utc)"
        " VALUES (?,?,?,?)", (r["id"], uid, "Prob temassizligi", db.utc_now()))
    still = db.query_one("SELECT COUNT(*) AS n FROM readings WHERE session_id = ?",
                         (sess_id,))["n"]
    check("dislanan okuma tabloda duruyor", still == 100)

    # --- 8. Certificate calculation ---------------------------------------
    print("\n8. Sertifika hesabi")
    db.execute("UPDATE sessions SET status = 'completed', ended_at = ? WHERE id = ?",
               (db.utc_now(), sess_id))
    data = certificate.collect(sess_id)
    check("dislanan okuma hesaba katilmadi", data["n"] == 99 and data["excluded"] == 1,
          "n=%s excluded=%s" % (data["n"], data["excluded"]))
    check("ortalama nominale yakin", abs(data["mean"] - 10.0) < 0.05,
          "%.6f" % data["mean"])
    check("U = 2 * u_a", abs(data["U"] - 2 * data["u_a"]) < 1e-15)
    check("sonuc uretildi (%s)" % data["result"],
          data["result"] in ("pass", "fail", "info"))

    no = certificate.next_cert_no()
    check("sertifika no bicimi dogru (%s)" % no,
          no.startswith("CAL-MED-") and no.endswith("0001"))

    # A certificate can also be produced from a simulation session — but
    # watermarked and from a separate numbering series
    sim_sess = db.execute(
        "INSERT INTO sessions (uuid, operator_id, dut_id, instrument_id, function,"
        " unit, nominal, started_at, status, is_simulated)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("test-uuid-2", uid, dut_id, sim["id"], "VDC", "V", 10.0, db.utc_now(),
         "completed", 1))
    conn.executemany(
        "INSERT INTO readings (session_id, seq, ts_utc, value, unit, raw)"
        " VALUES (?,?,?,?,?,?)",
        [(sim_sess, i + 1, db.utc_now(), values[i], "V", "%.8E" % values[i])
         for i in range(20)])
    conn.commit()

    try:
        sim_path, sim_no, _ = certificate.build_pdf(sim_sess, approver)
    except ImportError:
        check("simulasyon sertifikasi (reportlab kurulu degil, atlandi)", True)
    else:
        check("simulasyon sertifikasi SIM- serisinden numara aldi (%s)" % sim_no,
              sim_no.startswith("SIM-CAL-MED-"), sim_no)
        check("simulasyon sertifikasi PDF olarak uretildi",
              os.path.exists(sim_path) and os.path.getsize(sim_path) > 2000,
              "boyut=%d" % (os.path.getsize(sim_path)
                            if os.path.exists(sim_path) else 0))
        check("resmi numara serisi tuketilmedi", certificate.next_cert_no() == no,
              "%s != %s" % (certificate.next_cert_no(), no))

    # --- 9. Oscilloscope: driver and waveform capture --------------------
    print("\n9. Osiloskop entegrasyonu (Qt'siz)")
    from callog_common import drivers, waveform
    from callog_common.drivers.keysight_dsox1202a import KeysightDSOX1202A

    check("osiloskop surucusu kayitli", "dsox1202a" in drivers.REGISTRY)
    check("dalga destegi bayragi dogru",
          drivers.supports_waveform("dsox1202a")
          and not drivers.supports_waveform("fluke8846a"))

    scope = drivers.create("simulated_scope", "SIM", trigger_delay=0.01)
    scope.connect()
    scope.configure("VPP", channel="CHANnel1")
    vpp, _raw = scope.read_one()
    check("simulasyon VPP olcumu makul", abs(vpp - 2.0) < 0.05, "vpp=%.4f" % vpp)

    # If 9.9E+37 is silently accepted, the mean jumps to 1e37 and corrupts the session.
    real = KeysightDSOX1202A("USB0::TEST::INSTR")
    real._function = "VPP"
    real._query = lambda cmd: "+9.90000E+37"
    try:
        real.read_one()
        rejected = False
    except drivers.InstrumentError:
        rejected = True
    check("gecersiz olcum (9.9E+37) reddediliyor", rejected)

    scope.arm()
    fired = scope.wait_trigger(timeout_s=2)
    check("tetikleme bekleme donuyor", fired)

    # Waveform data is produced as a numpy array. The scalar measurement and
    # trigger logic were already verified above without numpy; the
    # point-data part only runs if numpy is installed.
    try:
        import numpy  # noqa: F401
        has_numpy = True
    except ImportError:
        has_numpy = False

    if not has_numpy:
        check("dalga yakalama (numpy kurulu degil, atlandi)", True)
        scope.close()
        return _summary()

    times, volts = scope.read_waveform("CHANnel1", points=200)
    check("dalga verisi okundu", len(times) == 200 and len(volts) == 200)

    # The stop flag must interrupt the wait — otherwise, in a setup where
    # the trigger never fires, the "Durdur" (Stop) button does nothing.
    scope.arm()
    stopped = scope.wait_trigger(timeout_s=5, should_stop=lambda: True)
    check("durdurma bayragi beklemeyi kesiyor", stopped is False)

    # The default folder must be evaluated at call time: if it were a
    # module-level variable, it would capture the test's temp data folder
    # too early and captures would be written into the real project folder.
    check("varsayilan klasor test veri klasorunun altinda",
          waveform.default_dir().startswith(_tmp), waveform.default_dir())

    wdir = os.path.join(_tmp, "dalgalar")
    n = waveform.write_csv(os.path.join(wdir, "t.csv"), times,
                           {"CH1_V": volts})
    check("CSV yazildi", n == 200 and os.path.isfile(os.path.join(wdir, "t.csv")))
    back_t, back_c = waveform.read_csv(os.path.join(wdir, "t.csv"))
    check("CSV geri okundu", len(back_t) == 200 and "CH1_V" in back_c)
    check("geri okunan deger ayni",
          abs(back_c["CH1_V"][5] - float(volts[5])) < 1e-6)

    at, ac = waveform.align(times, {"A": volts, "B": volts[:120]})
    check("farkli uzunluktaki kanallar kirpiliyor",
          len(at) == 120 and all(len(v) == 120 for v in ac.values()))

    inst_id = db.query_one(
        "SELECT id FROM instruments WHERE driver = 'simulated_scope'")["id"]
    cap_id = waveform.save(times, {"CH1_V": volts}, instrument_id=inst_id,
                           operator_id=uid, outdir=wdir, is_simulated=True)
    state, _msg = waveform.verify(cap_id)
    check("yakalama kaydedildi ve dogrulandi", state == "ok", state)
    with open(waveform.get(cap_id)["file_path"], "a", encoding="utf-8") as fh:
        fh.write("0,0\n")
    state, _msg = waveform.verify(cap_id)
    check("kurcalanan CSV tespit edildi", state == "changed", state)
    scope.close()

    # --- 10. Defibrillator test mode --------------------------------------
    print("\n12. Dalga sertifikasyonu")
    from callog_common import certificate as cert_mod

    # A certificate is tied to either a session or a series; both can't be
    # empty or both filled. Without this constraint, rows would exist whose
    # subject is ambiguous, or two measurements could share one number.
    try:
        db.execute("INSERT INTO certificates (cert_no, issued_at, issued_by,"
                   " result) VALUES ('X-1', ?, 1, 'info')", (db.utc_now(),))
        check("kaynaksiz sertifika reddediliyor", False, "eklendi")
    except Exception:
        check("kaynaksiz sertifika reddediliyor", True)
    try:
        db.execute("INSERT INTO certificates (session_id, series_id, cert_no,"
                   " issued_at, issued_by, result)"
                   " VALUES (?, 'SER-X', 'X-2', ?, 1, 'info')",
                   (sess_id, db.utc_now()))
        check("cift kaynakli sertifika reddediliyor", False, "eklendi")
    except Exception:
        check("cift kaynakli sertifika reddediliyor", True)

    cid = cert_mod.register_series("SER-TEST-1", "SERI-TEST-0001", uid,
                                   "pass", "/yok/x.pdf", "abc")
    row = cert_mod.for_series("SER-TEST-1")
    check("seri sertifikasi kaydedildi", row is not None and row["id"] == cid)
    check("seri sertifikasinda oturum yok", row["session_id"] is None)
    check("sonuc kaydedildi", row["result"] == "pass")

    # Regenerating must update the same row and drop the approval. The
    # approval is written directly here: `approve()` requires lab-manager
    # authority and this test's operator doesn't have it — the permission
    # rule is exercised separately in the UI test.
    db.execute("UPDATE certificates SET approved_by = ?, approved_at = ?"
               " WHERE id = ?", (uid, db.utc_now(), cid))
    check("onay bilgisi yazildi",
          cert_mod.for_series("SER-TEST-1")["approved_at"] is not None)
    cert_mod.register_series("SER-TEST-1", "SERI-TEST-0002", uid, "fail",
                             "/yok/y.pdf", "def")
    again = cert_mod.for_series("SER-TEST-1")
    check("yeniden uretim ayni satiri guncelledi", again["id"] == cid)
    check("yeni numara yazildi", again["cert_no"] == "SERI-TEST-0002")
    check("icerik degisince onay dusuruldu", again["approved_at"] is None)
    check("seri sertifikasi bir kez sayiliyor",
          db.query_one("SELECT COUNT(*) AS n FROM certificates"
                       " WHERE series_id = 'SER-TEST-1'")["n"] == 1)

    # The screenshot is now taken in the capture worker thread: the
    # `captured` signal must carry four values, or the UI connects with the old signature.
    from callog_common.acquisition import WaveformWorker
    check("yakalama gecikmesi varsayilani makul",
          0.2 <= WaveformWorker.DEFAULT_SHOT_DELAY_S <= 2.0,
          str(WaveformWorker.DEFAULT_SHOT_DELAY_S))

    # --- 13. Stability and outlier readings -------------------------------
    print("\n13. Kararlilik ve aykiri okuma")
    from callog_common import stability

    check("az okumada karar verilmiyor",
          stability.assess([1.0, 1.0, 1.0])["state"] == stability.UNKNOWN)

    # There's oscillation but no directional drift -> stable
    settled = [10.0 + (0.001 if i % 2 else -0.001) for i in range(20)]
    check("salinan ama kaymayan okuma kararli",
          stability.assess(settled)["state"] == stability.STABLE)

    # A steadily climbing reading: no noise, but drift -> settling
    ramp = [10.0 + 0.01 * i for i in range(20)]
    ramp_info = stability.assess(ramp)
    check("tirmanan okuma oturuyor sayiliyor",
          ramp_info["state"] == stability.DRIFTING, ramp_info["state"])
    check("egim birim/saniye olarak dogru",
          abs(ramp_info["slope"] - 0.01) < 1e-9, str(ramp_info["slope"]))
    check("pencere boyunca birikim dogru",
          abs(ramp_info["drift"] - 0.19) < 1e-9, str(ramp_info["drift"]))
    check("okuma araligi egimi olceklendiriyor",
          abs(stability.assess(ramp, interval_s=2.0)["slope"] - 0.005) < 1e-9)

    # When a tolerance is given, it compares the scatter against the tolerance band
    scattered = [10.0 + (0.5 if i % 2 else -0.5) for i in range(20)]
    check("bandan genis sacilim isaretleniyor",
          stability.assess(scattered, tolerance=0.2)["state"] == stability.NOISY)
    check("tolerans yoksa sacilim karari verilmiyor",
          stability.assess(scattered)["state"] == stability.STABLE)

    planted = [10.0 + 0.001 * (i % 3) for i in range(20)]
    planted[7] = 12.0
    found = stability.outliers(planted)
    check("aykiri okuma bulundu", found == [7], str(found))
    check("temiz dizide aykiri yok", stability.outliers(planted[:7]) == [])
    check("sifir sapmada aykiri aranmiyor",
          stability.outliers([5.0] * 20) == [])
    check("az okumada aykiri aranmiyor",
          not stability.is_outlier(99.0, 10.0, 0.01, n=3))
    check("canli aykiri kontrolu calisiyor",
          stability.is_outlier(99.0, 10.0, 0.01, n=30))

    # --- 14. Trend and limit-crossing prediction --------------------------
    print("\n14. Egilim ve sinir asimi")
    from datetime import date as _date
    from callog_common import trend

    check("tarih ayristirildi",
          trend.parse_day("2026-08-11T07:03:22+00:00") == _date(2026, 8, 11))
    check("bozuk tarih None donuyor", trend.parse_day("gecersiz") is None)

    slope, intercept, r2 = trend.fit([0, 1, 2, 3], [1.0, 3.0, 5.0, 7.0])
    check("dogrusal uyum egimi", abs(slope - 2.0) < 1e-9)
    check("dogrusal uyum kesisimi", abs(intercept - 1.0) < 1e-9)
    check("kusursuz uyumda r2 = 1", abs(r2 - 1.0) < 1e-9)
    check("tum x ayniysa uyum yok", trend.fit([2, 2, 2], [1.0, 2.0, 3.0]) is None)

    check("iki noktada egilim cizilmiyor",
          trend.analyse([("2024-01-01", 10.0), ("2025-01-01", 10.4)]) is None)

    # An instrument drifting +0.4 units per year
    drifting = [("2023-01-01", 10.0), ("2024-01-01", 10.4),
                ("2025-01-01", 10.8), ("2026-01-01", 11.2)]
    info = trend.analyse(drifting, nominal=10.0, tolerance=2.0,
                         today=_date(2026, 1, 1))
    check("yillik suruklenme hesaplandi",
          abs(info["slope_per_year"] - 0.4) < 0.01, str(info["slope_per_year"]))
    check("uzun gozlem araligi guvenilir sayiliyor", info["reliable"])
    check("sinir asimi tahmin edildi", info["crossing"] is not None)
    check("tahmin edilen sinir dogru",
          abs(info["crossing"]["limit"] - 12.0) < 1e-9)
    # 11.2 to 12.0 is 0.8 units, at 0.4/year -> roughly two years
    check("asim zamani makul",
          1.8 < info["crossing"]["years"] < 2.2,
          str(info["crossing"]["years"]))
    check("asim tarihi bugunden sonra",
          info["crossing"]["date"] > _date(2026, 1, 1))

    flat = [("2023-01-01", 10.0), ("2024-01-01", 10.0), ("2025-01-01", 10.0)]
    check("kaymayan cihazda tahmin yok",
          trend.analyse(flat, nominal=10.0, tolerance=1.0)["crossing"] is None)
    check("tolerans yoksa tahmin yok",
          trend.analyse(drifting, nominal=10.0)["crossing"] is None)

    outside = trend.analyse(drifting, nominal=10.0, tolerance=0.1,
                            today=_date(2026, 1, 1))
    check("zaten bant disindaki cihaz isaretleniyor",
          outside["crossing"]["already_out"])

    short = [("2026-01-01", 10.0), ("2026-01-02", 10.1), ("2026-01-03", 10.2)]
    check("kisa gozlemde tahmin guvenilmez sayiliyor",
          not trend.analyse(short, nominal=10.0, tolerance=1.0)["reliable"])
    check("ozet metni uretiliyor",
          "yıl" in trend.summary_tr(info, "V"))

    # --- 15. Backup ---------------------------------------------------
    print("\n15. Yedekleme")
    from callog_common import backup

    check("yedek yokken yas bilinmiyor", backup.age_days() is None)
    check("yedek yokken metin aciklayici",
          "alınmamış" in backup.age_text())

    path = backup.create(uid)
    check("yedek dosyasi olustu", os.path.isfile(path))
    check("yedek listeleniyor", len(backup.list_backups()) == 1)
    check("yedek yasi sifira yakin", backup.age_days() < 0.01)

    # The copy must actually be usable: a plain file copy in WAL mode leaves
    # data missing, which is what this test really checks for.
    import sqlite3 as _sqlite3
    copy = _sqlite3.connect(path)
    copied_users = copy.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    copied_certs = copy.execute("SELECT COUNT(*) FROM certificates").fetchone()[0]
    copy.close()
    live_users = db.query_one("SELECT COUNT(*) AS n FROM users")["n"]
    check("yedekte kullanicilar var", copied_users == live_users,
          "%d != %d" % (copied_users, live_users))
    check("yedekte sertifikalar var", copied_certs > 0)
    check("yedek denetim kaydina gecti",
          db.query_one("SELECT COUNT(*) AS n FROM audit_log"
                       " WHERE action = 'db.backup'")["n"] == 1)

    for _ in range(3):
        backup.create(uid, keep=2)
    check("eski yedekler budandi", len(backup.list_backups()) == 2,
          str(len(backup.list_backups())))

    # --- 16. Notification center -------------------------------------
    print("\n16. Bildirim merkezi")
    from callog_common import notifications, perms

    keys = lambda role: {n["key"] for n in notifications.collect(role)}

    # There's a certificate pending approval (created in section 12)
    check("onaylayan onay kuyrugunu goruyor", "cert_pending" in keys("approver"))
    check("operatore onay kuyrugu gosterilmiyor",
          "cert_pending" not in keys("operator"))

    db.execute("UPDATE instruments SET cal_due = '2020-01-01', is_active = 1"
               " WHERE driver = 'fluke8846a'")
    check("suresi dolmus referans bildiriliyor",
          "cal_expired" in keys("operator"))
    db.execute("UPDATE instruments SET cal_due = NULL WHERE driver = 'fluke8846a'")
    check("kalibrasyon tarihi yoksa bildiriliyor",
          "cal_unknown" in keys("operator"))
    check("simulasyon cihazi kalibrasyon uyarisi uretmiyor",
          all("SIM-" not in n["detail"]
              for n in notifications.collect("operator")
              if n["key"].startswith("cal_")))

    auth.authenticate("boyle-bir-kullanici-yok", "parola")
    check("basarisiz giris yoneticiye bildiriliyor",
          "login_failed" in keys("admin"))
    check("basarisiz giris operatore bildirilmiyor",
          "login_failed" not in keys("operator"))

    check("bildirimler onem sirasina gore",
          [n["level"] for n in notifications.collect("admin")]
          == sorted((n["level"] for n in notifications.collect("admin")),
                    key=lambda l: {"bad": 0, "warn": 1, "info": 2}[l]))
    check("her bildirimin hedefi var",
          all(n["target"] for n in notifications.collect("admin")))

    # --- 17. Permission matrix ------------------------------------------
    print("\n17. Yetki matrisi")
    rows = perms.matrix()
    check("matris satirlari uretildi", len(rows) >= 15, str(len(rows)))
    listed = {permission for _g, permission, _l, _a in rows}
    check("her yetki matriste",
          listed == set(perms._TABLE.keys()),
          str(set(perms._TABLE.keys()) - listed))
    by_perm = {p: a for _g, p, _l, a in rows}
    check("sertifika onayi operatorde yok",
          not by_perm[perms.CERT_APPROVE][perms.OPERATOR])
    check("sertifika onayi lab sorumlusunda var",
          by_perm[perms.CERT_APPROVE][perms.APPROVER])
    check("kullanici yonetimi yalnizca yoneticide",
          by_perm[perms.USER_MANAGE] == {perms.OPERATOR: False,
                                         perms.APPROVER: False,
                                         perms.ADMIN: True})

    # --- 18. Multiple measurement points (measurement plan) --------------
    print("\n18. Olcum plani")
    from callog_common import points

    # A legacy session with no plan: a single-point plan must be set up automatically
    legacy = points.ensure_default(sess_id)
    check("plansiz oturuma tek noktali plan kuruldu", len(legacy) == 1)
    check("nokta oturum sutunlarindan uretildi",
          legacy[0]["function"] == db.query_one(
              "SELECT function FROM sessions WHERE id = ?",
              (sess_id,))["function"])
    check("ikinci cagri yeni nokta acmiyor",
          len(points.ensure_default(sess_id)) == 1)

    orphan = points.summarize(legacy[0], is_first=True)
    check("sahipsiz okumalar ilk noktaya sayiliyor", orphan["n"] > 0,
          "n=%d" % orphan["n"])

    # New three-point session
    import uuid as _uuid
    dut_id = db.query_one("SELECT id FROM duts LIMIT 1")["id"]
    inst_id = db.query_one(
        "SELECT id FROM instruments WHERE driver = 'simulated'")["id"]
    plan_sid = db.execute(
        "INSERT INTO sessions (uuid, name, operator_id, dut_id, instrument_id,"
        " function, unit, nominal, tolerance, tolerance_mode, started_at,"
        " ended_at, status, is_simulated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(_uuid.uuid4()), "Plan testi", uid, dut_id, inst_id, "VDC", "V",
         10.0, 0.05, "mean", db.utc_now(), db.utc_now(), "completed", 1))
    p1 = points.create(plan_sid, 1, "VDC", "V", 10.0, 0.05)
    p2 = points.create(plan_sid, 2, "VDC", "V", 1.0, 0.001)
    p3 = points.create(plan_sid, 3, "RES", "Ω", 1000.0, None)

    conn = db.connect()
    for pid, base, spread, n in ((p1, 10.0, 0.001, 12),
                                 (p2, 1.02, 0.0002, 10),
                                 (p3, 1000.0, 0.05, 8)):
        conn.executemany(
            "INSERT INTO readings (session_id, point_id, seq, ts_utc, value,"
            " unit, elapsed_s) VALUES (?,?,?,?,?,?,?)",
            [(plan_sid, pid, i, db.utc_now(),
              base + spread * (1 if i % 2 else -1), "V", float(i))
             for i in range(n)])
    conn.commit()

    summaries = points.collect(plan_sid)
    check("uc nokta listelendi", len(summaries) == 3, str(len(summaries)))
    check("okumalar noktalara ayrildi",
          [s["n"] for s in summaries] == [12, 10, 8],
          str([s["n"] for s in summaries]))
    check("her nokta kendi ortalamasini veriyor",
          abs(summaries[0]["mean"] - 10.0) < 1e-9
          and abs(summaries[1]["mean"] - 1.02) < 1e-9)
    check("bant icindeki nokta uygun", summaries[0]["result"] == "pass")
    check("bant disindaki nokta uygun degil", summaries[1]["result"] == "fail",
          summaries[1]["result"])
    check("tolerans girilmemis nokta bilgilendirme",
          summaries[2]["result"] == "info")
    check("bir nokta bile uygunsuzsa belge uygunsuz",
          points.overall_result(summaries) == "fail")
    check("hepsi bilgilendirmeyse sonuc bilgilendirme",
          points.overall_result([summaries[2]]) == "info")
    check("nokta etiketi nominal ve fonksiyon tasiyor",
          points.label(summaries[0]) == "10 V (VDC)",
          points.label(summaries[0]))

    d = certificate.collect(plan_sid)
    check("sertifika coklu nokta oldugunu biliyor", d["multi"])
    check("toplam okuma sayisi dogru", d["total_n"] == 30, str(d["total_n"]))
    check("ust duzey alanlar ilk noktanin", d["n"] == 12 and d["nominal"] == 10.0)
    check("sertifika sonucu oturumun tamami icin", d["result"] == "fail")

    _cert_no, _sim, body, _verdict, _note, _sig, _d = certificate.sections(
        plan_sid)
    titles = [title for title, _rows in body]
    check("gövdede plan ozeti var", "Ölçüm planı" in titles, str(titles))
    check("her nokta kendi bolumunu aldi",
          sum(1 for x in titles if x.startswith("Ölçüm noktası")) == 3,
          str(titles))

    points.start(p2)
    check("nokta olculuyor olarak isaretlendi",
          points.get(p2)["status"] == points.RUNNING)
    check("siradaki bekleyen nokta bulunuyor",
          points.next_pending(plan_sid, after_seq=2)["seq"] == 3)
    points.finish(p2)
    check("nokta tamamlandi olarak kapandi",
          points.get(p2)["status"] == points.DONE
          and points.get(p2)["ended_at"] is not None)

    points.sync_session(plan_sid)
    srow = db.query_one("SELECT * FROM sessions WHERE id = ?", (plan_sid,))
    check("oturum sutunlari ilk noktayi yansitiyor",
          srow["nominal"] == 10.0 and srow["function"] == "VDC")

    # --- 19. Measurement templates -------------------------------------
    print("\n19. Olcum sablonlari")
    from callog_common import templates

    plan = [{"function": "VDC", "unit": "V", "nominal": 10.0,
             "tolerance": 0.05, "tolerance_mode": "mean"},
            {"function": "VDC", "unit": "V", "nominal": 100.0,
             "tolerance": 0.5, "tolerance_mode": "minmax"}]
    tid = templates.save("Fluke 175 yillik", plan, driver="simulated",
                         interval_s=0.5, nplc="10", user_id=uid)
    check("sablon kaydedildi", templates.get(tid) is not None)
    check("nokta plani geri okunuyor",
          len(templates.points_of(templates.get(tid))) == 2)
    check("ada gore bulunuyor",
          templates.by_name("Fluke 175 yillik")["id"] == tid)
    check("okuma periyodu saklandi", templates.get(tid)["interval_s"] == 0.5)

    same = templates.save("Fluke 175 yillik", plan[:1], user_id=uid)
    check("ayni ad ikinci satir acmiyor", same == tid)
    check("uzerine yazildi",
          len(templates.points_of(templates.get(tid))) == 1)

    try:
        templates.save("", plan, user_id=uid)
        empty_name_ok = True
    except ValueError:
        empty_name_ok = False
    check("adsiz sablon reddediliyor", not empty_name_ok)
    try:
        templates.save("Bos plan", [], user_id=uid)
        empty_plan_ok = True
    except ValueError:
        empty_plan_ok = False
    check("bos plan reddediliyor", not empty_plan_ok)

    check("surucuye gore suzuluyor",
          any(r["id"] == tid for r in templates.list_all("simulated")))
    templates.delete(tid, uid)
    check("sablon silindi", templates.get(tid) is None)
    check("silme denetim kaydina gecti",
          db.query_one("SELECT COUNT(*) AS n FROM audit_log"
                       " WHERE action = 'template.delete'")["n"] == 1)

    # --- 20. Per-user preferences, theme scale, language -----------------
    print("\n20. Tercihler, erisilebilirlik, dil")
    from callog_common import i18n, prefs, theme

    other = auth.create_user("tercih2", "Tercih Iki", "parola123", "operator")
    check("varsayilan tema beyaz", prefs.get(uid, prefs.THEME) == "light")
    prefs.set(uid, prefs.THEME, "dark")
    prefs.set(other, prefs.THEME, "contrast")
    check("tercih kullaniciya yazildi", prefs.get(uid, prefs.THEME) == "dark")
    check("ikinci kullanici etkilenmedi",
          prefs.get(other, prefs.THEME) == "contrast")
    prefs.set(uid, prefs.THEME, "light")
    check("uzerine yazma calisiyor", prefs.get(uid, prefs.THEME) == "light")
    check("tum tercihler varsayilanla tamamlaniyor",
          set(prefs.all_for(uid)) == set(prefs.DEFAULTS))
    check("sayisal tercih okunuyor",
          prefs.get_float(uid, prefs.FONT_SCALE, 1.0) == 1.0)

    theme.bind_user(uid)
    check("yuksek kontrast paleti tanimli", theme.CONTRAST in theme.TOKENS)
    check("yuksek kontrastta soluk metin yok",
          theme.TOKENS[theme.CONTRAST]["text_muted"]
          == theme.TOKENS[theme.CONTRAST]["text"])
    sheet = theme.stylesheet(theme.LIGHT)
    check("olcek 1.0'da stil sayfasi degismiyor",
          theme.scale_stylesheet(sheet, 1.0) == sheet)
    bigger = theme.scale_stylesheet("QLabel { font-size: 13px; }", 1.5)
    check("yazi boyutu carpanla buyuyor", "font-size: 20px" in bigger, bigger)
    theme.set_font_scale(1.3)
    check("olcek kullaniciya yazildi", abs(theme.font_scale() - 1.3) < 0.01)
    theme.set_font_scale(9.0)
    check("olcek ust sinirda kirpiliyor",
          abs(theme.font_scale() - theme.MAX_SCALE) < 0.01)
    theme.set_font_scale(1.0)

    check("varsayilan dil turkce", i18n.language() == i18n.TR)
    check("turkcede metin oldugu gibi", i18n.t("Sonuç") == "Sonuç")
    i18n.set_language(i18n.EN, uid)
    check("ingilizceye gecildi", i18n.language() == i18n.EN)
    check("katalogdaki metin cevriliyor", i18n.t("Sonuç") == "Result")
    check("sertifika basligi cevriliyor",
          i18n.t("KALİBRASYON SERTİFİKASI") == "CALIBRATION CERTIFICATE")
    check("katalogda olmayan metin aynen kaliyor",
          i18n.t("Buna karsilik yok") == "Buna karsilik yok")
    check("dil tercihi kullaniciya yazildi",
          prefs.get(uid, prefs.LANGUAGE) == "en")

    en_no, _sim, en_body, en_verdict, _n, en_sig, _d = certificate.sections(
        plan_sid)
    en_titles = [title for title, _rows in en_body]
    check("ingilizce sertifikada bolum basliklari cevrili",
          "Device under calibration" in [i18n.t(x) for x in en_titles],
          str(en_titles))
    check("ingilizce sonuc metni", en_verdict == "FAIL", en_verdict)
    check("etiketler ceviriden geciyor",
          i18n.t("Seri no") == "Serial no")
    check("imza satiri cevriliyor",
          i18n.t(en_sig[0][0]) == "Measured by")

    i18n.set_language(i18n.TR, uid)
    check("turkceye donuldu", i18n.language() == i18n.TR)

    # --- 21. Global search ----------------------------------------------
    print("\n21. Genel arama")
    from callog_common import search

    check("tek karakterde arama yapilmiyor", search.find("A") == [])
    check("bos terimde arama yapilmiyor", search.find("  ") == [])

    dut_row = db.query_one("SELECT * FROM duts WHERE id = ?", (dut_id,))
    hits = search.find(dut_row["serial_no"])
    check("seri no ile cihaz bulunuyor",
          any(h["kind"] == "dut" and h["id"] == dut_id for h in hits),
          str([(h["kind"], h["id"]) for h in hits]))
    check("ayni terim oturumu da getiriyor",
          any(h["kind"] == "session" for h in hits))

    cert_row = db.query_one("SELECT cert_no FROM certificates"
                            " WHERE cert_no NOT LIKE 'SERI%' LIMIT 1")
    cert_hits = search.find(cert_row["cert_no"])
    check("sertifika numarasi ile bulunuyor",
          any(h["kind"] == "certificate" and h["id"] == cert_row["cert_no"]
              for h in cert_hits))
    check("sonuclar tur sirasina gore",
          [search.KIND_ORDER.index(h["kind"]) for h in hits]
          == sorted(search.KIND_ORDER.index(h["kind"]) for h in hits))
    check("her sonucun hedefi var", all(h["target"] for h in hits))
    check("referans cihaz da aranabiliyor",
          any(h["kind"] == "instrument"
              for h in search.find("8846A")))

    # --- 22b. Batch evaluation report -------------------------------------
    print("\n22. Yol onarımı")
    from callog_common import waveform as wave_mod

    times = [0.0, 0.001, 0.002]
    cap_id = wave_mod.save(times, {"CH1_V": [0.1, 0.2, 0.3]},
                           instrument_id=inst_id, operator_id=uid)
    real_path = db.query_one(
        "SELECT file_path FROM waveform_captures WHERE id = ?",
        (cap_id,))["file_path"]
    check("test kaydının dosyası gerçekten var", os.path.isfile(real_path))

    # We corrupt the stored path to make DATA_DIR look like an "old
    # location" — exactly the situation that arises when the project folder is moved.
    stale_path = real_path.replace(db.DATA_DIR, r"C:\eski\konum\data", 1)
    db.execute("UPDATE waveform_captures SET file_path = ? WHERE id = ?",
              (stale_path, cap_id))
    fixed = db._reconcile_paths(db.connect())
    check("onarım en az bir satırı düzeltti", fixed >= 1, str(fixed))
    healed_path = db.query_one(
        "SELECT file_path FROM waveform_captures WHERE id = ?",
        (cap_id,))["file_path"]
    check("bozuk yol güncel DATA_DIR altında onarıldı",
          healed_path == real_path, healed_path)
    check("onarım denetim kaydına geçti",
          db.query_one("SELECT COUNT(*) AS n FROM audit_log"
                       " WHERE action = 'db.path_reconcile'")["n"] >= 1)

    return _summary()


def _summary():
    print("\n" + "=" * 52)
    print("Gecen: %d    Kalan: %d" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("\nBasarisiz testler:")
        for name, detail in FAILED:
            print("  - %s  %s" % (name, detail))
    print("=" * 52 + "\n")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
