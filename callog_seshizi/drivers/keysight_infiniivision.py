"""Keysight InfiniiVision osiloskop ailesi icin ortak surucu.

Bu ailenin (1200 X, 2000 X, 3000 X/T ...) SCPI komut kumesi buyuk olcude
ortaktir; modeller arasindaki fark kimlik dizesi, kanal sayisi ve bellek /
ornekleme siniridir. Ortak davranis burada durur, model sinifi yalnizca
`MODEL_TOKENS` ve `MODEL_NAME` degerlerini doldurur.

Neden ayri bir taban: ikinci bir osiloskop eklenirken tek secenek 500
satirlik surucuyu kopyalamakti; kopyanin icindeki iki tuzak duzeltmesi
(asagida anlatilan +9.9E+37 ve durdurulmus cihaz sorunlari) zamanla
birbirinden ayrisirdi.

İki yeteneği var ve ikisi farklı veri üretiyor:

* **Skaler ölçüm** — ``:MEASure:VPP?`` gibi sorgular okuma başına tek sayı
  döndürür. Uygulamanın oturum / istatistik / sertifika akışı bunun üzerine
  kurulu ve değişmeden çalışır. Osiloskop zaten bu büyüklükler (dikey
  doğruluk, zaman tabanı doğruluğu, yükselme süresi) üzerinden kalibre edilir.
* **Dalga yakalama** — tetikleme başına binlerce (t, V) noktası. Bunlar aynı
  büyüklüğün tekrarlı ölçümü değil, tek bir olayın örnekleri; ``readings``
  tablosuna yazılmaz, CSV dosyası olarak saklanır (bkz. ``waveform.py``).

Arayüz notları
--------------
* USB (arka paneldeki "USB Device" portu) üzerinden çalışır. Keysight IO
  Libraries Suite (VISA) kurulu olmalı.
* ``*IDN?`` yanıtı: ``KEYSIGHT TECHNOLOGIES,DSO-X 1202A,CN00000000,02.xx.xxxx``
  Eski aygıt yazılımlarında marka alanı ``AGILENT TECHNOLOGIES`` olabilir.
* İki analog kanal (CHANnel1, CHANnel2).

Bu sürücüde iki tuzak açıkça ele alınıyor; ikisi de sessizce yanlış veri üretir:

1. **+9.9E+37 = "ölçüm yapılamadı".** Sinyal yoksa, tetikleme kaçtıysa ya da
   büyüklük ekranda görünmüyorsa cihaz hata vermez, bu sayıyı döndürür. Ham
   haliyle kaydedilirse ortalama 1e37'ye fırlar ve tüm oturum çöpe gider.
   ``read_one`` bunu yakalayıp ``InstrumentError`` yükseltiyor.
2. **Durdurulmuş cihazda ölçüm donar.** ``:STOP`` durumundayken
   ``:MEASure:VPP?`` her seferinde *aynı* değeri döndürür. Uygulama bunu
   N tekrarlı okuma sanar, standart sapmayı sıfır, belirsizliği sıfır
   hesaplar — gerçekte hiç ölçüm yapılmamıştır. ``configure`` cihazı
   ``:RUN`` durumuna alıyor ve ``read_one`` edinimin ilerlediğini
   doğruluyor.
"""

import threading
import time

from callog_common.drivers.base import InstrumentError, MeasurementFunction, WaveformDriver

#: Cihazın "ölçüm yapılamadı" değeri. Eşik biraz altında: bazı aygıt
#: yazılımları 9.99999E+37 döndürüyor.
INVALID_MEASUREMENT = 9.9e37
INVALID_THRESHOLD = 9.0e37

#: Operation Status Condition Register, bit 3 = Run/acquiring
OPER_RUN_BIT = 1 << 3


