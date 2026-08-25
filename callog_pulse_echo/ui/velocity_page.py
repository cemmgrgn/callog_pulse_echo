"""Ses hızı sayfası — darbe/yankı ile canlı izleme ve ölçüm.

Neden dalga yakalama sayfasından ayrı: orada tek bir olay beklenir ve her
tetikleme kendi kaydıdır. Burada cihaz sürekli okunur, her kare çözümlenir
ve ekranda anlık ses hızı akar; operatör "Durdur ve ölç" dediğinde o kare
donar ve **ölçüm** olarak kaydedilir. İki akış aynı sayfaya sığdırılmaya
çalışıldığında düğmelerin yarısı her zaman anlamsız kalıyordu.

Ölçüm zinciri: prob → JSR DPR300 (ECHO konumu, alıcı kazancı) → osiloskop
CH1. Tek prob, tek kanal.
"""

import csv
import os

import numpy as np

from callog_common import drivers
from callog_common import perms
from callog_common import testmodes
from callog_common.acquisition import WaveformWorker
from callog_common.qt import Qt
from callog_common.qt import QtCore
from callog_common.qt import QtGui
from callog_common.qt import QtWidgets
from callog_common.ui.util import PAGE_MARGIN
from callog_common.ui.util import PAGE_SPACING
from callog_common.ui.page_shell import PageShell
from callog_common.ui.page_shell import field_label
from callog_common.ui.waveform_discovery import _DiscoveryMixin
from .. import setupadvice
from .. import pulse_echo_modes
from .. import ultrasonic
from .. import ml_models
from .scope_view import ScopeView
from .velocity_results import _VelocityResultsMixin

#: Basamak bloğunun nominal kalınlıkları (mm). Sertifikalı gerçek değer
#: operatör tarafından girilir; bu liste yalnızca seçiciyi dolduruyor.
STEP_BLOCK_MM = (25.0, 20.0, 15.0, 12.5, 10.0, 7.5, 5.0, 2.5)

#: Kayda geçen prob listesi. Merkez frekans hesapta **kullanılmıyor** —
#: sinyalden ölçülüyor; bu alan yalnızca izlenebilirlik için.
PROBES = (
    ("Meccasonics M639 SMN2M5 (2,5 MHz)", "m639"),
    ("ICHF016 (yüksek frekans)", "ichf016"),
    ("Diğer / elle", "other"),
)

#: İlk sıradaki varsayılan: laboratuvardaki basamaklı blok 316 paslanmaz.
MATERIALS = (
    ("316 Paslanmaz çelik", "paslanmaz_316"),
    ("Karbon çelik", "celik"),
    ("Alüminyum", "aluminyum"),
    ("Pirinç", "pirinc"),
    ("Doku fantomu (1540 m/s)", "fantom"),
    ("Su (1480 m/s)", "su"),
    ("Diğer", None),
)

#: Ölçüme katılacak yankı sayısının varsayılanı. 25 mm'de dördüncü yansıma
#: çoğu kez ya gürültüye gömülüyor ya da kayıt penceresinin kenarında
#: kesiliyor; üçü güvenilir biçimde yakalanıyor ve üç yankı iki bağımsız
#: aralık veriyor, yani tutarlılık yine denetlenebiliyor.
DEFAULT_ECHOES = 3

#: Nokta sayısı hesaplanırken hedeflenen örnekleme hızı (Sa/s) ve sınırlar.
#:
#: Zaman tabanı kalınlığa göre daraldıkça sabit bir nokta sayısı çözünürlüğü
#: bozar ya da boşuna veri taşır. 5 mm'lik basamakta 50 µs'lik pencereye
#: 2000 nokta düşen bir kayıt, gidiş-dönüş başına 69 örnek verdi: paket
#: içindeki çevrimler görünmedi ve 16 MHz'lik taşıyıcı katlandı — ölçülen
#: frekans 1,16 MHz gibi makul ama tamamen yanlış bir sayı olarak çıktı.
#:
#: Üst sınır canlı izleme için: cihazdan kare başına yüz binlerce nokta
#: çekmek aktarımı saniyelere çıkarır ve ekran akmaz olur.
TARGET_SAMPLE_RATE = 5e9
MIN_POINTS = 10000
MAX_POINTS = 100000


