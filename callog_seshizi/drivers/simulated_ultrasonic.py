"""Ultrasonic echo train simulation -- develop and test without a probe.

Generates the signal a pulse-echo setup would produce for a **known**
velocity and thickness. That is the point: the analysis in `ultrasonic.py`
can be checked against the value the signal was built from, which no
measurement on a real block can offer.

Modelled after a spike pulser/receiver (JSR DPR300 class) driving a single
contact probe in pulse-echo mode, because that is the instrument this is
used with.

What is modelled, and why each part matters
-------------------------------------------
* **Packet shape.** A spike pulser does not produce a tone burst. It dumps
  a fast high-voltage step into the probe and the probe rings down at its
  own resonance, so an echo is a **causal, exponentially damped** sinusoid:
  abrupt onset, decaying tail. A symmetric Gaussian burst -- what a gated
  function generator would give -- is kept as an option, but it is not the
  default, because a symmetric packet makes the envelope centroid land in
  an easier place than it really does.
* **Main bang.** The excitation spike couples straight into the receiver
  and is far larger than any echo -- large enough that the operator's gain
  setting drives it into clipping. That is normal practice: gain is set for
  the echoes and the bang is allowed to saturate. It is modelled, clipping
  included, because a clipped first packet has a different shape from an
  echo, and timing from it to an echo carries a systematic offset.
* **Amplitude decay.** Each round trip loses energy to attenuation and to
  the front-face reflection, so late echoes are small -- which is what makes
  the 4th echo hard to pick and averaging worth having.
* **8-bit quantisation, referred to the clipping level.** The real
  instrument returns a staircase whose step size follows the vertical scale
  the operator chose, not the size of the main bang. Omitting this would
  make the captures smoother than reality and let the sub-sample
  interpolation look better than it is.
"""

import math
import random
import time

import numpy as np

from callog_common.drivers.base import MeasurementFunction, WaveformDriver

#: Default probe and block, matching a typical aluminium setup.
DEFAULTS = {
    "velocity": 6320.0,      # m/s -- longitudinal, aluminium
    "thickness_m": 0.010,    # 10 mm
    "probe_hz": 5e6,         # probe centre frequency
    "cycles": 4.0,           # ring-down length of an echo, in cycles
    "n_echoes": 4,
    "decay": 0.55,           # amplitude ratio between successive echoes
    "amplitude": 0.5,        # V -- first echo, after receiver gain
    "noise": 0.004,          # V RMS
    "excitation": True,
    "excitation_cycles": 2.0,
    "excitation_gain": 8.0,  # main bang, before clipping
    "shape": "damped",       # "damped" (spike pulser) | "gaussian" (tone burst)
    #: Receiver clipping level, as a multiple of the first echo. Gain is set
    #: for the echoes, so the main bang saturates -- that is how the
    #: instrument is actually used.
    "clip_ratio": 4.0,
}

#: Probes on hand. Centre frequency is recorded for traceability only: the
#: analysis measures it from the captured signal rather than trusting a
#: nameplate, so a probe whose real resonance has drifted is still handled.
PROBES = {
    "m639": {"label": "Meccasonics M639 SMN2M5", "hz": 2.5e6, "cycles": 4.0},
    "ichf016": {"label": "ICHF016 (yüksek frekans)", "hz": 16e6, "cycles": 3.0},
}

#: Nominal steps of the calibrated step block, thickest first (mm). The
#: certified value of each step is entered by the operator; these are only
#: what the selector offers.
STEP_BLOCK_MM = (25.0, 20.0, 15.0, 12.5, 10.0, 7.5, 5.0, 2.5)

#: Sample rate the simulated instrument reports, matching the 3000T family's
#: maximum. The record length follows from this and the time span, exactly as
#: it does on the real instrument.
SAMPLE_RATE = 5e9


