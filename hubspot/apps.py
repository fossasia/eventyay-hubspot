from pathlib import Path
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

from . import __version__


try:
    from eventyay.base.plugins import PluginConfig
except ImportError as e:
    raise ImproperlyConfigured("Please use a later version of eventyay") from e


class EventyayHubspotPluginApp(PluginConfig):
    default = True
    name = "hubspot"
    verbose_name = _("Eventyay Hubspot Plugin")

    class EventyayPluginMeta:
        name = _("Hubspot")
        author = "Om Vanwari"
        description = _("This plugin allows you to integrate Eventyay with Hubspot")
        visible = True
        version = __version__
        category = "INTEGRATION"

    def ready(self):
        from . import signals  # NOQA

        # Load environment variables from eventyay-hubspot/.env.hubspot or .env if they exist
        plugin_dir = Path(__file__).resolve().parent.parent
        env_hubspot_path = plugin_dir / ".env.hubspot"
        env_path = plugin_dir / ".env"

        if env_hubspot_path.exists():
            load_dotenv(dotenv_path=env_hubspot_path)
        elif env_path.exists():
            load_dotenv(dotenv_path=env_path)
