from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _

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
        import logging

        logger = logging.getLogger(__name__)

        # .env.hubspot is only for local development convenience.
        # In production, configure credentials via the admin UI (Global Settings).
        plugin_dir = Path(__file__).resolve().parent.parent
        env_path = plugin_dir / ".env.hubspot"
        if env_path.exists():
            try:
                from dotenv import load_dotenv
            except ImportError:
                logger.warning("python-dotenv is not installed; skipping .env.hubspot loading")
            else:
                load_dotenv(dotenv_path=env_path)
                logger.info(f"HubSpot plugin loaded dev environment variables from: {env_path}")
