import hashlib
import logging
import requests
import time
from django.core.cache import cache
from pretix.base.models import Event_SettingsStore
from pretix.base.settings import GlobalSettingsObject
from pretix.helpers.urls import build_absolute_uri

logger = logging.getLogger(__name__)

