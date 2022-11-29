from django.utils.translation import gettext_lazy

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2.7 or above to run this plugin!")

__version__ = "1.2.3"


class PluginApp(PluginConfig):
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
        compatibility = "pretix>=3.13.0"

    def ready(self):
        from . import signals  # NOQA


default_app_config = "pretix_payone.PluginApp"
