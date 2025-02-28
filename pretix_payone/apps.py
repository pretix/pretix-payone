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
        picture = "pretix_payone/logo.svg"
        visible = True
        version = __version__
        category = "PAYMENT"
        compatibility = "pretix>=4.20.0"

        @property
        def description(self):
            t = gettext_lazy("Accept payments through PAYONE (formerly BS Payone).")
            t += '<div class="text text-info"><span class="fa fa-info-circle"></span> '
            t += gettext_lazy(
                "Also referred to as the <em>Kieler Platform</em>. Use this extension, if PAYONE has provided you with "
                "a <em>Merchant ID</em>."
            )
            t += "</div>"

            return t

    def ready(self):
        from . import signals  # NOQA
