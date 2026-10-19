import uuid
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from django.utils import timezone
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from portal.models import GoogleConnection, IntegrationConfig
from portal.services import IDENTITY_SCOPES, scopes_for
from portal.slots import bookable_starts, free_gaps, overlaps

SCOPES = scopes_for(("calendar", "meet"))

WORK_START = time(9, 0)
WORK_END = time(18, 0)
SLOT_STEP = timedelta(minutes=30)
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


class CalendarError(Exception):
    pass


def authorization_url(
    config: IntegrationConfig,
    state: str,
    scopes: list[str] | None = None,
    *,
    force_consent: bool = True,
) -> str:
    params = {
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes or SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "state": state,
    }
    if force_consent:
        params["prompt"] = "consent"
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(config: IntegrationConfig, code: str) -> dict:
    response = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "redirect_uri": config.redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    if response.status_code >= 400:
        raise CalendarError(_token_error(response))
    return response.json()


def fetch_userinfo(access_token: str) -> dict:
    response = requests.get(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if response.status_code >= 400:
        raise CalendarError("Google did not return the signed-in account.")
    payload = response.json()
    if not payload.get("sub") or not payload.get("email"):
        raise CalendarError("Google did not return an email for this account.")
    return payload


def _token_error(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "Google rejected the sign-in. Check the client id, secret, and redirect URL."
    code = body.get("error") or ""
    if code in {"invalid_client", "unauthorized_client"}:
        return "Google rejected the client id or secret saved in the admin portal."
    if code == "redirect_uri_mismatch":
        return "The redirect URL in the admin portal does not match the Google Cloud client."
    return "Google rejected the sign-in. Check the client id, secret, and redirect URL."


def _credentials(connection: GoogleConnection) -> Credentials:
    config = IntegrationConfig.get_solo()
    if not config.is_ready:
        raise CalendarError("Add the Google client id and secret in settings before using Google APIs.")
    if not connection.refresh_token:
        raise CalendarError("Connect Google Calendar before using the schedule.")

    stored_expiry = None
    if connection.token_expiry is not None:
        stored_expiry = connection.token_expiry.astimezone(dt_timezone.utc).replace(tzinfo=None)
    creds = Credentials(
        token=connection.access_token or None,
        refresh_token=connection.refresh_token,
        token_uri=TOKEN_URL,
        client_id=config.client_id,
        client_secret=config.client_secret,
        scopes=connection.granted_scopes or SCOPES or list(IDENTITY_SCOPES),
        expiry=stored_expiry,
    )
    try:
        if not creds.valid:
            creds.refresh(Request())
    except RefreshError as exc:
        raise CalendarError("Google Calendar access could not be refreshed. Connect Calendar again.") from exc

    connection.access_token = creds.token or ""
    if creds.refresh_token:
        connection.refresh_token = creds.refresh_token
    if creds.expiry is not None:
        connection.token_expiry = timezone.make_aware(creds.expiry, dt_timezone.utc)
    if creds.scopes:
        connection.granted_scopes = list(creds.scopes)
    connection.save(
        update_fields=[
            "access_token_encrypted",
            "refresh_token_encrypted",
            "granted_scopes",
            "token_expiry",
            "updated_at",
        ]
    )
    return creds


def _service(connection: GoogleConnection):
    return build("calendar", "v3", credentials=_credentials(connection), cache_discovery=False)


def _workspace_service(connection: GoogleConnection, api_name: str, api_version: str):
    return build(api_name, api_version, credentials=_credentials(connection), cache_discovery=False)


def _api_error(exc: HttpError) -> CalendarError:
    status = getattr(exc.resp, "status", None)
    if status in {401, 403}:
        return CalendarError("Google refused calendar access. Connect Calendar again and allow permission.")
    return CalendarError("Google Calendar could not complete that request.")


def _workspace_api_error(exc: HttpError, service_label: str) -> CalendarError:
    status = getattr(exc.resp, "status", None)
    if status in {401, 403}:
        return CalendarError(f"Google refused {service_label} access. Enable it and allow permission again.")
    return CalendarError(f"Google {service_label} could not complete that request.")


def _require_service(connection: GoogleConnection, service_key: str, label: str) -> None:
    if not connection.has_service(service_key):
        raise CalendarError(f"Turn on Google {label} in the integration panel first.")


def list_events(connection: GoogleConnection, range_start: datetime, range_end: datetime, tz_name: str) -> list[dict]:
    try:
        service = _service(connection)
        items: list[dict] = []
        page_token = None
        while True:
            response = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=range_start.isoformat(),
                    timeMax=range_end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    timeZone=tz_name,
                    maxResults=250,
                    pageToken=page_token,
                )
                .execute()
            )
            items.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return items
    except HttpError as exc:
        raise _api_error(exc) from exc
    except CalendarError:
        raise
    except Exception as exc:
        raise CalendarError("Google Calendar could not be reached.") from exc


