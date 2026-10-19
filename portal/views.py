import secrets
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from portal.forms import (
    EventForm,
    GoogleDocForm,
    GoogleFormBuilderForm,
    GoogleFormLookupForm,
    GoogleSheetForm,
    GoogleTaskForm,
    GoogleConnectForm,
    ScheduleForm,
)
from portal.google_api import (
    CalendarError,
    authorization_url,
    create_event,
    create_form_with_response_sheet,
    create_google_doc,
    create_google_sheet,
    create_google_task,
    day_availability,
    exchange_code,
    fetch_userinfo,
    get_google_form_overview,
    list_google_tasks,
)
from portal.models import GoogleConnection, IntegrationConfig
from portal.services import GOOGLE_SERVICES, missing_scopes, parse_scope_string, scopes_for


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//"):
        return reverse("home")
    return value


def home(request):
    today = None
    schedule_error = ""
    connection = None
    if request.user.is_authenticated:
        connection = GoogleConnection.objects.filter(user=request.user).first()
        if connection and connection.has_service("calendar"):
            try:
                today = day_availability(connection, timezone.localdate(), "Asia/Kolkata", 45)
            except CalendarError as exc:
                schedule_error = str(exc)
    connect_form = GoogleConnectForm(
        initial={
            "google_enabled": bool(connection and connection.google_enabled),
            "services": (connection.enabled_services if connection and connection.enabled_services else ["calendar", "meet"]),
        }
    )
    return render(
        request,
        "portal/home.html",
        {
            "next": _safe_next(request.GET.get("next")),
            "today": today,
            "schedule_error": schedule_error,
            "connect_form": connect_form,
            "google_services": GOOGLE_SERVICES,
        },
    )


@require_http_methods(["GET", "POST"])
def oauth_start(request):
    config = IntegrationConfig.get_solo()
    if not config.is_ready:
        messages.error(request, "Add the Google client id and secret in settings before anyone can connect.")
        return redirect("home")

    if request.method == "POST":
        form = GoogleConnectForm(request.POST)
        if not form.is_valid():
            for error in form.errors.get("services", []):
                messages.error(request, error)
            return redirect("home")
        if not form.cleaned_data["google_enabled"]:
            return _disable_google(request)
        selected = form.cleaned_data["services"]
    else:
        selected = [key for key in request.GET.getlist("service") if key]
        if not selected:
            selected = ["calendar", "meet"]

    needed = scopes_for(selected)
    connection = None
    if request.user.is_authenticated:
        connection = GoogleConnection.objects.filter(user=request.user).first()
        if (
            connection
            and connection.is_connected
            and not missing_scopes(connection.granted_scopes, needed)
        ):
            connection.google_enabled = True
            connection.enabled_services = selected
            connection.save(update_fields=["google_enabled", "enabled_services", "updated_at"])
            messages.success(request, "Google services saved. The saved token stays in use.")
            return redirect("home")

    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    request.session["oauth_next"] = _safe_next(request.POST.get("next") or request.GET.get("next"))
    request.session["oauth_services"] = selected
    request.session["oauth_scopes"] = needed
    request.session.modified = True
    return redirect(
        authorization_url(
            config,
            state,
            needed,
            force_consent=not (connection and connection.refresh_token),
        )
    )


