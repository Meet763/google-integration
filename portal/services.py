GOOGLE_SERVICES = (
    {
        "key": "calendar",
        "label": "Google Calendar",
        "hint": "Read meetings and create events",
        "scopes": (
            "https://www.googleapis.com/auth/calendar.events",
            "https://www.googleapis.com/auth/calendar.freebusy",
        ),
    },
    {
        "key": "meet",
        "label": "Google Meet",
        "hint": "Attach a Meet link when an event is created",
        "scopes": ("https://www.googleapis.com/auth/calendar.events",),
    },
    {
        "key": "docs",
        "label": "Google Docs",
        "hint": "Create documents from the app",
        "scopes": ("https://www.googleapis.com/auth/documents",),
    },
    {
        "key": "sheets",
        "label": "Google Sheets",
        "hint": "Create response workbooks for forms",
        "scopes": ("https://www.googleapis.com/auth/spreadsheets",),
    },
    {
        "key": "forms",
        "label": "Google Forms",
        "hint": "Create forms and read submitted responses",
        "scopes": (
            "https://www.googleapis.com/auth/forms.body",
            "https://www.googleapis.com/auth/forms.responses.readonly",
        ),
    },
    {
        "key": "tasks",
        "label": "Google Tasks",
        "hint": "List and create Google Tasks",
        "scopes": ("https://www.googleapis.com/auth/tasks",),
    },
)

SERVICE_KEYS = tuple(item["key"] for item in GOOGLE_SERVICES)
IDENTITY_SCOPES = ("openid", "email", "profile")


def scopes_for(keys: list[str] | tuple[str, ...]) -> list[str]:
    wanted = {key for key in keys if key in SERVICE_KEYS}
    scopes: list[str] = list(IDENTITY_SCOPES)
    seen = set(scopes)
    for item in GOOGLE_SERVICES:
        if item["key"] not in wanted:
            continue
        for scope in item["scopes"]:
            if scope not in seen:
                seen.add(scope)
                scopes.append(scope)
    return scopes


def parse_scope_string(value: str | None) -> list[str]:
    if not value:
        return []
    scopes: list[str] = []
    seen: set[str] = set()
    for part in value.replace(",", " ").split():
        if part and part not in seen:
            seen.add(part)
            scopes.append(part)
    return scopes


def missing_scopes(granted: list[str] | None, needed: list[str]) -> list[str]:
    have = set(granted or [])
    return [scope for scope in needed if scope not in have]
