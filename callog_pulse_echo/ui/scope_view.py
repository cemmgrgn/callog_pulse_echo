"""Osiloskop ekranı görünümü — bölme ızgarası, imleçler ve ölçüm paneli.

Cihazın ekranındaki işi uygulamada yapabilmek için. Yalnızca görsel bir
tercih değil: operatör osiloskopta zaten bölme cinsinden düşünüyor, ve
imleçle elle ölçülen Δt, otomatik çözümlemenin sonucunu **bağımsız olarak**
doğrulamanın tek yolu. Çözümleme yanlış çevrimi seçtiğinde bunu ancak
imleçle bakınca fark edersiniz.

Ölçüm matematiği burada değil, `callog_pulse_echo/measure.py` içinde: aynı "Vpp"
tanımının hem ekranda hem raporda hem testte geçerli olması için.

Görünüm cihazdan bağımsız: dikey/yatay ölçek yalnızca **ekranı** değiştirir,
cihaza yazılmaz. Cihaz ayarlarını değiştirmek ölçüm koşulunu değiştirmek
demek ve kayda geçmesi gerekir; ekranı yakınlaştırmak ise sonuca dokunmaz.
"""

from callog_common import theme
from callog_common.qt import Qt
from callog_common.qt import QtGui
from callog_common.qt import QtWidgets
from callog_common.qt import Signal
from .. import measure

#: Cihaz ekranı gibi: yatay 10, dikey 8 bölme.
H_DIVISIONS = 10
V_DIVISIONS = 8

#: 1-2-5 dizisi — osiloskop ölçek düğmesinin durakları. Ara değerlere izin
#: vermek ekranı okunaksız kılıyor: "3,7 V/bölme" hiçbir şeyi kolaylaştırmaz.
_STEPS = (1.0, 2.0, 5.0)

CURSOR_OFF = "off"
CURSOR_X = "x"
CURSOR_Y = "y"
CURSOR_XY = "xy"

#: Çok kanallı sayfalarda (Dalga yakalama) kanal başına iz rengi.
_SERIES_COLORS = ("#3fa9f5", "#f2c14b", "#4ec97f", "#e0705f")


def export_plot_widget(plot_widget, path):
    """pyqtgraph grafiğini PNG ya da SVG olarak diske yazar.

    `ScopeView.export_plot_image()` bunu kendi `self.plot`u üzerinde
    çağırıyor; bağımsız tutulmasının nedeni `compare_dialog.py`nin de
    (ScopeView kullanmayan, çıplak bir `pg.PlotWidget`) aynı dışa
    aktarmayı kullanması.

    PNG için önce pyqtgraph'ın dışa aktarıcısı deneniyor: ekran
    çözünürlüğünden bağımsız, rapora konacak kadar büyük bir görüntü
    üretiyor. Dışa aktarıcı bazı Qt sürümlerinde düşüyor; o durumda
    pencerenin kendi kopyası alınıyor — düşük çözünürlüklü ama hiç yoktan
    iyi ve kullanıcıya "kaydedilemedi" demekten iyi.
    """
    if path.lower().endswith(".svg"):
        from pyqtgraph.exporters import SVGExporter
        SVGExporter(plot_widget.getPlotItem()).export(path)
        return path
    try:
        from pyqtgraph.exporters import ImageExporter
        exporter = ImageExporter(plot_widget.getPlotItem())
        exporter.parameters()["width"] = 1600
        exporter.export(path)
    except Exception:
        if not plot_widget.grab().save(path):
            raise
    return path


def nice_step(value, direction=0):
    """Değeri 1-2-5 dizisine oturtur; direction=+1 bir üst, -1 bir alt durak."""
    if value <= 0:
        return 1.0
    import math

    decade = math.floor(math.log10(value))
    base = 10.0 ** decade
    ladder = [s * base for s in _STEPS] + [10.0 * base]
    if direction == 0:
        return min(ladder, key=lambda s: abs(math.log10(s / value)))
    current = min(ladder, key=lambda s: abs(math.log10(s / value)))
    full = ([s * base / 10.0 for s in _STEPS] + ladder
            + [s * base * 10.0 for s in _STEPS])
    full = sorted(set(full))
    index = full.index(min(full, key=lambda s: abs(s - current)))
    index = max(0, min(len(full) - 1, index + direction))
    return full[index]