def echo_train(velocity=None, thickness_m=None, probe_hz=None, cycles=None,
               n_echoes=None, points=None, amplitude=None, decay=None,
               noise=None, excitation=None, excitation_cycles=None,
               excitation_gain=None, span=None, bits=8, seed=None,
               shape=None, clip_ratio=None, recovery=None):
    """Builds a synthetic echo train.

    Return: (times, volts) as numpy arrays. Echo k is centred at
    ``k * 2 * thickness_m / velocity``; the excitation pulse, when present,
    sits at t = 0.
    """
    velocity = float(velocity or DEFAULTS["velocity"])
    thickness_m = float(thickness_m or DEFAULTS["thickness_m"])
    probe_hz = float(probe_hz or DEFAULTS["probe_hz"])
    cycles = float(cycles or DEFAULTS["cycles"])
    n_echoes = int(n_echoes or DEFAULTS["n_echoes"])
    amplitude = DEFAULTS["amplitude"] if amplitude is None else float(amplitude)
    decay = DEFAULTS["decay"] if decay is None else float(decay)
    noise = DEFAULTS["noise"] if noise is None else float(noise)
    excitation = DEFAULTS["excitation"] if excitation is None else bool(excitation)
    excitation_cycles = float(excitation_cycles
                              or DEFAULTS["excitation_cycles"])
    excitation_gain = float(excitation_gain or DEFAULTS["excitation_gain"])
    shape = shape or DEFAULTS["shape"]
    clip_ratio = float(clip_ratio or DEFAULTS["clip_ratio"])

    round_trip = 2.0 * thickness_m / velocity
    if span is None:
        span = (n_echoes + 0.5) * round_trip
    start = -0.3 * round_trip

    if points is None:
        # Follow the instrument: the record length is the time span times the
        # sample rate, capped so a thick block does not produce a gigantic
        # array during tests.
        points = int(min(200000, max(2000, round((span - start) * SAMPLE_RATE))))
    points = int(points)

    times = np.linspace(start, span, points)
    volts = np.zeros(points)

    rng = np.random.default_rng(seed)

    def burst(centre, gain, packet_cycles):
        local = times - centre
        if shape == "gaussian":
            # Gated tone burst: symmetric, envelope down to ~10% at half the
            # packet length.
            sigma = packet_cycles / (2.0 * 1.517 * probe_hz)
            envelope = np.exp(-(local / sigma) ** 2)
        else:
            # Spike excitation: the probe is struck once and rings down. The
            # decay constant is set so the envelope reaches 10% after
            # `packet_cycles` cycles, matching how the two shapes are
            # specified so they stay comparable.
            tau = packet_cycles / (2.3 * probe_hz)
            envelope = np.where(local >= 0.0, np.exp(-local / tau), 0.0)
        return gain * envelope * np.sin(2.0 * np.pi * probe_hz * local)

    if excitation:
        volts += burst(0.0, amplitude * excitation_gain, excitation_cycles)
    for k in range(1, n_echoes + 1):
        volts += burst(k * round_trip, amplitude * (decay ** (k - 1)), cycles)

    # Alıcı toparlanma kuyruğu: yüksek gerilimli darbeden sonra alıcı
    # sıfıra yavaşça dönüyor ve bu yavaş kuyruk yankıların altında akıyor.
    # Zamanlama bilgisi taşımaz ama zarfı kaldırdığı için iki yankı arası
    # sessizlik tabana inmez ve paketler birbirine karışır. Sahada bu,
    # "yüksek geçiren filtreyi yükseltince yankıları buluyor" olarak
    # görünüyor.
    if recovery:
        slow = float(recovery) * amplitude
        tau_slow = 1.5 * round_trip
        volts = volts + np.where(times >= 0.0,
                                 slow * np.exp(-times / tau_slow), 0.0)

    if noise > 0:
        volts = volts + rng.normal(0.0, noise, points)

    # Receiver saturation. The limit is tied to the echo amplitude, not to
    # the main bang, because that is what the gain knob is set against.
    clip_v = clip_ratio * amplitude if clip_ratio > 0 else None
    if clip_v:
        volts = np.clip(volts, -clip_v, clip_v)

    if bits:
        full_scale = 2.0 * (clip_v if clip_v
                            else float(np.max(np.abs(volts))))
        lsb = full_scale / float(2 ** int(bits))
        if lsb > 0:
            volts = np.round(volts / lsb) * lsb

    return times, volts


