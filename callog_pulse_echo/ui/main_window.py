"""CalLog Pulse-Echo's main window: the shared `callog_common` window plus
the velocity page.

Everything else (home, devices, sessions, measurement, approvals, history,
admin, theme/menu/backup machinery) comes from `BaseMainWindow` unchanged;
see that module's docstring for the override points used here.
"""

import os

from callog_common import db, perms
from callog_common.ui.main_window import MainWindow as BaseMainWindow
from callog_common.ui.main_window import has_active_scope
from .velocity_page import VelocityPage

#: Generic, no institution-specific screenshots — safe under `docs/` and
#: distributed with the repo, unlike callog_defib's operator guide.
SETUP_GUIDE_PDF = os.path.join(db.APP_DIR, "docs", "pulse-echo-kurulum.pdf")


class MainWindow(BaseMainWindow):

    def _app_title(self):
        return "CalLog Pulse-Echo"

    def _app_version_info(self):
        from .. import __author__, __version__
        return __version__, __author__

    def _extra_page_available(self):
        return has_active_scope()

    def _extra_page_permission(self):
        return perms.VIEW_VELOCITY

    def _build_extra_page(self):
        self.velocity = VelocityPage(self.state)
        return self.velocity

    def _extra_page_meta(self):
        return ("velocity", "Pulse-Echo", "velocity",
                "Darbe/yankı ile canlı izleme ve ölçüm.")

    def _extra_page_shortcut_entry(self):
        return ("velocity", "Pulse-Echo")

    def _refresh_extra_appearance(self):
        if self._extra is not None:
            self._extra.apply_plot_theme()

    def _shutdown_extra(self):
        if self._extra is not None:
            self._extra.shutdown()

    def _operator_guides(self):
        return [("Pulse-Echo kurulum kılavuzu (PDF)", SETUP_GUIDE_PDF,
                 "Pulse-Echo Ölçümü — Kurulum Kılavuzu")]