class ScopeView(QtWidgets.QWidget):
    """Bölme ızgaralı çizim alanı + imleçler + ölçüm paneli."""

    #: Bir dışa aktarma başarıyla bittiğinde kısa bir durum metni yayar.
    #: Sayfalar isterse kendi durum etiketine bağlar
    #: (`self.scope.exported.connect(self.status_label.setText)`); hiçbiri
    #: bağlamazsa da düğmeler kendi hata iletişim kutularını gösterdiği
    #: için sorunsuz çalışır.
    exported = Signal(str)

    def __init__(self, parent=None):
        QtWidgets.QWidget.__init__(self, parent)
        import pyqtgraph as pg

        self._pg = pg
        self._times = []
        self._values = []
        self._overlays = []
        self._guides = []
        self._auto_fit = True
        self._syncing_slider = False
        # Çoklu iz desteği (Dalga yakalama'da CH1/CH2 gibi). Tek izli
        # sayfalar (Ses hızı, Veri toplama) yalnızca "CH1"i kullanır ve bunun
        # farkına varmaz — `set_data` zaten `set_series`e yönlendiriyor.
        self._curves = {}
        self._series_order = []
        self._legend = None
        self._legend_registered = set()

        self.volts_per_div = 0.5
        self.time_per_div = 1e-6
        self.v_offset = 0.0
        self.t_position = 0.0

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        self.plot = pg.PlotWidget()
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        # Fare tekerleği = yakınlaştırma, sol tık sürükle = kaydırma —
        # gerçek osiloskopta olmayan ama beklenen bir kolaylık. Bölme
        # etiketleri (`vdiv_label`/`tdiv_label`) ve imleç sınırları fareyle
        # değişen görünümle senkron kalsın diye `sigRangeChanged` dinleniyor.
        self.plot.setMouseEnabled(x=True, y=True)
        self.plot.setMinimumHeight(220)
        vb = self.plot.getViewBox()
        vb.setDefaultPadding(0.0)
        self._syncing_range = False
        vb.sigRangeChanged.connect(self._on_view_range_changed)

        self.curve = self._get_curve("CH1")     # geriye dönük uyumluluk
        self._build_cursors()

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(8)
        body.addWidget(self.plot, 1)
        body.addWidget(self._side_panel())
        root.addLayout(body, 1)
        root.addWidget(self._control_bar())
        root.addWidget(self._position_bar())
        root.addWidget(self._export_bar())
        root.addWidget(self._cursor_readout())

        self.apply_theme()
        self._rescale()

    # --- kurulum ----------------------------------------------------------
    def _build_cursors(self):
        pg = self._pg
        self.cursor_mode = CURSOR_OFF
        self._cursors = {}
        for key, angle in (("x1", 90), ("x2", 90), ("y1", 0), ("y2", 0)):
            line = pg.InfiniteLine(angle=angle, movable=True,
                                   pen=pg.mkPen("#f5a623", width=1,
                                                style=Qt.DashLine))
            line.setZValue(50)
            line.sigPositionChanged.connect(self._on_cursor_moved)
            line.setVisible(False)
            self.plot.addItem(line)
            self._cursors[key] = line

    def _control_bar(self):
        bar = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self.vdiv_label = QtWidgets.QLabel()
        self.tdiv_label = QtWidgets.QLabel()
        for label in (self.vdiv_label, self.tdiv_label):
            label.setMinimumWidth(96)
            label.setAlignment(Qt.AlignCenter)
            label.setProperty("badge", "ok")

        row.addWidget(QtWidgets.QLabel("Dikey"))
        row.addWidget(self._step_button("−", lambda: self._zoom_v(-1)))
        row.addWidget(self.vdiv_label)
        row.addWidget(self._step_button("+", lambda: self._zoom_v(+1)))

        row.addSpacing(10)
        row.addWidget(QtWidgets.QLabel("Yatay"))
        row.addWidget(self._step_button("−", lambda: self._zoom_t(-1)))
        row.addWidget(self.tdiv_label)
        row.addWidget(self._step_button("+", lambda: self._zoom_t(+1)))

        row.addSpacing(10)
        self.fit_btn = QtWidgets.QPushButton("Ekrana sığdır")
        self.fit_btn.setToolTip(
            "Ölçeği kayda göre ayarlar. Yalnızca görüntüyü değiştirir; "
            "cihazdaki ayarlara dokunmaz.")
        self.fit_btn.clicked.connect(self.fit_to_data)
        row.addWidget(self.fit_btn)

        self.cursor_combo = QtWidgets.QComboBox()
        self.cursor_combo.addItem("İmleç yok", CURSOR_OFF)
        self.cursor_combo.addItem("Zaman (X)", CURSOR_X)
        self.cursor_combo.addItem("Gerilim (Y)", CURSOR_Y)
        self.cursor_combo.addItem("X ve Y", CURSOR_XY)
        self.cursor_combo.currentIndexChanged.connect(self._on_cursor_mode)
        row.addWidget(self.cursor_combo)

        self.window_chk = QtWidgets.QCheckBox("Ölçümler imleçler arasında")
        self.window_chk.setToolTip(
            "İşaretliyken otomatik ölçümler yalnızca X imleçleri arasındaki "
            "bölgeden hesaplanır — tek bir yankı paketini ayrı ölçmek için.")
        self.window_chk.toggled.connect(self.refresh_measurements)
        row.addWidget(self.window_chk)

        row.addStretch(1)
        return bar

    def _position_bar(self):
        """Zamanda kaydırma — cihazın yatay konum düğmesinin karşılığı.

        Yankı dizisi ekrana sığdığında bile tek bir paketi yakınlaştırıp
        üzerinde gezinmek gerekiyor: hangi çevrimin seçildiğini ancak
        yakınlaşınca görebilirsiniz. Yakınlaştırma tek başına işe yaramaz,
        yanına kaydırma gerekir.
        """
        bar = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        row.addWidget(QtWidgets.QLabel("Konum"))
        row.addWidget(self._step_button("⏮", lambda: self._pan(-5)))
        row.addWidget(self._step_button("◀", lambda: self._pan(-1)))

        self.position_slider = QtWidgets.QSlider(Qt.Horizontal)
        self.position_slider.setRange(0, 1000)
        self.position_slider.setValue(500)
        self.position_slider.setToolTip(
            "Ekranın ortasındaki zaman. Kaydırma yalnızca görüntüyü "
            "değiştirir; cihazdaki ayarlara ve ölçüme dokunmaz.")
        self.position_slider.valueChanged.connect(self._on_slider)
        row.addWidget(self.position_slider, 1)

        row.addWidget(self._step_button("▶", lambda: self._pan(+1)))
        row.addWidget(self._step_button("⏭", lambda: self._pan(+5)))

        self.centre_btn = QtWidgets.QPushButton("Tetiklemeye dön")
        self.centre_btn.setToolTip("Ekranın ortasını t = 0'a getirir.")
        self.centre_btn.clicked.connect(self._centre_on_trigger)
        row.addWidget(self.centre_btn)

        self.position_label = QtWidgets.QLabel("—")
        self.position_label.setMinimumWidth(84)
        self.position_label.setAlignment(Qt.AlignCenter)
        row.addWidget(self.position_label)
        return bar

    def _export_bar(self):
        """Ekranda duran kaydı diske yazan düğmeler — üç osiloskop sayfası
        da (Ses hızı, Veri toplama, Dalga yakalama) aynı üç düğmeyi görsün
        diye burada, sayfa başına ayrı ayrı yazılmak yerine.
        """
        bar = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self.save_plot_btn = QtWidgets.QPushButton("Grafiği kaydet")
        self.save_plot_btn.setToolTip(
            "Eğriyi PNG ya da SVG olarak diske yazar — rapora ya da "
            "e-postaya koymak için. Yüksek çözünürlüklü, yalnızca çizim.")
        self.save_plot_btn.clicked.connect(self._save_plot_image)
        row.addWidget(self.save_plot_btn)

        self.screenshot_btn = QtWidgets.QPushButton("Ekran görüntüsü al")
        self.screenshot_btn.setToolTip(
            "Osiloskop ekranının tamamını — ızgara, imleçler, ölçüm "
            "paneli dahil — göründüğü gibi PNG olarak kaydeder.")
        self.screenshot_btn.clicked.connect(self._save_screenshot)
        row.addWidget(self.screenshot_btn)

        self.save_csv_btn = QtWidgets.QPushButton("CSV kaydet")
        self.save_csv_btn.setToolTip(
            "Ekranda duran kaydı (zaman + kanal sütunları) CSV olarak "
            "yazar — o an görüntülenen veri, ham kayıt dosyasının yerini "
            "tutmaz.")
        self.save_csv_btn.clicked.connect(self._save_csv)
        row.addWidget(self.save_csv_btn)

        row.addStretch(1)
        return bar

    def _save_plot_image(self):
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, "Grafiği kaydet", "osiloskop-grafik.png",
            "PNG görüntü (*.png);;SVG çizim (*.svg)")
        if not path:
            return
        try:
            self.export_plot_image(path)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Kaydedilemedi", str(exc))
            return
        self.exported.emit("Grafik kaydedildi: %s" % path)

    def export_plot_image(self, path):
        """Eğriyi PNG ya da SVG olarak diske yazar (bkz. `export_plot_widget`)."""
        export_plot_widget(self.plot, path)

    def _save_screenshot(self):
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, "Ekran görüntüsü al", "osiloskop-ekran.png",
            "PNG görüntü (*.png)")
        if not path:
            return
        if not self.grab().save(path):
            QtWidgets.QMessageBox.warning(
                self, "Kaydedilemedi", "Ekran görüntüsü dosyaya yazılamadı.")
            return
        self.exported.emit("Ekran görüntüsü kaydedildi: %s" % path)

    def _save_csv(self):
        if not self._series_order:
            QtWidgets.QMessageBox.information(
                self, "Veri yok", "Ekranda kaydedilecek bir dalga yok.")
            return
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self, "CSV kaydet", "osiloskop-veri.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            self.export_csv(path)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Kaydedilemedi", str(exc))
            return
        self.exported.emit("CSV kaydedildi: %s" % path)

    def export_csv(self, path):
        """Ekranda duran izleri CSV'ye yazar — her iz için kendi zaman ve
        değer sütunuyla (kanallar farklı örnek sayısına sahip olabilir).
        """
        import csv
        from itertools import zip_longest

        header, columns = [], []
        for name in self._series_order:
            item = self._curves[name]
            xs = list(item.xData) if item.xData is not None else []
            ys = list(item.yData) if item.yData is not None else []
            columns += [xs, ys]
            header += ["t_%s_s" % name, name]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for row in zip_longest(*columns, fillvalue=""):
                writer.writerow(row)

    def _pan(self, divisions):
        """Ekranı bölme cinsinden kaydırır."""
        self.t_position += divisions * self.time_per_div
        self._rescale()

    def _centre_on_trigger(self):
        self.t_position = 0.0
        self._rescale()

    def _on_slider(self, value):
        if self._syncing_slider:
            return
        low, high = self._pan_bounds()
        self.t_position = low + (high - low) * (value / 1000.0)
        self._rescale()

    def _pan_bounds(self):
        """Kaydırmanın sınırları — kaydın dışına çıkıp kaybolmamak için.

        Bir ekran genişliği pay bırakılıyor: kaydın ilk ve son yankısını
        ekranın ortasına getirebilmek gerekiyor.
        """
        if not self._times:
            return -1.0, 1.0
        margin = 0.5 * H_DIVISIONS * self.time_per_div
        return self._times[0] - margin, self._times[-1] + margin

    def _sync_position(self):
        low, high = self._pan_bounds()
        self.t_position = max(low, min(high, self.t_position))
        span = high - low
        # Kaydırma imleci geri yazılırken sinyal bastırılıyor: aksi halde
        # slider -> t_position -> slider döngüsü kuruluyor ve kaydırma
        # düğmeleri tıklandığı yerde takılıp kalıyor.
        self._syncing_slider = True
        try:
            fraction = (self.t_position - low) / span if span else 0.5
            self.position_slider.setValue(int(round(1000 * fraction)))
        finally:
            self._syncing_slider = False
        self.position_label.setText(measure.format_value(self.t_position, "s"))

    def _side_panel(self):
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(260)
        lay = QtWidgets.QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # Sayfaya özgü, isteğe bağlı bir widget (örn. Dalga yakalama'nın
        # "Şok çözümlemesi" tablosu) — bkz. `add_side_widget()`. Boşken içi
        # boş, hiç yer kaplamaz.
        self._side_extra_layout = QtWidgets.QVBoxLayout()
        self._side_extra_layout.setContentsMargins(0, 0, 0, 0)
        self._side_extra_layout.setSpacing(4)
        lay.addLayout(self._side_extra_layout)

        # Sayfaya özgü canlı okumalar (örn. Ses hızı'nın c/Δt/frekans
        # sonuçları) — jenerik dalga ölçümleriyle (Vpp, yükselme…) aynı
        # panelde, osiloskobun hemen yanında dursun diye. Boşken gizli.
        self._readout_box = QtWidgets.QWidget()
        self._readout_grid = QtWidgets.QGridLayout(self._readout_box)
        self._readout_grid.setContentsMargins(0, 0, 0, 6)
        self._readout_grid.setHorizontalSpacing(8)
        self._readout_grid.setVerticalSpacing(2)
        self._readout_box.setVisible(False)
        self._readout_labels = []
        lay.addWidget(self._readout_box)

        caption = QtWidgets.QLabel("Ölçümler")
        caption.setProperty("hint", True)
        lay.addWidget(caption)

        self.measure_combo = QtWidgets.QComboBox()
        for key, label, _unit in measure.MEASUREMENTS:
            self.measure_combo.addItem(label, key)
        add = QtWidgets.QPushButton("Ekle")
        add.clicked.connect(self._add_measurement)
        pick = QtWidgets.QHBoxLayout()
        pick.setSpacing(4)
        pick.addWidget(self.measure_combo, 1)
        pick.addWidget(add)
        lay.addLayout(pick)

        self.measure_table = QtWidgets.QTableWidget(0, 2)
        self.measure_table.setHorizontalHeaderLabels(["Büyüklük", "Değer"])
        self.measure_table.verticalHeader().setVisible(False)
        self.measure_table.setEditTriggers(QtWidgets.QTableWidget.NoEditTriggers)
        self.measure_table.setSelectionBehavior(
            QtWidgets.QTableWidget.SelectRows)
        self.measure_table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.measure_table, 1)

        remove = QtWidgets.QPushButton("Seçileni kaldır")
        remove.clicked.connect(self._remove_measurement)
        lay.addWidget(remove)

        self._measure_keys = []
        return panel

    def add_side_widget(self, widget):
        """Yan panele, ölçüm tablosunun üstüne sayfaya özgü bir widget ekler
        (örn. Dalga yakalama'nın 'Şok çözümlemesi' tablosu). Widget üretimi
        çağıran sayfada kalır — bu yalnızca onu panele yerleştirir.
        """
        self._side_extra_layout.addWidget(widget)

    def set_external_readouts(self, fields):
        """Sayfaya özgü büyüklükler için kart alanını kurar (örn. ses
        hızı'nın c/Δt/frekans sonuçları) — jenerik dalga ölçümleriyle aynı
        panelde, osiloskobun hemen yanında dursun diye.

        `fields`: [(anahtar, etiket), ...]. Bir kez çağrılır; canlı
        güncelleme için dönen ``{anahtar: QLabel}`` sözlüğündeki
        etiketlerin `.setText()`'i çağrılır (aşağıdaki `_readout_box()`
        deki eski desenin aynısı).
        """
        for lbl in self._readout_labels:
            lbl.setParent(None)
        self._readout_labels = []
        while self._readout_grid.count():
            item = self._readout_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        values = {}
        for i, (key, label) in enumerate(fields):
            row, col = divmod(i, 2)
            cap = QtWidgets.QLabel(label)
            cap.setProperty("hint", True)
            val = QtWidgets.QLabel("—")
            val.setProperty("h2", True)
            self._readout_grid.addWidget(cap, row * 2, col)
            self._readout_grid.addWidget(val, row * 2 + 1, col)
            self._readout_labels += [cap, val]
            values[key] = val
        self._readout_box.setVisible(bool(fields))
        return values

    def _cursor_readout(self):
        self.readout = QtWidgets.QLabel("")
        self.readout.setProperty("hint", True)
        self.readout.setWordWrap(True)
        return self.readout

    def _step_button(self, text, slot):
        btn = QtWidgets.QToolButton()
        btn.setText(text)
        btn.setFixedWidth(26)
        btn.clicked.connect(slot)
        return btn

    # --- veri --------------------------------------------------------------
    def _get_curve(self, name, color=None):
        item = self._curves.get(name)
        if item is None:
            idx = len(self._series_order)
            pen_color = color or _SERIES_COLORS[idx % len(_SERIES_COLORS)]
            # Tek izli sayfalarda etiket yok (efsane yalnızca birden çok
            # kanal varken anlamlı); ilk kanal eklendiğinde henüz kaç kanal
            # olacağı bilinmediği için etiket `set_series`te sonradan yazılır.
            item = self.plot.plot(pen=self._pg.mkPen(pen_color, width=1.3))
            self._curves[name] = item
            self._series_order.append(name)
        return item

    def set_data(self, times, values, keep_view=True):
        """Tek izli sayfalar için (Ses hızı, Veri toplama) — 'CH1'i çizer.

        keep_view: canlı akışta ölçek her karede sıfırlanmamalı — operatör
        yakınlaştırdığı yeri kaybeder. İlk karede ya da 'Ekrana sığdır'
        istendiğinde ölçek yeniden hesaplanır.
        """
        self.set_series({"CH1": (times, values)}, keep_view=keep_view)

    def set_series(self, series, keep_view=True, colors=None):
        """Çok izli sayfalar için (Dalga yakalama, CH1+CH2).

        `series`: {ad: (times, values)}. Birden fazla iz varsa otomatik
        olarak bir gösterge (legend) eklenir ve her ize adı yazılır.
        """
        colors = colors or {}
        names = list(series.keys())
        multi = len(names) > 1
        if multi and self._legend is None:
            self._legend = self.plot.addLegend(offset=(-10, 10))

        for old in list(self._curves):
            if old not in names:
                self.plot.removeItem(self._curves.pop(old))
                self._series_order.remove(old)

        for i, name in enumerate(names):
            times, values = series[name]
            times = list(times)
            values = list(values)
            item = self._get_curve(name, colors.get(name))
            if multi and name not in self._legend_registered:
                item.opts["name"] = name
                if self._legend is not None:
                    self._legend.addItem(item, name)
                self._legend_registered.add(name)
            item.setData(times, values)
            if name == "CH1" or i == 0:
                self._times, self._values = times, values

        if self._auto_fit or not keep_view:
            self.fit_to_data()
            self._auto_fit = False
        else:
            self._rescale()
        self.refresh_measurements()

    # --- kalıcı referans çizgileri (tetikleme, ortalama, tolerans bandı) ---
    def set_guides(self, lines):
        """Paket işaretleyicilerinden farklı olarak yeni bir `set_guides`
        çağrılana kadar kalıcı çizgiler. `lines`: bir dizi
        ``{"orientation": "v"|"h", "pos": float, "color": "#hex"}``.
        """
        pg = self._pg
        for item in self._guides:
            self.plot.removeItem(item)
        self._guides = []
        for spec in lines:
            angle = 90 if spec.get("orientation", "v") == "v" else 0
            pen = pg.mkPen(spec.get("color", "#5a6470"), width=1,
                           style=spec.get("style", Qt.DashLine))
            line = pg.InfiniteLine(pos=spec["pos"], angle=angle, pen=pen,
                                   movable=False)
            line.setZValue(-5)
            self.plot.addItem(line)
            self._guides.append(line)

    def clear_overlays(self):
        for item in self._overlays:
            self.plot.removeItem(item)
        self._overlays = []

    def add_region(self, start, end, colour):
        pg = self._pg
        region = pg.LinearRegionItem(values=(start, end), movable=False,
                                     brush=pg.mkBrush(colour))
        region.setZValue(-10)
        self.plot.addItem(region)
        self._overlays.append(region)

    def add_marker(self, position, colour, angle=90):
        pg = self._pg
        line = pg.InfiniteLine(pos=position, angle=angle,
                               pen=pg.mkPen(colour, width=1.2,
                                            style=Qt.DashLine))
        line.setZValue(10)
        self.plot.addItem(line)
        self._overlays.append(line)

    # --- ölçek --------------------------------------------------------------
    def fit_to_data(self):
        if not self._times:
            return
        span_t = self._times[-1] - self._times[0]
        if span_t > 0:
            self.time_per_div = nice_step(span_t / H_DIVISIONS)
            self.t_position = 0.5 * (self._times[0] + self._times[-1])
        lo, hi = min(self._values), max(self._values)
        if hi > lo:
            # %20 pay: tepe tam ekran kenarında dururken kırpılmış mı diye
            # bakılamıyor.
            self.volts_per_div = nice_step(1.2 * (hi - lo) / V_DIVISIONS)
            self.v_offset = 0.5 * (lo + hi)
        self._rescale()

    def _zoom_v(self, direction):
        self.volts_per_div = nice_step(self.volts_per_div, direction)
        self._rescale()

    def _zoom_t(self, direction):
        self.time_per_div = nice_step(self.time_per_div, direction)
        self._rescale()

    def rescale(self):
        """`_rescale()`in genel adı — sayfaların dışarıdan çağırması için."""
        self._rescale()

    def autoscale_y(self):
        """Yalnızca dikey ölçeği veriye oturtur; zaman ekseni/kaydırma
        yerinde kalır. Sürekli akan kayıtlarda (Veri toplama) X ekseni
        `follow()` ile yönetilirken Y'nin her okumada tazelenmesi gerekiyor.
        """
        if not self._values:
            return
        lo, hi = min(self._values), max(self._values)
        if hi > lo:
            self.volts_per_div = nice_step(1.2 * (hi - lo) / V_DIVISIONS)
            self.v_offset = 0.5 * (lo + hi)
        self._rescale()

    def fit_x_to_range(self, x0, x1):
        """Yalnızca X eksenini verilen aralığa oturtur; Y dokunulmaz.

        Sürekli akan kayıtlarda (Veri toplama'nın 'Tümü' penceresi) tüm
        kaydı göstermek isterken Y'nin ayrı bir anahtarla ('Y otomatik')
        yönetilmesi gerekiyor — `fit_to_data()` ikisini birden değiştirir.
        """
        span = x1 - x0
        if span > 0:
            self.time_per_div = nice_step(span / H_DIVISIONS)
            self.t_position = 0.5 * (x0 + x1)
            self._rescale()

    def follow(self, latest_x, window_s):
        """Ekranın sağ kenarını `latest_x`e sabitler, genişliği `window_s`
        saniyeye ayarlar. Sürekli akan kayıtları izlemek için (Veri toplama
        sayfasındaki 'Takip et') — osiloskoptaki 'roll' moduna karşılık gelir.
        """
        self.time_per_div = nice_step(max(window_s, 1e-9) / H_DIVISIONS)
        half_t = 0.5 * H_DIVISIONS * self.time_per_div
        self.t_position = latest_x - half_t
        self._rescale()

    def set_axis_labels(self, bottom=None, left=None):
        if bottom is not None:
            self.plot.setLabel("bottom", bottom)
        if left is not None:
            self.plot.setLabel("left", left)

    def _rescale(self):
        self._sync_position()
        half_t = 0.5 * H_DIVISIONS * self.time_per_div
        half_v = 0.5 * V_DIVISIONS * self.volts_per_div
        # Fare ile sürüklenen/tekerlekle yakınlaştırılan görünüm de
        # `sigRangeChanged` üzerinden buraya geri döner — bastırılmazsa
        # döngü kurar ve 1-2-5 basamağına oturtmaya çalışırken titrer.
        self._syncing_range = True
        try:
            self.plot.setXRange(self.t_position - half_t,
                                self.t_position + half_t, padding=0)
            self.plot.setYRange(self.v_offset - half_v,
                                self.v_offset + half_v, padding=0)
        finally:
            self._syncing_range = False
        self.plot.getAxis("bottom").setTickSpacing(
            major=self.time_per_div, minor=self.time_per_div / 5.0)
        self.plot.getAxis("left").setTickSpacing(
            major=self.volts_per_div, minor=self.volts_per_div / 5.0)
        self.vdiv_label.setText("%s/böl"
                                % measure.format_value(self.volts_per_div, "V"))
        self.tdiv_label.setText("%s/böl"
                                % measure.format_value(self.time_per_div, "s"))
        self._place_cursors()

    def _on_view_range_changed(self, _vb, ranges):
        """Fareyle sürüklenen/tekerlekle yakınlaştırılan görünümü bölme
        durumuyla (`time_per_div`/`t_position`/...) senkron tutar — aksi
        halde bir sonraki +/- düğmesi fare hareketini geçersiz kılardı.
        """
        if self._syncing_range:
            return
        (x0, x1), (y0, y1) = ranges
        if x1 > x0:
            self.time_per_div = (x1 - x0) / H_DIVISIONS
            self.t_position = 0.5 * (x0 + x1)
        if y1 > y0:
            self.volts_per_div = (y1 - y0) / V_DIVISIONS
            self.v_offset = 0.5 * (y0 + y1)
        self._sync_position()
        self.vdiv_label.setText("%s/böl"
                                % measure.format_value(self.volts_per_div, "V"))
        self.tdiv_label.setText("%s/böl"
                                % measure.format_value(self.time_per_div, "s"))
        self._place_cursors()

    # --- imleçler -----------------------------------------------------------
    def _on_cursor_mode(self):
        self.cursor_mode = self.cursor_combo.currentData()
        show_x = self.cursor_mode in (CURSOR_X, CURSOR_XY)
        show_y = self.cursor_mode in (CURSOR_Y, CURSOR_XY)
        self._cursors["x1"].setVisible(show_x)
        self._cursors["x2"].setVisible(show_x)
        self._cursors["y1"].setVisible(show_y)
        self._cursors["y2"].setVisible(show_y)
        self._place_cursors(reset=True)
        self._on_cursor_moved()

    def _place_cursors(self, reset=False):
        """İmleçleri ekranın içinde tutar.

        Ölçek değişince ekran dışında kalan bir imleç sürüklenemez hale
        gelir ve kullanıcı onu geri getiremez.
        """
        half_t = 0.5 * H_DIVISIONS * self.time_per_div
        half_v = 0.5 * V_DIVISIONS * self.volts_per_div
        bounds = {
            "x1": (self.t_position - half_t, self.t_position + half_t,
                   self.t_position - 0.25 * H_DIVISIONS * self.time_per_div),
            "x2": (self.t_position - half_t, self.t_position + half_t,
                   self.t_position + 0.25 * H_DIVISIONS * self.time_per_div),
            "y1": (self.v_offset - half_v, self.v_offset + half_v,
                   self.v_offset - 0.25 * V_DIVISIONS * self.volts_per_div),
            "y2": (self.v_offset - half_v, self.v_offset + half_v,
                   self.v_offset + 0.25 * V_DIVISIONS * self.volts_per_div),
        }
        for key, (low, high, default) in bounds.items():
            line = self._cursors[key]
            line.setBounds((low, high))
            if reset or not (low <= line.value() <= high):
                line.setValue(default)

    def cursor_range(self):
        """(t1, t2) — X imleçleri arası; imleç kapalıysa None."""
        if self.cursor_mode not in (CURSOR_X, CURSOR_XY):
            return None
        a = self._cursors["x1"].value()
        b = self._cursors["x2"].value()
        return (a, b) if a <= b else (b, a)

    def _on_cursor_moved(self):
        parts = []
        span = self.cursor_range()
        if span:
            delta = span[1] - span[0]
            parts.append("X1 %s · X2 %s · ΔX %s"
                         % (measure.format_value(span[0], "s"),
                            measure.format_value(span[1], "s"),
                            measure.format_value(delta, "s")))
            if delta:
                parts.append("1/ΔX %s" % measure.format_value(1.0 / delta, "Hz"))
        if self.cursor_mode in (CURSOR_Y, CURSOR_XY):
            a = self._cursors["y1"].value()
            b = self._cursors["y2"].value()
            parts.append("Y1 %s · Y2 %s · ΔY %s"
                         % (measure.format_value(a, "V"),
                            measure.format_value(b, "V"),
                            measure.format_value(abs(b - a), "V")))
        self.readout.setText("      ".join(parts))
        if self.window_chk.isChecked():
            self.refresh_measurements()

    # --- ölçüm paneli --------------------------------------------------------
    def _add_measurement(self):
        key = self.measure_combo.currentData()
        if key in self._measure_keys:
            return
        self._measure_keys.append(key)
        self.refresh_measurements()

    def _remove_measurement(self):
        row = self.measure_table.currentRow()
        if 0 <= row < len(self._measure_keys):
            del self._measure_keys[row]
            self.refresh_measurements()

    def measurement_keys(self):
        return list(self._measure_keys)

    def set_measurements(self, keys):
        self._measure_keys = [k for k in keys if k in measure.BY_KEY]
        self.refresh_measurements()

    def refresh_measurements(self):
        times, values = self._times, self._values
        span = self.cursor_range() if self.window_chk.isChecked() else None
        if span:
            times, values = measure.window(times, values, span[0], span[1])

        rows = measure.compute_all(self._measure_keys, times, values)
        self.measure_table.setRowCount(len(rows))
        for i, (label, text) in enumerate(rows):
            self.measure_table.setItem(i, 0, QtWidgets.QTableWidgetItem(label))
            self.measure_table.setItem(i, 1, QtWidgets.QTableWidgetItem(text))
        self.measure_table.resizeColumnToContents(0)

    def measurement_results(self):
        """Panelde duran ölçümler — kayda geçirilecek biçimde."""
        times, values = self._times, self._values
        span = self.cursor_range() if self.window_chk.isChecked() else None
        if span:
            times, values = measure.window(times, values, span[0], span[1])
        out = {}
        for key in self._measure_keys:
            out[key] = measure.compute(key, times, values)
        if span:
            out["cursor_start_s"] = span[0]
            out["cursor_end_s"] = span[1]
            out["cursor_delta_s"] = span[1] - span[0]
        return out

    # --- görünüm --------------------------------------------------------------
    def apply_theme(self):
        pg = self._pg
        c = theme.colors()
        # Cihaz ekranı gibi koyu zemin: ızgara ve iz, temanın açık/koyu
        # olmasından bağımsız olarak aynı okunurlukta kalsın diye burada
        # sayfanın paletine uyulmuyor.
        self.plot.setBackground(QtGui.QColor("#101418"))
        for name in ("left", "bottom"):
            axis = self.plot.getAxis(name)
            axis.setPen(pg.mkPen("#5a6470"))
            axis.setTextPen(pg.mkPen("#9aa4b0"))
        self.plot.showGrid(x=True, y=True, alpha=0.45)
        self.curve.setPen(pg.mkPen("#3fa9f5", width=1.3))
        self.readout.setStyleSheet("color: %s;" % c["text_muted"])