class SimulatedUltrasonic(WaveformDriver):
    """Mimics a DSOX3012T watching a pulse-echo setup."""

    FUNCTIONS = [
        MeasurementFunction("VPP", "Tepeden tepeye gerilim", "V"),
        MeasurementFunction("VMAX", "En büyük gerilim", "V"),
        MeasurementFunction("VMIN", "En küçük gerilim", "V"),
        MeasurementFunction("VRMS", "RMS gerilim", "V"),
    ]

    CHANNELS = (("CHANnel1", "Kanal 1"), ("CHANnel2", "Kanal 2"))

    DEFAULT_POINTS = 20000

    def __init__(self, address="SIM", channel="CHANnel1", trigger_delay=None,
                 velocity=None, thickness_m=None, probe_hz=None, cycles=None,
                 n_echoes=None, noise=None, excitation=None, **kwargs):
        WaveformDriver.__init__(self, address, **kwargs)
        self.channel = channel if str(channel).startswith("CHAN") else "CHANnel1"
        self._trigger_delay = trigger_delay
        self._armed = False
        self._armed_at = 0.0

        #: The block being measured. The interface refreshes these before
        #: every frame, because the operator changes the thickness while the
        #: live monitor is running -- that is the whole point of the page.
        self.velocity = velocity or DEFAULTS["velocity"]
        self.thickness_m = thickness_m or DEFAULTS["thickness_m"]
        self.probe_hz = probe_hz or DEFAULTS["probe_hz"]
        self.cycles = cycles or DEFAULTS["cycles"]
        self.n_echoes = n_echoes or DEFAULTS["n_echoes"]
        self.noise = DEFAULTS["noise"] if noise is None else noise
        self.excitation = (DEFAULTS["excitation"] if excitation is None
                           else bool(excitation))
        self._averaging = 1
        #: HRESolution kipi. Gercek cihazda komsu ornekleri ortalayarak 8
        #: bitin ustune cikar; burada kuantalama adimini kucultmekle
        #: modellenir. Dorduncu yanki ilk yankinin altida birine indigi icin
        #: 8 bitte birkac basamaga sikisiyor -- fark buradan gorulur.
        self.high_resolution = False
        self._setup = {
            "volts_per_div": 0.5, "time_per_div": 1e-6, "offset": 0.0,
            "probe_ratio": 1.0, "time_position": 0.0, "trigger_level": 0.0,
            # Supurme kipi de saklaniyor: yakalama sirasinda AUTO'da kalmak
            # kareleri darbeyle eszamansiz birakiyor ve bu, gercek cihazda
            # sessizce yanlis veri uretiyor. Simulasyon bunu tutmazsa test
            # de yakalayamaz.
            "trigger_sweep": None,
        }

    @property
    def is_simulated(self):
        return True

    # --- lifecycle -----------------------------------------------------
    def connect(self):
        self.identity = ("KEYSIGHT TECHNOLOGIES,DSO-X 3012T,SIM-DSOX3012T,"
                         "SIMULASYON-1.0")
        return self.identity

    def close(self):
        self._armed = False

    def identify(self):
        return self.identity or self.connect()

    def check_errors(self):
        return []

    def sample_rate(self):
        return SAMPLE_RATE

    # --- scalar measurement --------------------------------------------
    def configure(self, function_key, channel=None, averaging=None, **settings):
        if channel:
            self.channel = channel
        if averaging:
            self._averaging = int(averaging)
        self._function = function_key

    def set_high_resolution(self, on=True, averaging=None):
        self.high_resolution = bool(on) and not averaging
        if averaging:
            self._averaging = int(averaging)

    def read_one(self):
        _times, volts = self._frame()
        value = {
            "VPP": float(np.max(volts) - np.min(volts)),
            "VMAX": float(np.max(volts)),
            "VMIN": float(np.min(volts)),
            "VRMS": float(np.sqrt(np.mean(volts ** 2))),
        }.get(self._function)
        if value is None:
            raise KeyError("Bu cihazda olmayan fonksiyon: %s" % self._function)
        return value, "%+.4E" % value

    # --- waveform capture ------------------------------------------------
    def displayed_channels(self, force=()):
        return [name for name, _label in self.CHANNELS]

    def arm(self):
        self._armed = True
        self._armed_at = time.monotonic()

    def wait_trigger(self, timeout_s=None, should_stop=None, poll_s=0.02):
        """The function generator triggers continuously, so this is quick."""
        delay = (self._trigger_delay if self._trigger_delay is not None
                 else random.uniform(0.01, 0.05))
        deadline = self._armed_at + delay
        while True:
            if should_stop is not None and should_stop():
                return False
            now = time.monotonic()
            if now >= deadline:
                self._armed = False
                return True
            if timeout_s is not None and (now - self._armed_at) > timeout_s:
                return False
            time.sleep(min(poll_s, max(0.0, deadline - now)))

    def read_waveform(self, source, points=None):
        if str(source).endswith("2"):
            return self._sync_waveform(points)
        return self._frame(points)

    def _frame(self, points=None):
        # Hardware averaging reduces the noise by sqrt(N) -- the reason the
        # 3rd and 4th echoes become pickable at all on a real setup.
        noise = self.noise / math.sqrt(max(1, self._averaging))
        return echo_train(
            velocity=self.velocity, thickness_m=self.thickness_m,
            probe_hz=self.probe_hz, cycles=self.cycles,
            n_echoes=self.n_echoes, noise=noise, excitation=self.excitation,
            bits=12 if self.high_resolution else 8,
            points=points or self.DEFAULT_POINTS)

    def _sync_waveform(self, points=None):
        """Channel 2: the function generator's sync output."""
        n = int(points or self.DEFAULT_POINTS)
        round_trip = 2.0 * self.thickness_m / self.velocity
        span = (self.n_echoes + 0.5) * round_trip
        times = np.linspace(-0.3 * round_trip, span, n)
        volts = np.where((times >= 0) & (times < 0.1 * round_trip), 3.3, 0.0)
        return times, volts + np.random.normal(0.0, 0.01, n)

    def run(self):
        self._armed = False

    def stop(self):
        self._armed = False

    def screenshot(self, path, palette="COLor"):
        raise NotImplementedError(
            "Simülasyon sürücüsü ekran görüntüsü üretmiyor.")

    # --- scale and trigger ------------------------------------------------
    def apply_setup(self, channel=None, averaging=None, **settings):
        if channel:
            self.channel = channel
        if averaging:
            self._averaging = int(averaging)
        for key, value in settings.items():
            if value is not None and key in self._setup:
                self._setup[key] = value
        return self.read_setup(channel)

    def read_setup(self, channel=None):
        out = dict(self._setup)
        out["channel"] = channel or self.channel
        return out

    def autoscale(self, channel=None):
        return self.read_setup(channel)

    def set_sweep(self, mode):
        pass
