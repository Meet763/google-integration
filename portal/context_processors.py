from portal.models import GoogleConnection, IntegrationConfig


def portal(request):
    config = None
    connection = None
    try:
        config = IntegrationConfig.get_solo()
    except Exception:
        config = None
    if getattr(request, "user", None) is not None and request.user.is_authenticated:
        connection = GoogleConnection.objects.filter(user=request.user).first()
    calendar_ready = bool(connection and connection.has_service("calendar"))
    meet_ready = bool(connection and connection.has_service("meet"))
    docs_ready = bool(connection and connection.has_service("docs"))
    forms_ready = bool(connection and connection.has_service("forms"))
    sheets_ready = bool(connection and connection.has_service("sheets"))
    tasks_ready = bool(connection and connection.has_service("tasks"))
    return {
        "integration": config,
        "google_connection": connection,
        "calendar_ready": calendar_ready,
        "meet_ready": meet_ready,
        "docs_ready": docs_ready,
        "forms_ready": forms_ready,
        "sheets_ready": sheets_ready,
        "tasks_ready": tasks_ready,
    }
