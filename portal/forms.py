from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from portal.models import GoogleConnection, IntegrationConfig
from portal.services import GOOGLE_SERVICES, SERVICE_KEYS

TIMEZONES = [
    ("Asia/Kolkata", "India — Asia/Kolkata"),
    ("UTC", "UTC"),
    ("America/New_York", "US Eastern — America/New_York"),
    ("America/Chicago", "US Central — America/Chicago"),
    ("America/Los_Angeles", "US Pacific — America/Los_Angeles"),
    ("Europe/London", "UK — Europe/London"),
    ("Europe/Berlin", "Central Europe — Europe/Berlin"),
    ("Asia/Dubai", "Gulf — Asia/Dubai"),
    ("Asia/Singapore", "Singapore — Asia/Singapore"),
    ("Australia/Sydney", "Sydney — Australia/Sydney"),
]

DURATION_CHOICES = [
    (30, "30 min"),
    (45, "45 min"),
    (60, "60 min"),
    (90, "90 min"),
]


class GoogleConnectForm(forms.Form):
    google_enabled = forms.BooleanField(required=False, label="Enable Google integration")
    services = forms.MultipleChoiceField(
        required=False,
        choices=[(item["key"], item["label"]) for item in GOOGLE_SERVICES],
        widget=forms.CheckboxSelectMultiple,
        label="Google services",
    )

    def clean(self):
        cleaned = super().clean()
        selected = [key for key in cleaned.get("services") or [] if key in SERVICE_KEYS]
        if cleaned.get("google_enabled") and not selected:
            self.add_error("services", "Choose at least one Google service to enable.")
        cleaned["services"] = selected
        return cleaned


class ScheduleForm(forms.Form):
    day = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    window_start = forms.TimeField(
        label="From",
        initial="09:00",
        widget=forms.TimeInput(attrs={"type": "time"}),
        input_formats=["%H:%M", "%H:%M:%S"],
    )
    window_end = forms.TimeField(
        label="Until",
        initial="18:00",
        widget=forms.TimeInput(attrs={"type": "time"}),
        input_formats=["%H:%M", "%H:%M:%S"],
    )
    duration = forms.TypedChoiceField(
        choices=DURATION_CHOICES,
        coerce=int,
        label="Duration",
        initial=45,
        widget=forms.RadioSelect,
    )
    timezone_name = forms.ChoiceField(choices=TIMEZONES, label="Time zone", initial="Asia/Kolkata")

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("window_start")
        end = cleaned.get("window_end")
        if start and end and end <= start:
            self.add_error("window_end", "Until must be later than From on the same day.")
        return cleaned


class EventForm(forms.Form):
    title = forms.CharField(max_length=200, label="Title")
    day = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    start_time = forms.TimeField(
        label="Start",
        widget=forms.TimeInput(attrs={"type": "time"}),
        input_formats=["%H:%M", "%H:%M:%S"],
    )
    duration = forms.IntegerField(min_value=15, max_value=480, label="Duration (minutes)", initial=45)
    timezone_name = forms.ChoiceField(choices=TIMEZONES, label="Time zone", initial="Asia/Kolkata")
    meet_enabled = forms.BooleanField(required=False, label="Add a Google Meet link")
    attendees_enabled = forms.BooleanField(required=False, label="Add attendees")
    attendees = forms.CharField(
        required=False,
        label="Attendee emails",
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "one@example.com"}),
        help_text="One email address per line.",
    )

    def __init__(
        self,
        *args,
        config: IntegrationConfig | None = None,
        connection: GoogleConnection | None = None,
        **kwargs,
    ):
        self.config = config or IntegrationConfig.get_solo()
        self.connection = connection
        super().__init__(*args, **kwargs)
        meet_ok = self.config.meet_allowed and (
            connection is None or connection.has_service("meet")
        )
        if not meet_ok:
            self.fields["meet_enabled"].disabled = True
        if not self.config.attendees_allowed:
            self.fields["attendees_enabled"].disabled = True
            self.fields["attendees"].disabled = True

    def clean_timezone_name(self):
        name = self.cleaned_data["timezone_name"]
        try:
            ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise ValidationError("Choose a valid time zone.") from exc
        return name

    def clean(self):
        cleaned = super().clean()
        meet_ok = self.config.meet_allowed and (
            self.connection is None or self.connection.has_service("meet")
        )
        if not meet_ok:
            cleaned["meet_enabled"] = False
        if not self.config.attendees_allowed:
            cleaned["attendees_enabled"] = False
            cleaned["attendees"] = ""

        emails: list[str] = []
        if cleaned.get("attendees_enabled"):
            raw = cleaned.get("attendees") or ""
            for line in raw.replace(",", "\n").splitlines():
                email = line.strip().lower()
                if not email:
                    continue
                try:
                    validate_email(email)
                except ValidationError:
                    self.add_error("attendees", f"{email} is not a valid email address.")
                    continue
                if email not in emails:
                    emails.append(email)
            if not emails and not self.errors.get("attendees"):
                self.add_error("attendees", "Add at least one attendee email, or turn attendees off.")
            if len(emails) > 20:
                self.add_error("attendees", "Add at most 20 attendees.")
        cleaned["attendee_emails"] = emails

        day = cleaned.get("day")
        start_time = cleaned.get("start_time")
        duration = cleaned.get("duration")
        tz_name = cleaned.get("timezone_name")
        if day and start_time and duration and tz_name and "timezone_name" not in self.errors:
            start = datetime.combine(day, start_time, tzinfo=ZoneInfo(tz_name))
            cleaned["start_at"] = start
            cleaned["end_at"] = start + timedelta(minutes=duration)
        return cleaned


