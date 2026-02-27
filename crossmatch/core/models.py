from django.db import models
import uuid
from django.utils.translation import gettext_lazy as _
from django.contrib.auth.models import User

from .log import get_logger
logger = get_logger(__name__)