@require_http_methods(["GET"])
def oauth_callback(request):
    error = request.GET.get("error")
    if error:
        messages.error(request, "Google sign-in was cancelled.")
        return redirect("home")

    expected = request.session.pop("oauth_state", "")
    destination = request.session.pop("oauth_next", reverse("home"))
    selected = request.session.pop("oauth_services", ["calendar", "meet"])
    requested_scopes = request.session.pop("oauth_scopes", scopes_for(selected))
    if not expected or request.GET.get("state") != expected:
        messages.error(request, "Sign-in could not be verified. Try connecting again.")
        return redirect("home")

    code = request.GET.get("code")
    if not code:
        messages.error(request, "Google did not return a sign-in code.")
        return redirect("home")

    config = IntegrationConfig.get_solo()
    try:
        token = exchange_code(config, code)
        profile = fetch_userinfo(token["access_token"])
    except CalendarError as exc:
        messages.error(request, str(exc))
        return redirect("home")
    except Exception:
        messages.error(request, "Google sign-in did not finish. Check the admin client settings.")
        return redirect("home")

    user = _user_from_google(profile)
    connection, _created = GoogleConnection.objects.get_or_create(
        user=user,
        defaults={"google_sub": profile["sub"], "email": profile["email"].lower()},
    )
    connection.google_sub = profile["sub"]
    connection.email = profile["email"].lower()
    connection.access_token = token.get("access_token", "")
    refresh = token.get("refresh_token")
    if refresh:
        connection.refresh_token = refresh
    expires_in = token.get("expires_in")
    if expires_in:
        connection.token_expiry = timezone.now() + timedelta(seconds=int(expires_in))
    granted = parse_scope_string(token.get("scope", "")) or requested_scopes
    connection.granted_scopes = granted
    connection.enabled_services = selected
    connection.google_enabled = True
    connection.save()
    if not connection.refresh_token:
        messages.error(request, "Google did not grant offline access. Enable Google again and allow permission.")
        return redirect("home")
    login(request, user)
    labels = [item["label"] for item in GOOGLE_SERVICES if item["key"] in selected]
    messages.success(request, f"Google is connected for {connection.email}: {', '.join(labels)}.")
    return redirect(destination)


def _user_from_google(profile: dict) -> User:
    email = profile["email"].lower()
    sub = profile["sub"]
    existing = GoogleConnection.objects.filter(google_sub=sub).select_related("user").first()
    if existing:
        user = existing.user
        if user.email != email:
            user.email = email
            user.save(update_fields=["email"])
        return user

    user = User.objects.filter(username__iexact=email).exclude(is_staff=True).first()
    if user is None:
        user = User.objects.filter(email__iexact=email, is_staff=False).first()
    if user is None:
        username = email
        if User.objects.filter(username__iexact=username).exists():
            username = f"g-{sub}"[:150]
        user = User(username=username, email=email)
        user.set_unusable_password()
        user.save()
    elif user.email != email:
        user.email = email
        user.save(update_fields=["email"])
    return user


@login_required
@require_POST
def disable_google(request):
    return _disable_google(request)


def _disable_google(request):
    if request.user.is_authenticated:
        connection = GoogleConnection.objects.filter(user=request.user).first()
        if connection:
            connection.google_enabled = False
            connection.save(update_fields=["google_enabled", "updated_at"])
            messages.success(request, "Google integration is off. The saved token is kept so you do not have to sign in again.")
            return redirect("home")
    messages.success(request, "Google integration is off.")
    return redirect("home")


@login_required
@require_POST
def disconnect(request):
    GoogleConnection.objects.filter(user=request.user).delete()
    logout(request)
    messages.success(request, "Google was disconnected and the saved token was removed.")
    return redirect("home")


@require_POST
def logout_view(request):
    logout(request)
    return redirect("home")


@login_required
@require_http_methods(["GET"])
def schedule(request):
    connection = _require_connection(request)
    if connection is None:
        return redirect("home")

    if request.GET:
        data = request.GET.copy()
        data.setdefault("window_start", "09:00")
        data.setdefault("window_end", "18:00")
        if "duration" not in data:
            data["duration"] = "45"
        if "timezone_name" not in data:
            data["timezone_name"] = "Asia/Kolkata"
        form = ScheduleForm(data)
    else:
        form = ScheduleForm(
            {
                "day": timezone.localdate().isoformat(),
                "window_start": "09:00",
                "window_end": "18:00",
                "duration": 45,
                "timezone_name": "Asia/Kolkata",
            }
        )
    result = None
    selected = None
    if form.is_valid():
        selected = form.cleaned_data
        try:
            result = day_availability(
                connection,
                selected["day"],
                selected["timezone_name"],
                selected["duration"],
                selected["window_start"],
                selected["window_end"],
            )
        except CalendarError as exc:
            messages.error(request, str(exc))

    return render(
        request,
        "portal/schedule.html",
        {"form": form, "result": result, "selected": selected},
    )