class GoogleFormBuilderForm(forms.Form):
    title = forms.CharField(max_length=200, label="Form title")
    description = forms.CharField(
        required=False,
        label="Description",
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    questions = forms.CharField(
        label="Questions",
        widget=forms.Textarea(attrs={"rows": 6, "placeholder": "Name\nCompany\nWhat do you need help with?"}),
        help_text="One short-answer question per line.",
    )
    collect_email = forms.BooleanField(required=False, label="Collect email address")
    create_sheet = forms.BooleanField(
        required=False,
        initial=True,
        label="Create a matching Google Sheet",
        help_text="The Forms API exposes the linked sheet id as read-only, so this creates the workbook and headers for the response destination.",
    )

    def clean_questions(self):
        raw = self.cleaned_data["questions"]
        questions = []
        for line in raw.splitlines():
            question = line.strip()
            if question and question not in questions:
                questions.append(question)
        if not questions:
            raise ValidationError("Add at least one question.")
        if len(questions) > 20:
            raise ValidationError("Add at most 20 questions.")
        return questions


class GoogleDocForm(forms.Form):
    title = forms.CharField(max_length=200, label="Document title")
    body = forms.CharField(
        required=False,
        label="Body",
        widget=forms.Textarea(attrs={"rows": 10, "placeholder": "Draft notes, agenda, or summary..."}),
    )


class GoogleSheetForm(forms.Form):
    title = forms.CharField(max_length=200, label="Spreadsheet title")
    rows = forms.CharField(
        required=False,
        label="Rows",
        widget=forms.Textarea(attrs={"rows": 8, "placeholder": "Name, Status, Notes\nAda, Open, First follow-up"}),
        help_text="One row per line. Separate cells with commas.",
    )

    def clean_rows(self):
        raw = self.cleaned_data.get("rows") or ""
        rows = []
        for line in raw.splitlines():
            cells = [cell.strip() for cell in line.split(",")]
            if any(cells):
                rows.append(cells)
        if len(rows) > 100:
            raise ValidationError("Add at most 100 rows in this POC.")
        return rows


class GoogleTaskForm(forms.Form):
    title = forms.CharField(max_length=200, label="Task title")
    notes = forms.CharField(
        required=False,
        label="Notes",
        widget=forms.Textarea(attrs={"rows": 4}),
    )
    due = forms.DateField(required=False, label="Due date", widget=forms.DateInput(attrs={"type": "date"}))


class GoogleFormLookupForm(forms.Form):
    form_id = forms.CharField(
        max_length=200,
        label="Form ID",
        help_text="Paste the ID from a Google Form URL.",
    )

    def clean_form_id(self):
        value = self.cleaned_data["form_id"].strip()
        if "/forms/d/" in value:
            value = value.split("/forms/d/", 1)[1].split("/", 1)[0]
        return value