def _is_declined(event: dict) -> bool:
    for attendee in event.get("attendees") or []:
        if attendee.get("self") and attendee.get("responseStatus") == "declined":
            return True
    return False


def _parse_timed(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, dt_timezone.utc)
        return parsed


def describe_event(event: dict, tz: ZoneInfo) -> dict | None:
    if event.get("status") == "cancelled" or event.get("transparency") == "transparent":
        return None
    if _is_declined(event):
        return None
    start = event.get("start") or {}
    end = event.get("end") or {}
    title = event.get("summary") or "(No title)"
    if "dateTime" in start and "dateTime" in end:
        start_at = _parse_timed(start["dateTime"]).astimezone(tz)
        end_at = _parse_timed(end["dateTime"]).astimezone(tz)
        return {
            "title": title,
            "all_day": False,
            "start": start_at,
            "end": end_at,
            "start_label": start_at.strftime("%H:%M"),
            "end_label": end_at.strftime("%H:%M"),
        }
    if "date" in start:
        return {
            "title": title,
            "all_day": True,
            "start": None,
            "end": None,
            "start_label": "All day",
            "end_label": "",
        }
    return None


def day_availability(
    connection: GoogleConnection,
    day: date,
    tz_name: str,
    duration_minutes: int,
    window_start_time: time | None = None,
    window_end_time: time | None = None,
) -> dict:
    tz = ZoneInfo(tz_name)
    start_clock = window_start_time or WORK_START
    end_clock = window_end_time or WORK_END
    day_start = datetime.combine(day, time.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    events = list_events(connection, day_start, day_end, tz_name)
    meetings = []
    outside = []
    busy: list[tuple[datetime, datetime]] = []
    all_day = False
    window_start = datetime.combine(day, start_clock, tzinfo=tz)
    window_end = datetime.combine(day, end_clock, tzinfo=tz)
    for event in events:
        described = describe_event(event, tz)
        if described is None:
            continue
        if described["all_day"]:
            all_day = True
            meetings.append(described)
            continue
        event_start = described["start"]
        event_end = described["end"]
        if event_start < window_end and event_end > window_start:
            meetings.append(described)
            busy.append((event_start, event_end))
        else:
            outside.append(described)

    if all_day:
        gaps: list[tuple[datetime, datetime]] = []
    else:
        gaps = free_gaps(busy, window_start, window_end)
    slots = bookable_starts(gaps, timedelta(minutes=duration_minutes), SLOT_STEP)
    return {
        "meetings": meetings,
        "outside_meetings": outside,
        "meeting_count": len(meetings),
        "open_gaps": [
            {
                "start_label": gap_start.strftime("%H:%M"),
                "end_label": gap_end.strftime("%H:%M"),
                "minutes": int((gap_end - gap_start).total_seconds() // 60),
            }
            for gap_start, gap_end in gaps
        ],
        "slots": [
            {
                "start": slot_start,
                "end": slot_end,
                "free_until": gap_end,
                "start_label": slot_start.strftime("%H:%M"),
                "end_label": slot_end.strftime("%H:%M"),
                "free_until_label": gap_end.strftime("%H:%M"),
                "start_value": slot_start.strftime("%H:%M"),
            }
            for slot_start, slot_end, gap_end in slots
        ],
        "all_day_block": all_day,
        "window_label": f"{start_clock.strftime('%H:%M')}–{end_clock.strftime('%H:%M')}",
    }


def _conflicts(connection: GoogleConnection, start: datetime, end: datetime, tz_name: str) -> bool:
    events = list_events(connection, start, end, tz_name)
    tz = start.tzinfo or ZoneInfo(tz_name)
    busy: list[tuple[datetime, datetime]] = []
    for event in events:
        described = describe_event(event, tz)
        if described is None:
            continue
        if described["all_day"]:
            return True
        busy.append((described["start"], described["end"]))
    return overlaps(start, end, busy)


def create_event(
    connection: GoogleConnection,
    *,
    title: str,
    start: datetime,
    end: datetime,
    tz_name: str,
    meet: bool,
    attendees: list[str],
) -> dict:
    if _conflicts(connection, start, end, tz_name):
        raise CalendarError("That time overlaps a meeting already on the calendar.")

    body: dict = {
        "summary": title,
        "start": {"dateTime": start.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end.isoformat(), "timeZone": tz_name},
    }
    if meet:
        body["conferenceData"] = {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
    if attendees:
        body["attendees"] = [{"email": email} for email in attendees]

    try:
        service = _service(connection)
        created = (
            service.events()
            .insert(
                calendarId="primary",
                body=body,
                conferenceDataVersion=1 if meet else 0,
                sendUpdates="all" if attendees else "none",
            )
            .execute()
        )
    except HttpError as exc:
        raise _api_error(exc) from exc
    except CalendarError:
        raise
    except Exception as exc:
        raise CalendarError("Google Calendar could not be reached.") from exc

    meet_link = created.get("hangoutLink") or ""
    if not meet_link:
        for entry in (created.get("conferenceData") or {}).get("entryPoints") or []:
            if entry.get("entryPointType") == "video" and entry.get("uri"):
                meet_link = entry["uri"]
                break
    return {
        "id": created.get("id", ""),
        "summary": created.get("summary") or title,
        "html_link": created.get("htmlLink") or "",
        "meet_link": meet_link,
        "attendees": [item.get("email", "") for item in created.get("attendees") or []],
        "start_label": start.strftime("%Y-%m-%d %H:%M"),
        "end_label": end.strftime("%H:%M"),
        "timezone": tz_name,
    }


def create_google_doc(connection: GoogleConnection, *, title: str, body: str) -> dict:
    _require_service(connection, "docs", "Docs")
    try:
        service = _workspace_service(connection, "docs", "v1")
        document = service.documents().create(body={"title": title}).execute()
        document_id = document["documentId"]
        if body.strip():
            service.documents().batchUpdate(
                documentId=document_id,
                body={
                    "requests": [
                        {
                            "insertText": {
                                "location": {"index": 1},
                                "text": body.strip() + "\n",
                            }
                        }
                    ]
                },
            ).execute()
    except HttpError as exc:
        raise _workspace_api_error(exc, "Docs") from exc
    except CalendarError:
        raise
    except Exception as exc:
        raise CalendarError("Google Docs could not be reached.") from exc

    return {
        "id": document_id,
        "title": title,
        "url": f"https://docs.google.com/document/d/{document_id}/edit",
    }


def create_google_sheet(connection: GoogleConnection, *, title: str, rows: list[list[str]]) -> dict:
    _require_service(connection, "sheets", "Sheets")
    values = [[cell.strip() for cell in row] for row in rows if any(cell.strip() for cell in row)]
    if not values:
        values = [["Name", "Status", "Notes"]]
    try:
        service = _workspace_service(connection, "sheets", "v4")
        created = (
            service.spreadsheets()
            .create(
                body={
                    "properties": {"title": title},
                    "sheets": [{"properties": {"title": "Sheet1"}}],
                }
            )
            .execute()
        )
        spreadsheet_id = created["spreadsheetId"]
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range="Sheet1!A1",
            valueInputOption="RAW",
            body={"values": values},
        ).execute()
    except HttpError as exc:
        raise _workspace_api_error(exc, "Sheets") from exc
    except CalendarError:
        raise
    except Exception as exc:
        raise CalendarError("Google Sheets could not be reached.") from exc

    return {
        "id": spreadsheet_id,
        "title": title,
        "url": created.get("spreadsheetUrl", ""),
        "rows": values,
    }


def list_google_tasks(connection: GoogleConnection) -> dict:
    _require_service(connection, "tasks", "Tasks")
    try:
        service = _workspace_service(connection, "tasks", "v1")
        tasklists = service.tasklists().list(maxResults=10).execute().get("items", [])
        tasklist_id = tasklists[0]["id"] if tasklists else "@default"
        tasks = (
            service.tasks()
            .list(tasklist=tasklist_id, showCompleted=True, showHidden=False, maxResults=20)
            .execute()
            .get("items", [])
        )
    except HttpError as exc:
        raise _workspace_api_error(exc, "Tasks") from exc
    except CalendarError:
        raise
    except Exception as exc:
        raise CalendarError("Google Tasks could not be reached.") from exc

    return {
        "tasklists": tasklists,
        "tasklist_id": tasklist_id,
        "tasks": tasks,
    }


def create_google_task(connection: GoogleConnection, *, title: str, notes: str, due: date | None) -> dict:
    _require_service(connection, "tasks", "Tasks")
    try:
        service = _workspace_service(connection, "tasks", "v1")
        tasklists = service.tasklists().list(maxResults=1).execute().get("items", [])
        tasklist_id = tasklists[0]["id"] if tasklists else "@default"
        body = {"title": title}
        if notes.strip():
            body["notes"] = notes.strip()
        if due:
            body["due"] = datetime.combine(due, time.min, tzinfo=dt_timezone.utc).isoformat().replace("+00:00", "Z")
        task = service.tasks().insert(tasklist=tasklist_id, body=body).execute()
    except HttpError as exc:
        raise _workspace_api_error(exc, "Tasks") from exc
    except CalendarError:
        raise
    except Exception as exc:
        raise CalendarError("Google Tasks could not be reached.") from exc

    return task


def get_google_form_overview(connection: GoogleConnection, *, form_id: str) -> dict:
    _require_service(connection, "forms", "Forms")
    try:
        service = _workspace_service(connection, "forms", "v1")
        form = service.forms().get(formId=form_id).execute()
        responses = service.forms().responses().list(formId=form_id, pageSize=20).execute().get("responses", [])
    except HttpError as exc:
        raise _workspace_api_error(exc, "Forms") from exc
    except CalendarError:
        raise
    except Exception as exc:
        raise CalendarError("Google Forms could not be reached.") from exc

    return {
        "id": form_id,
        "title": (form.get("info") or {}).get("title") or "Google Form",
        "edit_uri": f"https://docs.google.com/forms/d/{form_id}/edit",
        "responder_uri": form.get("responderUri") or "",
        "linked_sheet_id": form.get("linkedSheetId") or "",
        "response_count": len(responses),
        "responses": responses,
    }


def create_form_with_response_sheet(
    connection: GoogleConnection,
    *,
    title: str,
    description: str,
    questions: list[str],
    collect_email: bool,
    create_sheet: bool,
) -> dict:
    if not connection.has_service("forms"):
        raise CalendarError("Turn on Google Forms in the integration panel first.")
    if create_sheet and not connection.has_service("sheets"):
        raise CalendarError("Turn on Google Sheets to create the response workbook.")

    clean_questions = [question.strip() for question in questions if question.strip()]
    if not clean_questions:
        raise CalendarError("Add at least one form question.")

    try:
        forms_service = _workspace_service(connection, "forms", "v1")
        form = (
            forms_service.forms()
            .create(body={"info": {"title": title, "documentTitle": title}})
            .execute()
        )
        form_id = form["formId"]

        requests = [
            {
                "updateFormInfo": {
                    "info": {"description": description},
                    "updateMask": "description",
                }
            }
        ]
        if collect_email:
            requests.append(
                {
                    "updateSettings": {
                        "settings": {"emailCollectionType": "RESPONDER_INPUT"},
                        "updateMask": "emailCollectionType",
                    }
                }
            )
        for index, question in enumerate(clean_questions):
            requests.append(
                {
                    "createItem": {
                        "item": {
                            "title": question,
                            "questionItem": {
                                "question": {
                                    "required": True,
                                    "textQuestion": {"paragraph": False},
                                }
                            },
                        },
                        "location": {"index": index},
                    }
                }
            )
        forms_service.forms().batchUpdate(
            formId=form_id,
            body={"requests": requests},
        ).execute()
        forms_service.forms().setPublishSettings(
            formId=form_id,
            body={
                "publishSettings": {
                    "publishState": {
                        "isPublished": True,
                        "isAcceptingResponses": True,
                    }
                },
                "updateMask": "publishState",
            },
        ).execute()
        form = forms_service.forms().get(formId=form_id).execute()
    except HttpError as exc:
        raise _workspace_api_error(exc, "Forms") from exc
    except CalendarError:
        raise
    except Exception as exc:
        raise CalendarError("Google Forms could not be reached.") from exc

    sheet = None
    if create_sheet:
        sheet = _create_response_sheet(connection, title, clean_questions, collect_email)

    return {
        "form_id": form.get("formId", ""),
        "form_title": (form.get("info") or {}).get("title") or title,
        "responder_uri": form.get("responderUri") or "",
        "edit_uri": f"https://docs.google.com/forms/d/{form.get('formId', '')}/edit",
        "linked_sheet_id": form.get("linkedSheetId") or "",
        "sheet": sheet,
        "questions": clean_questions,
    }


def _create_response_sheet(
    connection: GoogleConnection,
    title: str,
    questions: list[str],
    collect_email: bool,
) -> dict:
    headers = ["Timestamp"]
    if collect_email:
        headers.append("Email Address")
    headers.extend(questions)
    try:
        sheets_service = _workspace_service(connection, "sheets", "v4")
        created = (
            sheets_service.spreadsheets()
            .create(
                body={
                    "properties": {"title": f"{title} responses"},
                    "sheets": [{"properties": {"title": "Form Responses"}}],
                }
            )
            .execute()
        )
        spreadsheet_id = created["spreadsheetId"]
        sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range="'Form Responses'!A1",
            valueInputOption="RAW",
            body={"values": [headers]},
        ).execute()
    except HttpError as exc:
        raise _workspace_api_error(exc, "Sheets") from exc
    except CalendarError:
        raise
    except Exception as exc:
        raise CalendarError("Google Sheets could not be reached.") from exc

    return {
        "spreadsheet_id": created.get("spreadsheetId", ""),
        "spreadsheet_url": created.get("spreadsheetUrl", ""),
        "headers": headers,
    }