class VelocityPage(_DiscoveryMixin, _VelocityResultsMixin, PageShell):

    def __init__(self, app_state, parent=None):
        PageShell.__init__(self, "velocity", parent)
        self.state = app_state
        self.driver = None
        self.worker = None
        self._scan_thread = None
        self._auto_scanned = False
        self._frame = None          # (times, values) — ekrandaki son kare
        self._result = None         # o karenin çözümlemesi
        self._history = []          # canlı kareler boyunca biriken hızlar
        self._series_id = None
        self._curves = {}
        self._markers = []
        self._advice = []

        self.set_title(
            "Ses hızı ve Kalınlık Analizi",
            "Darbe/yankı yöntemiyle blokta ses hızı ve ML kalınlık kestirimi — "
            "Klasik DSP veya eğitilmiş Makine Öğrenmesi (ML) modelleri ile anlık analiz.")

        self.add_settings_widget(self._instrument_box())
        self.add_settings_widget(self._block_box())
        self._build_actions()

        content = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(content)
        root.setContentsMargins(*PAGE_MARGIN)
        root.setSpacing(PAGE_SPACING)

        split = QtWidgets.QSplitter(Qt.Vertical)
        split.addWidget(self._plot_box())
        split.addWidget(self._tables_box())
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        # Osiloskop görünümü ızgara + yan panel + denetim çubuğu taşıyor;
        # daha kısa bir başlangıçta ızgara birkaç piksele sıkışıyor.
        split.setSizes([430, 230])
        root.addWidget(split, 1)
        # Canlı okuma sayıları (c, Δt, frekans…) osiloskobun kendi yan
        # panelinde duruyor — üstte ayrı bir kutu her sayfada osiloskobun
        # farklı yükseklikte başlamasına yol açıyordu (bkz. Veri toplama,
        # Dalga yakalama: onlarda böyle bir kutu yok).
        self._readouts = self.scope.set_external_readouts((
            ("pred_thick", "Model Kalınlık"),
            ("pct_error", "% Sapma (Bağıl)"),
            ("velocity", "Anlık ses hızı"),
            ("mean", "Ortalama (n kare)"),
            ("std", "Std sapma"),
            ("u", "Genişletilmiş U (k=2)"),
            ("dt", "Δt (1→2)"),
            ("freq", "Ölçülen frekans"),
            ("echoes", "Bulunan yankı"),
            ("coherence", "Faz tutarlılığı (DSP)"),
            ("model_info", "Aktif Model"),
        ))

        self.status_label = QtWidgets.QLabel("Hazır.")
        self.status_label.setProperty("hint", True)
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                        QtWidgets.QSizePolicy.Preferred)
        self.status_label.setMinimumWidth(200)
        root.addWidget(self.status_label)
        self.scope.exported.connect(self.status_label.setText)

        self.set_content(content)

        self._reload_instruments()
        self._reload_duts()
        self._on_step_changed()
        self._refresh_probe_hint()
        self._sync_points()
        self._update_buttons()

    # --- ayar kutuları ----------------------------------------------------
    def _instrument_box(self):
        box = QtWidgets.QGroupBox("Cihaz ve prob")
        grid = QtWidgets.QGridLayout(box)
        grid.setHorizontalSpacing(12)

        # Ayar paneli 300 px sabit genişlikte — birleşim kutuları en uzun
        # ögeye göre serbest genişlerse paneli yatay kaydırmaya zorlar.
        # `AdjustToMinimumContentsLengthWithIcon` + kısa bir minimum uzunluk
        # bunu bir üst sınırla kesiyor; tam metin açılır listede kalıyor.
        self.instrument_combo = QtWidgets.QComboBox()
        self.instrument_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.instrument_combo.setMinimumContentsLength(14)
        self.instrument_combo.currentIndexChanged.connect(
            self._on_instrument_changed)

        self.address_edit = QtWidgets.QLineEdit()
        self.address_edit.setPlaceholderText("USB0::…::INSTR")
        self.scan_addr_btn = QtWidgets.QPushButton("Otomatik bul")
        self.scan_addr_btn.clicked.connect(self._scan_address)
        addr_row = QtWidgets.QHBoxLayout()
        addr_row.addWidget(self.address_edit, 1)
        addr_row.addWidget(self.scan_addr_btn)

        self.dut_combo = QtWidgets.QComboBox()
        self.dut_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.dut_combo.setMinimumContentsLength(14)
        self.dut_combo.setToolTip(
            "Ölçümü bir cihaz/blok kaydına bağlar — cihaz defterinde "
            "birlikte görünürler.")

        self.probe_combo = QtWidgets.QComboBox()
        self.probe_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.probe_combo.setMinimumContentsLength(14)
        for label, key in PROBES:
            self.probe_combo.addItem(label, key)
        self.probe_combo.currentIndexChanged.connect(self._refresh_probe_hint)
        self.probe_combo.setToolTip(
            "Yalnızca kayda geçer. Merkez frekans hesapta kullanılmıyor — "
            "yakalanan sinyalin tayfından ölçülüyor, böylece etiketiyle "
            "oynanmış ya da yaşlanmış bir prob da doğru işlenir.")

        self.points_spin = QtWidgets.QSpinBox()
        self.points_spin.setRange(1000, 2000000)
        self.points_spin.setSingleStep(5000)
        self.points_spin.setValue(50000)
        self.points_spin.setToolTip(
            "Kanal başına nokta. Zamanlama çözünürlüğü buna bağlı: yankı "
            "aralığı 25 mm'de ~7,9 µs, 2,5 mm'de ~790 ns.")

        # Kısa etiket: tam açıklama ("HRES") araç ipucunda duruyor, dar
        # panelde checkbox'ın kendi satırına taşmasını engelliyor.
        self.hires_chk = QtWidgets.QCheckBox("Yüksek çözünürlük")
        self.hires_chk.setChecked(True)
        self.hires_chk.setToolTip(
            "8 bitte 4. yankı ilk yankının altıda birine iner ve iki-üç "
            "kuantalama basamağına sıkışır; tepe seçimi merdivene takılır.\n"
            "HRESolution komşu örnekleri ortalayarak bunun üstüne çıkar.")

        self.avg_spin = QtWidgets.QSpinBox()
        self.avg_spin.setRange(1, 256)
        self.avg_spin.setValue(1)
        self.avg_spin.setSpecialValueText("kapalı")
        self.avg_spin.setToolTip(
            "Donanım ortalaması. Zayıf yankıları gürültüden çıkarır ama "
            "canlı yenilemeyi yavaşlatır.")

        # Tek sütun (etiket + alan): panel dar (300 px) olduğu için iki
        # çifti yan yana koymak sıkışmaya yol açıyordu.
        grid.addWidget(field_label("Osiloskop"), 0, 0)
        grid.addWidget(self.instrument_combo, 0, 1)
        grid.addWidget(field_label("VISA adresi"), 1, 0)
        grid.addLayout(addr_row, 1, 1)
        grid.addWidget(field_label("Prob"), 2, 0)
        grid.addWidget(self.probe_combo, 2, 1)
        grid.addWidget(field_label("Nokta / kare"), 3, 0)
        grid.addWidget(self.points_spin, 3, 1)
        grid.addWidget(self.hires_chk, 4, 0, 1, 2)
        grid.addWidget(field_label("Ortalama"), 5, 0)
        grid.addWidget(self.avg_spin, 5, 1)
        grid.addWidget(field_label("Blok kaydı"), 6, 0)
        grid.addWidget(self.dut_combo, 6, 1)
        grid.setColumnStretch(1, 1)
        return box

    def _block_box(self):
        box = QtWidgets.QGroupBox("Blok ve çözümleme")
        grid = QtWidgets.QGridLayout(box)
        grid.setHorizontalSpacing(12)

        self.step_combo = QtWidgets.QComboBox()
        for mm in STEP_BLOCK_MM:
            self.step_combo.addItem("%.1f mm basamak" % mm, mm)
        self.step_combo.currentIndexChanged.connect(self._on_step_changed)

        # Kalınlık canlı değiştirilebilir: sayfanın varlık sebebi bu. Değer
        # sertifikalı gerçek kalınlık, seçicideki nominal değil.
        self.thickness_spin = QtWidgets.QDoubleSpinBox()
        self.thickness_spin.setRange(0.100, 200.0)
        self.thickness_spin.setDecimals(3)
        self.thickness_spin.setSuffix(" mm")
        self.thickness_spin.setSingleStep(0.01)
        self.thickness_spin.setValue(25.0)
        self.thickness_spin.setToolTip(
            "Basamağın **sertifikalı** kalınlığı. Hız doğrudan buna orantılı: "
            "%0,1 kalınlık hatası %0,1 hız hatası demek.")
        self.thickness_spin.valueChanged.connect(self._on_thickness_changed)

        self.u_thickness_spin = QtWidgets.QDoubleSpinBox()
        self.u_thickness_spin.setRange(0.0, 1000.0)
        self.u_thickness_spin.setDecimals(1)
        self.u_thickness_spin.setSuffix(" µm")
        self.u_thickness_spin.setValue(1.0)
        self.u_thickness_spin.setToolTip(
            "Kalınlık ölçümünün standart belirsizliği. İnce basamakta "
            "toplam belirsizliğe baskın katkıyı bu verir.")
        self.u_thickness_spin.valueChanged.connect(self._refresh_readout)

        self.material_combo = QtWidgets.QComboBox()
        self.material_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.material_combo.setMinimumContentsLength(14)
        for label, key in MATERIALS:
            self.material_combo.addItem(label, key)
        self.material_combo.currentIndexChanged.connect(self._refresh_readout)

        self.echoes_spin = QtWidgets.QSpinBox()
        self.echoes_spin.setRange(2, 8)
        self.echoes_spin.setValue(DEFAULT_ECHOES)
        self.echoes_spin.setToolTip(
            "Ortalamaya katılacak yankı sayısı. Zaman tabanı ve nokta "
            "sayısı buna göre yeniden hesaplanır.")
        self.echoes_spin.valueChanged.connect(self._on_echoes_changed)

        self.skip_first_chk = QtWidgets.QCheckBox("İlk paket uyarma darbesi")
        self.skip_first_chk.setChecked(True)
        self.skip_first_chk.setToolTip(
            "Ekrandaki en büyük paket genellikle ana darbedir, yankı değil. "
            "Şekli yankılardan farklı olduğu için ondan bir yankıya süre "
            "ölçmek sistematik hata taşır.\n"
            "İşaretliyse ilk paket ölçüme katılmaz.")
        self.skip_first_chk.toggled.connect(self._reanalyze_frame)

        # Fantom pin hedefleri için: yankılar t=0'da değil, hedefin
        # derinliğine karşılık gelen anda gelir. CIRS 040GSE'de düşey grup
        # 30-100 mm arasında, yani 39-130 µs — t=0'dan başlayan bir pencere
        # onları hiç görmez. Bloklarda 0 bırakılır.
        self.depth_spin = QtWidgets.QDoubleSpinBox()
        self.depth_spin.setRange(0.0, 500.0)
        self.depth_spin.setDecimals(1)
        self.depth_spin.setSuffix(" mm")
        self.depth_spin.setValue(0.0)
        self.depth_spin.setSpecialValueText("yüzeyden")
        self.depth_spin.setToolTip(
            "Kayıt penceresinin başlayacağı derinlik.\n\n"
            "Basamaklı blokta 0: yankılar ön yüzden itibaren gelir.\n"
            "Fantomda hedef grubu derinde olduğu için pencerenin oraya "
            "kaydırılması gerekir — ilk hedefin biraz öncesini girin.\n\n"
            "Yalnızca cihazın zaman konumunu değiştirir; hesaba girmez.")
        self.depth_spin.valueChanged.connect(self._on_echoes_changed)

        self.temp_spin = QtWidgets.QDoubleSpinBox()
        self.temp_spin.setRange(-50.0, 150.0)
        self.temp_spin.setDecimals(1)
        self.temp_spin.setSuffix(" °C")
        self.temp_spin.setValue(23.0)
        self.temp_spin.setToolTip(
            "Yalnızca kaydedilir, düzeltme uygulanmaz. Alüminyumda ses hızı "
            "yaklaşık −1 m/s/°C değişir; düzeltmeyi yapmak ölçümü değil "
            "yorumu değiştirir, o yüzden ham değer saklanıyor.")

        self.model_combo = QtWidgets.QComboBox()
        self.model_combo.setSizeAdjustPolicy(
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.model_combo.setMinimumContentsLength(14)
        for label, key in ml_models.AVAILABLE_MODELS:
            self.model_combo.addItem(label, key)
        self.model_combo.currentIndexChanged.connect(self._reanalyze_frame)
        self.model_combo.setToolTip(
            "Canlı izlemede kullanılacak analiz modelini belirler.\n\n"
            "• Tuned ML Modeli: Optimize edilmiş Gradient Boosting modeli (En yüksek doğruluk).\n"
            "• Akıllı Hibrit: DSP koheransı yüksekken ağırlıklı füzyon, gürültülü koşullarda ML.\n"
            "• Temel ML: 44 özellikli temel ağaç modeli.\n"
            "• Klasik DSP: Geleneksel paket zarfı ve tepe korelasyonu.")

        grid.addWidget(field_label("Basamak"), 0, 0)
        grid.addWidget(self.step_combo, 0, 1)
        grid.addWidget(field_label("Gerçek kalınlık"), 1, 0)
        grid.addWidget(self.thickness_spin, 1, 1)
        grid.addWidget(field_label("Malzeme"), 2, 0)
        grid.addWidget(self.material_combo, 2, 1)
        grid.addWidget(field_label("u(kalınlık)"), 3, 0)
        grid.addWidget(self.u_thickness_spin, 3, 1)
        grid.addWidget(field_label("Yankı sayısı"), 4, 0)
        grid.addWidget(self.echoes_spin, 4, 1)
        grid.addWidget(self.skip_first_chk, 5, 0, 1, 2)
        grid.addWidget(field_label("Sıcaklık"), 6, 0)
        grid.addWidget(self.temp_spin, 6, 1)
        grid.addWidget(field_label("Pencere başlangıcı"), 7, 0)
        grid.addWidget(self.depth_spin, 7, 1)
        grid.addWidget(field_label("Analiz Modeli"), 8, 0)
        grid.addWidget(self.model_combo, 8, 1)
        grid.setColumnStretch(1, 1)
        return box

    def _build_actions(self):
        """Bölge A'nın sabit eylem çubuğunu doldurur — hep aynı yerde, kaydırma gerektirmez."""
        self.autoscale_btn = self.add_action("Otomatik ölçekle", self._autoscale)
        self.autoscale_btn.setToolTip(
            "Cihazın ön panelindeki Auto Scale ile aynı: sinyali bulup "
            "ekrana oturtur ve tetiklemeyi kurar.\n\n"
            "İlk kurulumda bunu kullanın — tetikleme yanlışsa cihaz hiç "
            "tetiklenmez ve sayfa 'zaman aşımı' der. Ölçeği oturttuktan "
            "sonra zaman tabanı kalınlığa göre yeniden yazılır.")
        self.start_btn = self.add_action("Başlat", self._start, primary=True)
        self.stop_btn = self.add_action("Durdur ve ölç", self._stop_and_measure)
        self.stop_btn.setToolTip(
            "Canlı akışı durdurur, ekrandaki kareyi ölçüm olarak alır ve "
            "listeye ekler.")
        self.load_csv_btn = self.add_action("CSV'den yükle…", self._load_csv)
        self.load_csv_btn.setToolTip(
            "Cihazsız da çalışılabilir: daha önce kaydedilmiş ham CSV'yi "
            "(time_s, CH1_V sütunları) okur ve canlı bir kareymiş gibi "
            "çözümler.")
        self.add_action_separator()
        self.save_btn = self.add_action("Kaydet", self._save_measurement)
        self.save_btn.setToolTip(
            "Ham CSV + çözümleme sonucunu veritabanına yazar (SHA-256 ile).")
        self.export_btn = self.add_action("Dışa aktar…", self._export)
        self.export_btn.setToolTip(
            "Serideki tüm ölçümleri tek klasöre çıkarır: ham CSV'ler ve "
            "çözümleme özeti. İnceleme için gönderilecek paket budur.")
        self.reset_btn = self.add_action("Sıfırla", self._reset_stats)

    def _plot_box(self):
        box = QtWidgets.QGroupBox("Osiloskop ekranı")
        lay = QtWidgets.QVBoxLayout(box)
        self.scope = ScopeView()
        # Yankı ölçümünde ilk bakılanlar. Kullanıcı panelden istediğini
        # ekleyip çıkarabiliyor; bunlar yalnızca açılış durumu.
        self.scope.set_measurements(["vpp", "vmax", "freq"])
        lay.addWidget(self.scope)
        hint = QtWidgets.QLabel(
            "Renkli bölgeler bulunan yankı paketleri; kesikli çizgiler "
            "zamanlanan çevrimler (yeşil tepe, turuncu çukur). İşaretler "
            "beklediğiniz çevrimde değilse 'Çevrim indeksi'ni değiştirin.\n"
            "X imleçlerini iki yankının aynı çevrimine sürükleyip ΔX'e "
            "bakarak otomatik sonucu elle doğrulayabilirsiniz.")
        hint.setProperty("hint", True)
        hint.setWordWrap(True)
        lay.addWidget(hint)
        return box

    def apply_plot_theme(self):
        self.scope.apply_theme()

    # --- _DiscoveryMixin'in beklediği kancalar ---------------------------
    def current_mode(self):
        return testmodes.get(pulse_echo_modes.SOUND_VELOCITY)

    def _on_instrument_changed(self):
        """Karışıklık olmasın diye mixin'inki yerine geçiyor.

        Dalga yakalama sayfasının sürümü bir çıktı klasörü alanına yazıyor;
        bu sayfada öyle bir alan yok, kayıt klasörü seri anahtarından
        türetiliyor.
        """
        inst = self._current_instrument()
        if inst is None:
            self.address_edit.setEnabled(False)
            self.scan_addr_btn.setEnabled(False)
            return
        is_sim = drivers.is_simulated(inst["driver"])
        self.address_edit.setEnabled(not is_sim)
        self.scan_addr_btn.setEnabled(not is_sim)
        self.address_edit.setText("SIM" if is_sim else (inst["address"] or ""))

    def _nominal_energy(self):
        return None

    def _mode_chain(self):
        return {}

    def reload(self):
        self._reload_measurements()

    def showEvent(self, event):
        self._reload_duts()
        self.reload()
        QtWidgets.QWidget.showEvent(self, event)
        if not self._auto_scanned:
            self._auto_scanned = True
            QtCore.QTimer.singleShot(150, self._auto_scan_if_needed)

    def shutdown(self):
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(3000)
        self._close_driver()

    # --- canlı akış -------------------------------------------------------
    def _thickness_m(self):
        return self.thickness_spin.value() / 1000.0

    def _reference_velocity(self):
        key = self.material_combo.currentData()
        return ultrasonic.REFERENCE_VELOCITY.get(key)

    def _probe_hint(self):
        """Seçilen prob bu kalınlıkta çalışır mı — ölçüme girmeden önce.

        Yüksek frekanslı prob ince basamaklar için: soğurma kabaca frekansın
        karesiyle arttığı için 25 mm'lik çelikte 16 MHz'lik bir demet arka
        duvara gidip dönene kadar tükeniyor ve ekranda yankı görünmüyor.
        Bunu operatörün "cihaz bozuk mu" diye aramasına bırakmak yerine
        prob/kalınlık seçildiği anda söylemek gerekiyor.
        """
        probe = self.probe_combo.currentData()
        thickness_mm = self.thickness_spin.value()
        if probe == "ichf016" and thickness_mm > 12.0:
            return ("ICHF016 yüksek frekanslı: %.1f mm'de soğurma yankıları "
                    "tüketir, ekranda yansıma görünmeyebilir. Bu kalınlık "
                    "için M639 (2,5 MHz) takın; ICHF016'yı ince basamaklarda "
                    "(7,5 mm ve altı) kullanın." % thickness_mm)
        if probe == "m639" and thickness_mm < 10.0:
            return ("M639 2,5 MHz: %.1f mm'de paket süresi yankı aralığına "
                    "sığmaz, yansımalar ayrışmaz. Bu kalınlık için ICHF016 "
                    "takın." % thickness_mm)
        return None

    def _on_step_changed(self):
        nominal = self.step_combo.currentData()
        if nominal:
            self.thickness_spin.setValue(nominal)

    def _on_echoes_changed(self):
        # Yankı sayısı hem pencereyi hem çözünürlük ihtiyacını değiştiriyor.
        self._sync_points()
        self._apply_timebase()
        self._reanalyze_frame()

    def _refresh_probe_hint(self):
        hint = self._probe_hint()
        if hint:
            self.status_label.setText(hint)

    def _on_thickness_changed(self):
        self._refresh_probe_hint()
        self._sync_points()
        # Kalınlık değişince zaman tabanı da değişmeli: 25 mm'ye göre
        # ayarlanmış ekranda 2,5 mm'nin dört yankısı tek çizgiye çöker.
        self._apply_timebase()
        self._reanalyze_frame()

    def _recommended_points(self):
        """Bu kalınlıkta bir karenin taşıması gereken nokta sayısı."""
        per_div = self._timebase()
        if not per_div:
            return None
        span = per_div * 10.0
        wanted = int(round(span * TARGET_SAMPLE_RATE))
        return max(MIN_POINTS, min(MAX_POINTS, wanted))

    def _timebase(self):
        reference = self._reference_velocity() or 6320.0
        return pulse_echo_modes.timebase_for(self._thickness_m(), reference,
                                      echoes=self.echoes_spin.value())

    def _sync_points(self):
        """Nokta sayısını kalınlığa göre tazeler.

        Operatör elle değiştirebiliyor; ama kalınlık değiştiğinde eski değer
        neredeyse her zaman yanlış olduğu için üzerine yazılıyor. Yanlış
        yönde bir hata sessiz kalmıyor: çözümleme gidiş-dönüş başına düşen
        örneği denetleyip uyarıyor.
        """
        points = self._recommended_points()
        if points and points != self.points_spin.value():
            self.points_spin.setValue(points)

    def _apply_timebase(self, sweep=None):
        """Zaman tabanini ve tetikleme surpurmesini cihaza yazar.

        `sweep` verilmezse duruma gore secilir: yakalama surerken NORMal,
        dururken AUTO. Ikisi ayri isler ve karistirilmasi pahaliya mal
        oluyor -- bkz. `_sweep_for`.
        """
        if self.driver is None:
            return
        per_div = self._timebase()
        if not per_div:
            return
        # Tetikleme ekranın soluna alınıyor. Cihazda `:TIMebase:POSition`
        # ekranın **ortasındaki** zamandır; 0 bırakılırsa yankı dizisi t=0'da
        # başladığı için ekranın yarısı tetikleme öncesi boşluğa gider ve
        # pencereye yankıların ancak yarısı sığar. Ölçülen bir kayıtta tam
        # bu oldu: 30 µs'lik pencerede yalnızca tek yankı vardı ve ölçüm
        # yapılamadı. %10 pay tetikleme öncesi için bırakılıyor.
        span = per_div * 10.0
        # Pencerenin ortasındaki zaman. Başlangıç derinliği verilmişse
        # pencere oraya kaydırılıyor; yoksa tetikleme solda kalıyor.
        reference = self._reference_velocity() or 6320.0
        start = 2.0 * (self.depth_spin.value() / 1000.0) / reference
        try:
            self.driver.apply_setup(channel="CHANnel1", time_per_div=per_div,
                                    time_position=start + 0.4 * span,
                                    trigger_sweep=sweep or self._sweep_for())
        except Exception as exc:
            self.status_label.setText("Zaman tabanı yazılamadı: %s" % exc)

    def _sweep_for(self):
        """Yakalama suruyorsa NORMal, duruyorsa AUTO.

        AUTO supurmede cihaz, tetikleme gelmediginde kendiliginden tarar --
        ekranda bir sey gorunsun diye. Olcekleri ayarlarken tam olarak bu
        istenir: NORMal'de ekran donar ve operator sinyali kaybeder.

        Ama yakalama sirasinda AUTO felakettir: kaydedilen kare artik
        darbeyle **eszamanli degildir**, PRF'e gore rastgele bir fazda
        baslar ve yankilar arasindaki bos bolgeye denk gelebilir. Ekranda
        "ara sira goruntu kayiyor, bos yeri yakaliyor" diye gorunen sey
        budur. NORMal'de cihaz yalnizca gercek tetiklemede edinim yapar;
        tetikleme gelmiyorsa kare de gelmez ve durum cubugu zaman asimi
        yazar -- yanlis veri yerine durust bir sessizlik.
        """
        running = self.worker is not None and self.worker.isRunning()
        return "NORMal" if running else "AUTO"

    def _scan_address(self):
        """Adres taramasi -- ama once acik oturumu birakarak.

        Tarama, bulunan her VISA kaynagini tek tek acip ``*IDN?`` soruyor.
        Sayfanin kendi baglantisi acikken bu, cihazi kendi kendine ikinci
        kez actirmaya calismak demek: USBTMC ayni anda tek oturuma izin
        verdigi icin istek yanitsiz kaliyor ve
        ``VI_ERROR_TMO`` ile dusuyor. Kullaniciya gorunen sey "cihaz
        bulunamadi" oluyor, oysa cihaz bagli ve calisiyor.
        """
        if self.worker is not None and self.worker.isRunning():
            self.status_label.setText(
                "Canlı izleme sürerken adres taranamaz — önce durdurun.")
            return
        self._close_driver()
        _DiscoveryMixin._scan_address(self)

    def _autoscale(self):
        """Cihazın kendi Auto Scale'i — sinyali bulmak için.

        Kalibrasyon ölçümünün kendisinde kullanılmaz (ölçek doğruluk
        bütçesinin parçası), ama ilk kurulumda tetiklemeyi ve dikey ölçeği
        oturtmanın en hızlı yolu bu. Ardından zaman tabanı kalınlığa göre
        yeniden yazılıyor: Auto Scale ekranı taşıyıcıya göre ayarlar ve
        yankı dizisinin tamamını görmez.
        """
        inst = self._current_instrument()
        if inst is None:
            return
        drv, owned = self._open_driver(inst)
        if drv is None:
            return
        try:
            drv.autoscale("CHANnel1")
            self.driver = drv
            # Olcek ayarlarken AUTO: sinyal ekranda kalsin.
            self._apply_timebase(sweep="AUTO")
            self.status_label.setText(
                "Otomatik ölçekleme yapıldı, zaman tabanı kalınlığa göre "
                "ayarlandı. Canlı izlemeyi başlatabilirsiniz.")
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Ölçeklenemedi", str(exc))
            if owned:
                self._close_driver()

    def _start(self):
        if not self.state.can(perms.VELOCITY_MEASURE):
            QtWidgets.QMessageBox.warning(
                self, "Yetki yok", perms.denial_message(perms.VELOCITY_MEASURE))
            return
        inst = self._current_instrument()
        if inst is None:
            return
        drv, _owned = self._open_driver(inst)
        if drv is None:
            return
        self.driver = drv
        self._sync_sim_block(drv)

        try:
            if hasattr(drv, "set_high_resolution"):
                drv.set_high_resolution(
                    self.hires_chk.isChecked(),
                    averaging=(self.avg_spin.value()
                               if self.avg_spin.value() > 1 else None))
        except Exception as exc:
            self.status_label.setText("Edinim kipi ayarlanamadı: %s" % exc)
        # Yakalamaya gecmeden NORMal supurmeye aliniyor: bundan sonraki her
        # kare gercek bir tetiklemeye kilitli olmali.
        self._apply_timebase(sweep="NORMal")

        self.worker = WaveformWorker(
            drv, ["CHANnel1"], points=self.points_spin.value(),
            max_captures=0, timeout_s=5, screenshot=False)
        self.worker.captured.connect(self._on_frame)
        self.worker.error.connect(self._on_worker_error)
        self.worker.status.connect(self.status_label.setText)
        self.worker.finished_run.connect(self._on_worker_done)
        self.worker.start()
        self.status_label.setText("Canlı izleme başladı.")
        self._update_buttons()

    def _sync_sim_block(self, drv):
        """Simülasyon sürücüsüne güncel bloğu bildirir.

        Operatör kalınlığı canlı değiştirdiğinde simülasyonun eski kalınlıkla
        yankı üretmeyi sürdürmesi, sayfayı denemeyi anlamsız kılardı. Gerçek
        sürücülerde bu alanlar yok — sessizce atlanıyor.
        """
        if drv is None or not hasattr(drv, "thickness_m"):
            return
        drv.thickness_m = self._thickness_m()
        drv.n_echoes = max(drv.n_echoes, self.echoes_spin.value())
        if hasattr(drv, "high_resolution"):
            drv.high_resolution = self.hires_chk.isChecked()

    def _load_csv(self):
        """Daha önce kaydedilmiş ham CSV'yi ekrandaki kareymiş gibi yükler.

        `waveform.save`'in yazdığı formatla aynı: ilk sütun `time_s`, ardından
        kanal sütunları (`CH1_V`...). Cihaz bağlı olmadan, geçmiş bir kaydı
        yeniden çözümlemek veya başka bir kaynaktan gelen veriyi denemek için.
        """
        if self.worker is not None and self.worker.isRunning():
            self.status_label.setText(
                "Canlı izleme sürerken CSV yüklenemez — önce durdurun.")
            return
        path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self, "CSV'den yükle", "", "CSV dosyaları (*.csv);;Tüm dosyalar (*.*)")
        if not path:
            return
        try:
            times, values = _read_waveform_csv(path)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "CSV okunamadı", str(exc))
            return
        if len(times) == 0:
            QtWidgets.QMessageBox.warning(
                self, "CSV boş", "Dosyada okunabilir veri satırı bulunamadı.")
            return
        self._frame = (times, values)
        self._analyze_current(record=True)
        self.status_label.setText(
            "CSV yüklendi (%d nokta): %s" % (len(times), os.path.basename(path)))
        self._update_buttons()

    def _on_frame(self, _count, times, columns, _shot):
        values = columns.get("CH1_V")
        if values is None or len(values) == 0:
            return
        self._frame = (times, values)
        self._sync_sim_block(self.driver)
        self._analyze_current(record=True)

    def _reanalyze_frame(self):
        """Ayar değişince ekrandaki kareyi yeniden çözer — cihaza gitmeden.

        İstatistiğe yazmaz: aynı kare, farklı ayarla ikinci kez sayılırsa
        ortalama ve saçılım şişer.
        """
        if self._frame is not None:
            self._analyze_current(record=False, keep_view=False)

    def _analyze_current(self, record, keep_view=True):
        times, values = self._frame
        model_key = self.model_combo.currentData() if hasattr(self, "model_combo") else "tuned_gbr"
        result = ml_models.analyze_with_model(
            times, values,
            thickness_m=self._thickness_m(),
            reference_velocity=self._reference_velocity(),
            model_key=model_key,
            max_echoes=self.echoes_spin.value(),
            skip_first_packet=self.skip_first_chk.isChecked()
        )
        self._result = result
        # Kurulum önerisi her karede yeniden çıkarılıyor: operatör düğmeyi
        # çevirdiğinde etkisini bir sonraki karede görmeli.
        self._advice = setupadvice.advise(
            times, values, self._thickness_m(), self._reference_velocity(),
            result)
        if record and result.get("found"):
            self._history.append(result["velocity"])
        self._draw_frame(keep_view=keep_view)
        self._refresh_readout()
        self._fill_tables()

    def _on_worker_error(self, text):
        self.status_label.setText(text)

    def _on_worker_done(self):
        self._update_buttons()

    def _stop_and_measure(self):
        if self.worker is not None:
            self.worker.stop()
            self.worker.wait(3000)
            self.worker = None
        self._update_buttons()
        if self._result is None or not self._result.get("found"):
            self.status_label.setText(
                "Durduruldu, ama ekrandaki karede ölçülebilir yankı yok.")
            return
        pred_th = self._result.get("predicted_thickness_mm")
        if pred_th is not None:
            self.status_label.setText(
                "Ölçüldü: %.1f m/s (Model Kalınlık: %.2f mm). 'Ölçümü kaydet' ile veritabanına yazılır."
                % (self._result["velocity"], pred_th))
        else:
            self.status_label.setText(
                "Ölçüldü: %.1f m/s. 'Ölçümü kaydet' ile veritabanına yazılır."
                % self._result["velocity"])

    def _reset_stats(self):
        self._history = []
        self._series_id = None
        self._refresh_readout()

    def _close_driver(self):
        if self.driver is not None:
            try:
                self.driver.close()
            except Exception:
                pass
            self.driver = None

    def _update_buttons(self):
        running = self.worker is not None and self.worker.isRunning()
        may = self.state.can(perms.VELOCITY_MEASURE)
        self.start_btn.setEnabled(may and not running)
        self.stop_btn.setEnabled(running)
        has_result = bool(self._result and self._result.get("found"))
        self.save_btn.setEnabled(may and has_result and not running)
        self.export_btn.setEnabled(bool(self._series_id))
        self.set_status("Canlı akış" if running else "Hazır",
                        "ok" if running else "idle")

    # --- çizim -------------------------------------------------------------
    def _draw_frame(self, keep_view=True):
        if self._frame is None:
            return
        times, values = self._frame
        self.scope.set_data(times, values, keep_view=keep_view)

        self.scope.clear_overlays()
        self._markers = []
        if not (self._result and self._result.get("found")):
            return

        for packet in self._result.get("packets", []):
            self.scope.add_region(packet["start_time"], packet["end_time"],
                                  QtGui.QColor(70, 120, 190, 55))
            # Gecikme artık paket içindeki tek bir çevrimden değil, dalganın
            # tamamının korelasyonundan çıkıyor; işaret de bir çevrimi değil
            # yankının konumunu gösteriyor.
            self.scope.add_marker(packet["centroid_time"], "#4ec97f")
            self._markers.append(packet["index"])

    # --- okuma paneli ------------------------------------------------------
    def _refresh_readout(self):
        result = self._result
        show = self._readouts
        if not (result and result.get("found")):
            for key in show:
                show[key].setText("—")
            if result:
                lines = [result.get("reason", "")]
                lines += [_advice_text(a) for a in self._advice
                          if a[0] != setupadvice.INFO]
                self.status_label.setText("  ·  ".join(x for x in lines if x))
            self._update_buttons()
            return

        budget = ultrasonic.uncertainty(
            result, u_thickness_m=self.u_thickness_spin.value() / 1e6,
            type_a_velocity=self._history_u_a())

        pred_th = result.get("predicted_thickness_mm")
        if "pred_thick" in show:
            show["pred_thick"].setText("%.2f mm" % pred_th if pred_th is not None else "—")
        if "pct_error" in show:
            pct = result.get("pct_error")
            show["pct_error"].setText("%% %.2f" % pct if pct is not None else "—")
        if "model_info" in show:
            m_name = result.get("ml_model_name", "Tuned ML")
            show["model_info"].setText(m_name[:16] + "…" if len(m_name) > 16 else m_name)

        if "velocity" in show:
            show["velocity"].setText("%.1f m/s" % result["velocity"])
        if "dt" in show:
            show["dt"].setText(_si(_first_dt(result), "s"))
        if "freq" in show:
            show["freq"].setText(
                _si(1.0 / result["carrier_period_s"], "Hz")
                if result.get("carrier_period_s") else "—")
        if "echoes" in show:
            show["echoes"].setText(str(len(result.get("packets", []))))
        if "coherence" in show:
            show["coherence"].setText("%.3f" % (result.get("dsp_coherence") or result.get("coherence") or 0))

        n = len(self._history)
        if "mean" in show:
            if n:
                mean = sum(self._history) / n
                show["mean"].setText("%.1f m/s  (n=%d)" % (mean, n))
            else:
                show["mean"].setText("—")
        if "std" in show:
            std = self._history_std()
            show["std"].setText("%.2f m/s" % std if std is not None else "—")
        if "u" in show:
            show["u"].setText("± %.1f m/s" % budget["expanded"] if budget else "—")

        reference = self._reference_velocity()
        parts = []
        if pred_th is not None:
            parts.append("Model: %s → Kestirim: %.2f mm (sapma: %%%.2f)"
                         % (result.get("ml_model_name", "ML"), pred_th, result.get("pct_error", 0.0)))
        if reference:
            parts.append("referans %.0f m/s'den sapma %%%.2f"
                         % (reference,
                            100 * (result["velocity"] - reference) / reference))
        if result.get("is_anomaly"):
            parts.insert(0, "⚠️ [UYARI: Sinyalde Anomali / Bozulma Algılandı]")
        parts.extend(result.get("warnings", []))
        # Kurulum önerileri en öne: uyarı sonucu tarif eder, öneri ne
        # yapılacağını söyler ve operatörün ihtiyacı olan ikincisidir.
        parts = [_advice_text(a) for a in self._advice
                 if a[0] != setupadvice.INFO] + parts
        self.status_label.setText("  ·  ".join(parts) if parts else "Ölçüm iyi.")
        self._update_buttons()

    def _history_std(self):
        n = len(self._history)
        if n < 2:
            return None
        mean = sum(self._history) / n
        return (sum((v - mean) ** 2 for v in self._history) / (n - 1)) ** 0.5

    def _history_u_a(self):
        """Tekrarlanan karelerden gelen A tipi belirsizlik.

        Kare **içi** saçılım bunun yerine kullanılamaz: oradaki kestirimler
        aynı zaman damgalarını paylaşıyor, bağımsız tekrar değiller.
        """
        std = self._history_std()
        if std is None:
            return None
        return std / (len(self._history) ** 0.5)