@login_required
@require_http_methods(["GET", "POST"])
def event_new(request):
    connection = _require_connection(request)
    if connection is None:
        return redirect("home")

    config = IntegrationConfig.get_solo()
    initial = _event_initial(request)
    if request.method == "POST":
        form = EventForm(request.POST, config=config, connection=connection)
        if form.is_valid():
            try:
                created = create_event(
                    connection,
                    title=form.cleaned_data["title"],
                    start=form.cleaned_data["start_at"],
                    end=form.cleaned_data["end_at"],
                    tz_name=form.cleaned_data["timezone_name"],
                    meet=form.cleaned_data["meet_enabled"],
                    attendees=form.cleaned_data["attendee_emails"],
                )
            except CalendarError as exc:
                messages.error(request, str(exc))
            else:
                return render(request, "portal/event_done.html", {"event": created})
    else:
        form = EventForm(initial=initial, config=config, connection=connection)

    return render(request, "portal/event_form.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
def meet_new(request):
    connection = _require_google_service(request, "meet", "Turn on Google Meet in the integration panel first.")
    if connection is None:
        return redirect("home")

    config = IntegrationConfig.get_solo()
    initial = _event_initial(request)
    initial.setdefault("title", "Google Meet")
    if request.method == "POST":
        form = EventForm(request.POST, config=config, connection=connection)
        if form.is_valid():
            try:
                created = create_event(
                    connection,
                    title=form.cleaned_data["title"],
                    start=form.cleaned_data["start_at"],
                    end=form.cleaned_data["end_at"],
                    tz_name=form.cleaned_data["timezone_name"],
                    meet=True,
                    attendees=form.cleaned_data["attendee_emails"],
                )
            except CalendarError as exc:
                messages.error(request, str(exc))
            else:
                return render(request, "portal/event_done.html", {"event": created})
    else:
        form = EventForm(initial=initial, config=config, connection=connection)
        form.fields["meet_enabled"].initial = True
        form.fields["meet_enabled"].disabled = True

    return render(request, "portal/meet_form.html", {"form": form})


@login_required
@require_http_methods(["GET", "POST"])
def google_doc_new(request):
    connection = _require_google_service(request, "docs", "Turn on Google Docs in the integration panel first.")
    if connection is None:
        return redirect("home")

    created = None
    if request.method == "POST":
        form = GoogleDocForm(request.POST)
        if form.is_valid():
            try:
                created = create_google_doc(
                    connection,
                    title=form.cleaned_data["title"],
                    body=form.cleaned_data["body"],
                )
            except CalendarError as exc:
                messages.error(request, str(exc))
    else:
        form = GoogleDocForm(initial={"title": "New workspace doc"})

    return render(request, "portal/google_doc_form.html", {"form": form, "created": created})


@login_required
@require_http_methods(["GET", "POST"])
def google_sheet_new(request):
    connection = _require_google_service(request, "sheets", "Turn on Google Sheets in the integration panel first.")
    if connection is None:
        return redirect("home")

    created = None
    if request.method == "POST":
        form = GoogleSheetForm(request.POST)
        if form.is_valid():
            try:
                created = create_google_sheet(
                    connection,
                    title=form.cleaned_data["title"],
                    rows=form.cleaned_data["rows"],
                )
            except CalendarError as exc:
                messages.error(request, str(exc))
    else:
        form = GoogleSheetForm(initial={"title": "New workspace sheet"})

    return render(request, "portal/google_sheet_form.html", {"form": form, "created": created})


@login_required
@require_http_methods(["GET", "POST"])
def google_tasks(request):
    connection = _require_google_service(request, "tasks", "Turn on Google Tasks in the integration panel first.")
    if connection is None:
        return redirect("home")

    if request.method == "POST":
        form = GoogleTaskForm(request.POST)
        if form.is_valid():
            try:
                create_google_task(
                    connection,
                    title=form.cleaned_data["title"],
                    notes=form.cleaned_data["notes"],
                    due=form.cleaned_data["due"],
                )
                messages.success(request, "Task created in Google Tasks.")
                return redirect("google_tasks")
            except CalendarError as exc:
                messages.error(request, str(exc))
    else:
        form = GoogleTaskForm()

    task_data = {"tasks": [], "tasklists": []}
    try:
        task_data = list_google_tasks(connection)
    except CalendarError as exc:
        messages.error(request, str(exc))
    return render(request, "portal/google_tasks.html", {"form": form, "task_data": task_data})


@login_required
@require_http_methods(["GET", "POST"])
def google_form_new(request):
    connection = _require_google_service(request, "forms", "Turn on Google Forms in the integration panel first.")
    if connection is None:
        return redirect("home")

    if request.method == "POST":
        form = GoogleFormBuilderForm(request.POST)
        if form.is_valid():
            try:
                created = create_form_with_response_sheet(
                    connection,
                    title=form.cleaned_data["title"],
                    description=form.cleaned_data["description"],
                    questions=form.cleaned_data["questions"],
                    collect_email=form.cleaned_data["collect_email"],
                    create_sheet=form.cleaned_data["create_sheet"],
                )
            except CalendarError as exc:
                messages.error(request, str(exc))
            else:
                return render(request, "portal/google_form_done.html", {"created": created})
    else:
        form = GoogleFormBuilderForm(
            initial={
                "title": "New intake form",
                "questions": "Name\nCompany\nWhat do you need help with?",
                "collect_email": True,
                "create_sheet": bool(connection.has_service("sheets")),
            }
        )

    return render(
        request,
        "portal/google_form_form.html",
        {
            "form": form,
            "sheets_ready": connection.has_service("sheets"),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def google_form_lookup(request):
    connection = _require_google_service(request, "forms", "Turn on Google Forms in the integration panel first.")
    if connection is None:
        return redirect("home")

    overview = None
    if request.method == "POST":
        form = GoogleFormLookupForm(request.POST)
        if form.is_valid():
            try:
                overview = get_google_form_overview(connection, form_id=form.cleaned_data["form_id"])
            except CalendarError as exc:
                messages.error(request, str(exc))
    else:
        form = GoogleFormLookupForm()

    return render(request, "portal/google_form_lookup.html", {"form": form, "overview": overview})


def _event_initial(request) -> dict:
    initial = {"day": timezone.localdate(), "duration": 45, "timezone_name": "Asia/Kolkata"}
    if request.GET.get("day"):
        initial["day"] = request.GET.get("day")
    if request.GET.get("start"):
        initial["start_time"] = request.GET.get("start")
    if request.GET.get("duration"):
        initial["duration"] = request.GET.get("duration")
    if request.GET.get("tz"):
        initial["timezone_name"] = request.GET.get("tz")
    return initial


def _require_connection(request):
    connection = GoogleConnection.objects.filter(user=request.user).first()
    if connection is None or not connection.is_connected or not connection.google_enabled:
        messages.error(request, "Enable Google integration before scheduling.")
        return None
    if not connection.has_service("calendar"):
        messages.error(request, "Turn on Google Calendar in the integration panel first.")
        return None
    if not IntegrationConfig.get_solo().is_ready:
        messages.error(request, "Add the Google client id and secret in settings.")
        return None
    return connection


def _require_google_service(request, service_key: str, message: str):
    connection = GoogleConnection.objects.filter(user=request.user).first()
    if connection is None or not connection.is_connected or not connection.google_enabled:
        messages.error(request, "Enable Google integration before using Google Workspace APIs.")
        return None
    if not connection.has_service(service_key):
        messages.error(request, message)
        return None
    if not IntegrationConfig.get_solo().is_ready:
        messages.error(request, "Add the Google client id and secret in settings.")
        return None
    return connection