class InfiniiVisionScope(WaveformDriver):

    #: *IDN? yanitinda aranacak desenler. Hepsinin degil, **herhangi
    #: birinin** bulunmasi yeterli: eski aygit yazilimlarinda marka alani
    #: AGILENT olarak gelebiliyor, o yuzden model deseni tek basina da
    #: eslesebilmeli.
    MODEL_TOKENS = ()

    #: Hata mesajinda gorunecek model adi.
    MODEL_NAME = "InfiniiVision"


    FUNCTIONS = [
        MeasurementFunction("VPP", "Tepeden tepeye gerilim", "V"),
        MeasurementFunction("VAMP", "Genlik (tepe–taban)", "V"),
        MeasurementFunction("VMAX", "En büyük gerilim", "V"),
        MeasurementFunction("VMIN", "En küçük gerilim", "V"),
        MeasurementFunction("VAVG", "Ortalama gerilim (DC)", "V"),
        MeasurementFunction("VRMS", "RMS gerilim", "V"),
        MeasurementFunction("FREQ", "Frekans", "Hz"),
        MeasurementFunction("PER", "Periyot", "s"),
        MeasurementFunction("RISE", "Yükselme süresi", "s"),
        MeasurementFunction("FALL", "Düşme süresi", "s"),
        MeasurementFunction("PWID", "Pozitif darbe genişliği", "s"),
        MeasurementFunction("DUTY", "Görev çevrimi", "%"),
    ]

    CHANNELS = (("CHANnel1", "Kanal 1"), ("CHANnel2", "Kanal 2"))

    #: Fonksiyon anahtarı → :MEASure: sorgusu.
    #: VAVerage ve VRMS ek argüman ister; onlar aşağıda özel olarak kuruluyor.
    _MEASURE = {
        "VPP": "VPP",
        "VAMP": "VAMPlitude",
        "VMAX": "VMAX",
        "VMIN": "VMIN",
        "FREQ": "FREQuency",
        "PER": "PERiod",
        "RISE": "RISetime",
        "FALL": "FALLtime",
        "PWID": "PWIDth",
        "DUTY": "DUTYcycle",
    }

    def __init__(self, address, channel="CHANnel1", timeout_ms=15000, **kwargs):
        WaveformDriver.__init__(self, address, **kwargs)
        self.channel = _normalize_channel(channel)
        self.timeout_ms = timeout_ms
        self._inst = None
        self._rm = None
        self._lock = threading.Lock()

    # --- yaşam döngüsü -------------------------------------------------
    def connect(self):
        import pyvisa

        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(self.address)
        self._inst.timeout = self.timeout_ms
        self._inst.read_termination = "\n"
        self._inst.write_termination = "\n"

        # Yarim kalmis bir aktarim USBTMC uc noktasini asili birakiyor ve
        # sonraki her istek zaman asimina dusuyor -- uygulama duzgun
        # kapanmadiginda tipik olarak bu oluyor. `clear` cihaz tarafindaki
        # kuyrugu bosaltip oturumu yeniden kullanilir hale getiriyor.
        # Desteklemeyen arayuzlerde sessizce atlaniyor: kritik bir adim
        # degil, kurtarma adimi.
        try:
            self._inst.clear()
        except Exception:
            pass

        # Asili kalmis bir uc nokta ilk sorguyu `VI_ERROR_INP_PROT_VIOL` ile
        # dusuruyor; temizleme o istegi tuketip oturumu duzeltiyor ve ikinci
        # sorgu geciyor. Tek deneme birakmak, uygulama duzgun kapanmadiktan
        # sonraki ilk baglantiyi her seferinde basarisiz kilardi.
        try:
            self.identity = self.identify()
        except Exception:
            try:
                self._inst.clear()
            except Exception:
                pass
            self.identity = self.identify()
        up = self.identity.upper()
        if self.MODEL_TOKENS and not any(t in up for t in self.MODEL_TOKENS):
            raise InstrumentError(
                "Beklenen cihaz %s değil. Gelen yanıt: %s"
                % (self.MODEL_NAME, self.identity))

        self._write("*CLS")
        return self.identity

    def close(self):
        with self._lock:
            if self._inst is not None:
                try:
                    # Ön paneli kullanıcıya geri ver: durdurulmuş bir ekranda
                    # bırakmak, sonraki kişinin cihazın bozuk olduğunu
                    # sanmasına yol açıyor.
                    self._inst.write(":RUN")
                except Exception:
                    pass
                try:
                    self._inst.close()
                finally:
                    self._inst = None
            if self._rm is not None:
                try:
                    self._rm.close()
                finally:
                    self._rm = None

    # --- düşük seviye --------------------------------------------------
    def _write(self, cmd):
        with self._lock:
            self._inst.write(cmd)

    def _query(self, cmd):
        with self._lock:
            return self._inst.query(cmd).strip()

    def _query_binary(self, cmd):
        import numpy as np

        with self._lock:
            return self._inst.query_binary_values(
                cmd, datatype="H", is_big_endian=False, container=np.array)

    # --- sorgular ------------------------------------------------------
    def identify(self):
        return self._query("*IDN?")

    def sample_rate(self):
        """Cihazin o an kullandigi ornekleme hizi (Sa/s). None okunamazsa.

        Veri sayfasindaki azami degerden okunmuyor, cihazdan soruluyor:
        gercek ornekleme hizi zaman tabanina, acik kanal sayisina ve bellek
        derinligine gore degisir. Zaman izgarasinin belirsizlik butcesine
        katkisi bu sayidan hesaplandigi icin varsayilan bir deger kullanmak
        butceyi sessizce yanlis gosterirdi.
        """
        try:
            return float(self._query(":ACQuire:SRATe?"))
        except (ValueError, InstrumentError):
            return None

    def set_high_resolution(self, on=True, averaging=None):
        """Yuksek cozunurluk kipi: komsu ornekleri ortalayarak 8 bitin ustune cikar.

        Zayif yankilarin secilebilmesi icin onemli — 3. ve 4. yansima ilk
        yankinin onda birine kadar inebiliyor ve 8 bitlik izgarada birkac
        basamaga sikisiyor. Tetiklemeden tetiklemeye ortalama (AVERage)
        tekrarlanabilir bir sinyal gerektirir; HRESolution tek atimda da
        calisir, bu yuzden ikisi ayri secenek olarak duruyor.
        """
        if averaging:
            self._write(":ACQuire:TYPE AVERage")
            self._write(":ACQuire:COUNt %d" % int(averaging))
        elif on:
            self._write(":ACQuire:TYPE HRESolution")
        else:
            self._write(":ACQuire:TYPE NORMal")
        errs = self.check_errors()
        if errs:
            raise InstrumentError("Edinim kipi ayarlanamadi: " + "; ".join(errs))

    def check_errors(self):
        errors = []
        for _ in range(20):
            resp = self._query(":SYSTem:ERRor?")
            if not resp:
                break
            code = resp.split(",")[0].strip()
            if code in ("0", "+0"):
                break
            errors.append(resp)
        return errors

    # --- skaler ölçüm ---------------------------------------------------
    def configure(self, function_key, channel=None, averaging=None,
                  autoscale=False, **kw):
        if function_key not in self._MEASURE and function_key not in ("VAVG", "VRMS"):
            raise InstrumentError("Bu cihazda olmayan fonksiyon: %s" % function_key)

        if channel:
            self.channel = _normalize_channel(channel)
        self._write(":%s:DISPlay ON" % self.channel)

        if autoscale:
            # Sinyal büyüklüğü bilinmiyorsa dikey/yatay ayarı cihaza bıraktır.
            # Kalibrasyonda genelde istenmez: ölçek değişimi doğruluk
            # bütçesini değiştirir, bu yüzden varsayılan kapalı.
            self._write(":AUToscale")

        if averaging:
            self._write(":ACQuire:TYPE AVERage")
            self._write(":ACQuire:COUNt %d" % int(averaging))
        else:
            self._write(":ACQuire:TYPE NORMal")

        # Ölçümün tazelenmesi için cihaz taramaya devam etmeli.
        self._write(":RUN")
        self._write(":MEASure:SOURce %s" % self.channel)

        self._function = function_key
        errs = self.check_errors()
        if errs:
            raise InstrumentError("Cihaz hatası: " + "; ".join(errs))

    def _measure_query(self, function_key):
        if function_key == "VAVG":
            # <aralık>,<kaynak>: ekrandaki tüm veri üzerinden ortalama
            return ":MEASure:VAVerage? DISPlay,%s" % self.channel
        if function_key == "VRMS":
            # <aralık>,<tür>,<kaynak>: DC bileşen dahil gerçek RMS
            return ":MEASure:VRMS? DISPlay,DC,%s" % self.channel
        return ":MEASure:%s? %s" % (self._MEASURE[function_key], self.channel)

    def read_one(self):
        if self._function is None:
            raise InstrumentError("Önce configure() çağrılmalı")
        raw = self._query(self._measure_query(self._function))
        try:
            value = float(raw)
        except ValueError:
            raise InstrumentError("Sayıya çevrilemeyen yanıt: %r" % raw)

        if abs(value) >= INVALID_THRESHOLD:
            raise InstrumentError(
                "Cihaz ölçüm yapamadı (%s). Sinyal yok, tetikleme kararsız ya da "
                "ölçülen büyüklük ekranda görünmüyor olabilir. Dikey/yatay ölçeği "
                "kontrol edin." % raw)
        return value, raw

    # --- dalga yakalama --------------------------------------------------
    def displayed_channels(self, force=()):
        """Ekranda açık kanallar. force ile verilenler her zaman dahil edilir."""
        found = []
        for name, _label in self.CHANNELS:
            try:
                on = self._query(":%s:DISPlay?" % name) in ("1", "ON")
            except Exception:
                on = False
            if on or name in force:
                found.append(name)
        return found

    def arm(self):
        self._write(":SINGle")
        self._query("*OPC?")   # komutun işlendiğinden emin ol

    def wait_trigger(self, timeout_s=None, should_stop=None, poll_s=0.05):
        start = time.monotonic()
        while True:
            if should_stop is not None and should_stop():
                return False
            try:
                cond = int(self._query(":OPERegister:CONDition?"))
            except (ValueError, InstrumentError):
                return False
            if not cond & OPER_RUN_BIT:
                return True          # Run biti düştü → tetiklendi, edinim bitti
            if timeout_s is not None and (time.monotonic() - start) > timeout_s:
                return False
            time.sleep(poll_s)

    def read_waveform(self, source, points=None):
        import numpy as np

        self._write(":WAVeform:SOURce %s" % _normalize_channel(source))
        self._write(":WAVeform:POINts:MODE RAW")
        if points:
            self._write(":WAVeform:POINts %d" % int(points))
        self._write(":WAVeform:FORMat WORD")
        self._write(":WAVeform:BYTeorder LSBFirst")
        self._write(":WAVeform:UNSigned 1")

        pre = [float(v) for v in self._query(":WAVeform:PREamble?").split(",")]
        if len(pre) < 10:
            raise InstrumentError("Eksik dalga başlığı: %r" % pre)
        _fmt, _typ, _npts, _cnt, xinc, xorig, xref, yinc, yorig, yref = pre[:10]

        raw = self._query_binary(":WAVeform:DATA?")
        if raw.size == 0:
            raise InstrumentError("Cihaz boş dalga verisi döndürdü")

        volts = (raw.astype(np.float64) - yref) * yinc + yorig
        times = (np.arange(raw.size, dtype=np.float64) - xref) * xinc + xorig
        return times, volts

    def run(self):
        self._write(":RUN")

    def stop(self):
        self._write(":STOP")

    # --- ekran görüntüsü ---------------------------------------------------
    def screenshot(self, path, palette="COLor"):
        """Osiloskop ekranını olduğu gibi PNG olarak kaydeder.

        Neden cihazdan alınıyor da uygulamada çizilmiyor: rapora giren
        görüntünün *cihazın gördüğü* olması gerekiyor. Uygulamanın kendi
        çizimi bölme ayarlarını, tetikleme işaretini ve cihaz üzerindeki
        ölçüm okumalarını içermez; denetimde "ekranda ne vardı" sorusunun
        cevabı bu dosyadır.
        """
        with self._lock:
            old_term = self._inst.read_termination
            old_timeout = self._inst.timeout
            try:
                # İkili aktarımda satır sonu karakteri veriyi ortadan keser:
                # PNG içinde 0x0A baytı geçmesi olağan.
                self._inst.read_termination = None
                self._inst.timeout = max(self.timeout_ms, 20000)
                data = self._inst.query_binary_values(
                    ":DISPlay:DATA? PNG,%s" % palette,
                    datatype="B", container=bytearray, header_fmt="ieee")
            finally:
                self._inst.read_termination = old_term
                self._inst.timeout = old_timeout

        blob = bytes(data)
        if not blob.startswith(b"\x89PNG"):
            raise InstrumentError(
                "Cihazdan gelen veri PNG değil (%d bayt). Aygıt yazılımı "
                "ekran görüntüsünü desteklemiyor olabilir." % len(blob))
        with open(path, "wb") as fh:
            fh.write(blob)
        return path

    # --- ölçek ve tetikleme ------------------------------------------------
    def _apply_one(self, label, command, failures):
        """Tek komut gönderir ve hemen hata kuyruğuna bakar.

        Toplu gönderip sonunda tek kontrol yapmak, kullanıcıya
        `-222,"Data out of range"` deyip **hangi ayarın** reddedildiğini
        söylemiyordu. Ayrıca reddedilen bir komuttan sonraki komutlar
        tutarsız bir ölçekle uygulanmaya devam ediyordu.
        """
        self._write(command)
        for err in self.check_errors():
            failures.append((label, command, err))
            return False
        return True

    def autoscale(self, channel=None):
        """Cihazın kendi otomatik ölçeklemesi (ön paneldeki Auto Scale).

        Kalibrasyon ölçümünde kullanılmaz — ölçek doğruluk bütçesinin
        parçası. Ama sinyali *bulmak* için kullanılıyor: operatör bağlantıyı
        kurarken dalgayı ekranda görmek istiyor ve bunun için ön panele
        uzanmak zorunda kalmamalı. Sonrasında test modunun ölçekleri
        `apply_setup` ile yeniden yazılıyor.
        """
        ch = _normalize_channel(channel or self.channel)
        self._write(":%s:DISPlay ON" % ch)
        self._write(":AUToscale")
        self._query("*OPC?")
        errs = self.check_errors()
        if errs:
            raise InstrumentError("Otomatik ölçekleme hatası: " + "; ".join(errs))
        return self.read_setup(ch)

    def set_sweep(self, mode):
        """Tetikleme süpürme kipi: 'AUTO' | 'NORMal' | 'SINGle'."""
        self._write(":TRIGger:SWEep %s" % mode)

    def apply_setup(self, channel=None, volts_per_div=None, offset=None,
                    time_per_div=None, time_position=None, probe_ratio=None,
                    coupling=None, trigger_level=None, trigger_slope=None,
                    trigger_source=None, averaging=None, trigger_sweep="NORMal"):
        """Test modunun gerektirdiği cihaz ayarlarını uygular.

        Yalnızca verilen alanlar değiştirilir. Kalibrasyonda ölçek doğruluk
        bütçesinin parçası olduğu için `:AUToscale` kullanılmıyor — ölçek
        bilinerek seçiliyor ve seçilen değer kayda geçiyor.

        **Prob oranı = bölücü oranı.** Cihaza harici bölücüyü bildirmenin yolu
        prob zayıflatmasıdır. Bildirilmezse iki şey birden bozulur:

        * Dikey duyarlılık sınırı 1:1'de 500 µV…5 V/bölme'dir; 50 V/bölme
          istendiğinde cihaz `-222 Data out of range` döndürür. 1:1000
          bildirildiğinde sınır 0,5 V…5 kV/bölme olur ve istek geçerlileşir.
        * Cihazın ekranı, ölçümleri ve ekran görüntüsü bölünmüş gerilimi
          gösterir; rapordaki kV değerleriyle uyuşmaz.
        """
        ch = _normalize_channel(channel or self.channel)
        failures = []
        self._write(":%s:DISPlay ON" % ch)
        self.check_errors()

        # Prob oranı ÖNCE: dikey ölçek sınırı prob oranına bağlı, sonra
        # ayarlanırsa geçerli bir V/bölme değeri bile reddedilir.
        if probe_ratio:
            self._apply_one("Prob / bölücü oranı",
                            ":%s:PROBe %g" % (ch, float(probe_ratio)), failures)
        if coupling:
            self._apply_one("Kuplaj", ":%s:COUPling %s" % (ch, coupling), failures)
        if volts_per_div:
            self._apply_one("Dikey ölçek",
                            ":%s:SCALe %g" % (ch, float(volts_per_div)), failures)
        if offset is not None:
            self._apply_one("Dikey ofset",
                            ":%s:OFFSet %g" % (ch, float(offset)), failures)

        if time_per_div:
            self._apply_one("Zaman tabanı",
                            ":TIMebase:SCALe %g" % float(time_per_div), failures)
        if time_position is not None:
            self._apply_one("Zaman konumu",
                            ":TIMebase:POSition %g" % float(time_position),
                            failures)

        if trigger_source or trigger_level is not None or trigger_slope:
            source = _normalize_channel(trigger_source or ch)
            self._apply_one("Tetikleme modu", ":TRIGger:MODE EDGE", failures)
            self._apply_one("Tetikleme kaynağı",
                            ":TRIGger:EDGE:SOURce %s" % source, failures)
            if trigger_slope:
                self._apply_one("Tetikleme kenarı",
                                ":TRIGger:EDGE:SLOPe %s" % trigger_slope, failures)
            if trigger_level is not None:
                self._apply_one("Tetikleme eşiği",
                                ":TRIGger:EDGE:LEVel %g,%s"
                                % (float(trigger_level), source), failures)
            # Süpürme kipi çağırana bırakıldı, çünkü iki farklı iş var:
            #
            # * Yakalama sırasında NORMal gerekir — tetikleme gelmezse cihaz
            #   kendiliğinden taramamalı, yoksa boş bir ekranı "yakalanmış
            #   şok" sanardık.
            # * Ölçekleri ayarlarken AUTO gerekir. NORMal'de tetikleme
            #   gelmediği sürece ekran donuyor; operatör ayarı uyguladıktan
            #   sonra sinyali kaybediyor ve ön paneldeki Auto Scale'e
            #   basmadan geri getiremiyor.
            if trigger_sweep:
                self._apply_one("Tetikleme süpürmesi",
                                ":TRIGger:SWEep %s" % trigger_sweep, failures)

        if averaging:
            self._apply_one("Ortalama türü", ":ACQuire:TYPE AVERage", failures)
            self._apply_one("Ortalama sayısı",
                            ":ACQuire:COUNt %d" % int(averaging), failures)

        if failures:
            raise InstrumentError(_setup_error_text(failures, self.read_setup(ch)))
        return self.read_setup(ch)

    def read_setup(self, channel=None):
        """Cihazda o an geçerli olan ölçek/tetikleme ayarlarını okur."""
        ch = _normalize_channel(channel or self.channel)

        def num(cmd, default=None):
            try:
                return float(self._query(cmd))
            except (ValueError, InstrumentError):
                return default

        return {
            "channel": ch,
            "volts_per_div": num(":%s:SCALe?" % ch),
            "offset": num(":%s:OFFSet?" % ch),
            "probe_ratio": num(":%s:PROBe?" % ch),
            "time_per_div": num(":TIMebase:SCALe?"),
            "time_position": num(":TIMebase:POSition?"),
            "trigger_level": num(":TRIGger:EDGE:LEVel?"),
        }