def _read_waveform_csv(path):
    """`time_s, CH1_V[, ...]` başlıklı CSV'yi (times, values) çiftine çevirir.

    Yalnızca ilk kanal sütunu (`CH1_V` varsa o, yoksa ikinci sütun) okunur —
    bu sayfa tek kanal ile çalışıyor.
    """
    times, values = [], []
    with open(path, "r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if not header or len(header) < 2:
            raise ValueError("Beklenen sütunlar yok (time_s, CH1_V).")
        value_idx = header.index("CH1_V") if "CH1_V" in header else 1
        for row in reader:
            if len(row) <= value_idx:
                continue
            try:
                t = float(row[0])
                v = float(row[value_idx])
            except ValueError:
                continue
            times.append(t)
            values.append(v)
    return np.array(times, dtype=np.float64), np.array(values, dtype=np.float64)


def _advice_text(advice):
    """(önem, sorun, yapılacak) -> tek satır."""
    severity, issue, action = advice
    return "[%s] %s → %s" % (severity.upper(), issue, action)


def _first_dt(result):
    for e in result.get("estimates", ()):
        if e["round_trips"] == 1:
            return e["dt"]
    return None


def _si(value, unit):
    if value is None:
        return "—"
    for factor, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""),
                           (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs(value) >= factor:
            return "%.4g %s%s" % (value / factor, prefix, unit)
    return "%.3g %s" % (value, unit)
