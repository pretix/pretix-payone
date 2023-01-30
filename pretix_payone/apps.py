from django.utils.translation import gettext_lazy
from . import __version__

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2.7 or above to run this plugin!")


class PluginApp(PluginConfig):
    default = True
    name = "pretix_payone"
    verbose_name = "PAYONE"

    class PretixPluginMeta:
        name = gettext_lazy("PAYONE")
        author = "pretix team"
        description = gettext_lazy(
            "Accept payments through PAYONE (formerly BS Payone)."
        )
        picture = "pretix_payone/logo.svg"
        visible = True
        version = __version__
        category = "PAYMENT"
        compatibility = "pretix>=4.16.0"

    def ready(self):
        from . import signals  # NOQA