def _setup_error_text(failures, current):
    """Reddedilen ayarları, cihazın kabul ettiği değerlerle birlikte anlatır.

    `-222,"Data out of range"` tek başına hiçbir şey söylemiyor; kullanıcının
    ekranda hangi alanı değiştireceğini bilmesi gerekiyor.
    """
    lines = ["Cihaz şu ayar(lar)ı kabul etmedi:"]
    for label, command, err in failures:
        lines.append("• %s  →  %s" % (label, err))

    if any(f[0] == "Dikey ölçek" for f in failures):
        probe = current.get("probe_ratio") or 1.0
        lines.append("")
        lines.append(
            "Dikey ölçek sınırı prob oranına bağlı: cihaz 1:1 probda "
            "500 µV – 5 V/bölme kabul eder. Şu an prob oranı 1:%g, yani "
            "izin verilen aralık %s – %s/bölme."
            % (probe, _volt_text(0.0005 * probe), _volt_text(5.0 * probe)))
        lines.append(
            "Harici bölücü kullanıyorsanız bölücü oranını girin — uygulama "
            "onu cihaza prob oranı olarak bildirir.")

    lines.append("")
    lines.append("Cihazda şu an geçerli olan: %s/bölme · %s/bölme · prob 1:%g"
                 % (_volt_text(current.get("volts_per_div")),
                    _time_text(current.get("time_per_div")),
                    current.get("probe_ratio") or 1.0))
    return "\n".join(lines)


def _volt_text(value):
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return "%.4g kV" % (value / 1000.0)
    if abs(value) < 0.001:
        return "%.4g µV" % (value * 1e6)
    if abs(value) < 1:
        return "%.4g mV" % (value * 1000.0)
    return "%.4g V" % value


def _time_text(value):
    if value is None:
        return "—"
    for factor, unit in ((1.0, "s"), (1e-3, "ms"), (1e-6, "µs"), (1e-9, "ns")):
        if abs(value) >= factor:
            return "%.4g %s" % (value / factor, unit)
    return "%.3g s" % value


def _normalize_channel(name):
    """'CH1', 'ch1', '1', 'CHANnel1' → 'CHANnel1'"""
    text = str(name).strip().upper()
    for suffix in ("1", "2"):
        if text in ("CHANNEL" + suffix, "CHAN" + suffix, "CH" + suffix, suffix):
            return "CHANnel" + suffix
    return str(name)
