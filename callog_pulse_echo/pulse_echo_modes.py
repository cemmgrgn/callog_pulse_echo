"""Registers this app's test mode into the shared `testmodes` registry.

Importing this module (done once, from `callog_pulse_echo/__init__.py`) is
what makes `testmodes.MODES`/`testmodes.get()` know about the sound-velocity
mode — the registry itself lives in `callog_common.testmodes` and knows
nothing about ultrasonic measurement specifically. See
`callog_defib/defib_modes.py` for the same pattern on the defib side.
"""

from callog_common import testmodes

SOUND_VELOCITY = "sound_velocity"

#: Ses hızı ölçümü. Kendi sayfası var (canlı izleme akışı dalga yakalamadan
#: farklı), ama ölçek/tetikleme varsayılanları ve kayda geçen mod anahtarı
#: diğerleriyle aynı yerde dursun diye burada tanımlı.
#:
#: Zaman tabanı burada sabit **değil**: yankı aralığı 2d/c olduğu için
#: kalınlığa bağlı ve sayfada `timebase_for` ile hesaplanıyor. Sabit bir
#: değer 25 mm'de dört yankıyı ekrana sığdırmaz, 2,5 mm'de ise hepsini tek
#: bir dikey çizgiye ezerdi.
SOUND_VELOCITY_MODE = testmodes.register_mode(testmodes.TestMode(
    key=SOUND_VELOCITY,
    label="Ses hızı — darbe/yankı",
    description=(
        "Bloktan dönen yankı dizisinden ses hızını hesaplar. Tek prob, "
        "darbe-yankı (pulser/receiver ECHO konumunda)."),
    setup={
        # Ana darbenin yükselen kenarı tetikleme için fazlasıyla büyük ve
        # tekrarlanabilir. Ölçülen büyüklük yankılar **arası** fark olduğu
        # için t=0'ın nerede olduğu sonuca girmiyor — tetiklemeyi sinyalin
        # kendisinden almak burada sakınca yaratmıyor.
        "trigger_slope": "POSitive",
        "coupling": "DC",
        "time_position": 0.0,
    },
    capture={"points": 50000, "count": 0, "timeout_s": 5},
    chain={},
    warning=(
        "Pulser/receiver ECHO konumunda olmalı ve alıcı çıkışı osiloskoba "
        "bağlanmalı. Ana darbe alıcıda kırpılır — normaldir, kazanç "
        "yankılara göre ayarlanır."),
))


def timebase_for(thickness_m, velocity, echoes=4, divisions=10):
    """Yankı dizisini ekrana sığdıran zaman tabanı (s/bölme).

    Dizinin uzunluğu `echoes * 2d/c`; üstüne son yankının kuyruğu ve
    tetikleme öncesi için pay bırakılıyor. Kalınlık canlı değiştirilebildiği
    için bu her değişimde yeniden hesaplanıp cihaza yazılır.
    """
    if not thickness_m or not velocity:
        return None
    span = (echoes + 0.6) * 2.0 * float(thickness_m) / float(velocity)
    return max(testmodes.MIN_TIME_PER_DIV_S, span / float(divisions))
