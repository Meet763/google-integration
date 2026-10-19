from django.conf import settings
from django.db import models

from portal.crypto import decrypt, encrypt


def default_google_services() -> list[str]:
    return ["calendar", "meet"]


class IntegrationConfig(models.Model):
    """Single row of Google OAuth settings entered in the admin portal."""

    client_id = models.CharField(max_length=255, blank=True)
    client_secret_encrypted = models.TextField(blank=True)
    redirect_uri = models.URLField(
        max_length=500,
        default="http://127.0.0.1:8000/oauth/callback/",
    )
    meet_allowed = models.BooleanField(default=True)
    attendees_allowed = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "integration config"

    def __str__(self) -> str:
        return "Google Calendar integration"

    @classmethod
    def get_solo(cls) -> "IntegrationConfig":
        obj, _created = cls.objects.get_or_create(pk=1)
        obj._apply_settings_overrides()
        return obj

    def _apply_settings_overrides(self) -> None:
        client_id = getattr(settings, "GOOGLE_CLIENT_ID", "")
        client_secret = getattr(settings, "GOOGLE_CLIENT_SECRET", "")
        redirect_uri = getattr(settings, "GOOGLE_REDIRECT_URI", "")
        if client_id:
            self.client_id = client_id
        if client_secret:
            self.client_secret_encrypted = encrypt(client_secret)
        if redirect_uri:
            self.redirect_uri = redirect_uri
        self.meet_allowed = bool(getattr(settings, "GOOGLE_MEET_ALLOWED", self.meet_allowed))
        self.attendees_allowed = bool(getattr(settings, "GOOGLE_ATTENDEES_ALLOWED", self.attendees_allowed))

    @property
    def client_secret(self) -> str:
        return decrypt(self.client_secret_encrypted)

    @client_secret.setter
    def client_secret(self, value: str) -> None:
        self.client_secret_encrypted = encrypt(value)

    @property
    def has_secret(self) -> bool:
        return bool(self.client_secret)

    @property
    def is_ready(self) -> bool:
        return bool(self.client_id and self.has_secret and self.redirect_uri)


class GoogleConnection(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="google_connection",
    )
    google_sub = models.CharField(max_length=255, unique=True)
    email = models.EmailField()
    refresh_token_encrypted = models.TextField(blank=True)
    access_token_encrypted = models.TextField(blank=True)
    token_expiry = models.DateTimeField(null=True, blank=True)
    enabled_services = models.JSONField(default=default_google_services, blank=True)
    granted_scopes = models.JSONField(default=list, blank=True)
    google_enabled = models.BooleanField(default=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.email

    def has_service(self, key: str) -> bool:
        return self.google_enabled and self.is_connected and key in (self.enabled_services or [])

    @property
    def refresh_token(self) -> str:
        return decrypt(self.refresh_token_encrypted)

    @refresh_token.setter
    def refresh_token(self, value: str) -> None:
        self.refresh_token_encrypted = encrypt(value)

    @property
    def access_token(self) -> str:
        return decrypt(self.access_token_encrypted)

    @access_token.setter
    def access_token(self, value: str) -> None:
        self.access_token_encrypted = encrypt(value)

    @property
    def is_connected(self) -> bool:
        return bool(self.refresh_token)
