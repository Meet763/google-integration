from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from portal.crypto import decrypt, encrypt
from portal.forms import (
    EventForm,
    GoogleConnectForm,
    GoogleFormBuilderForm,
    GoogleFormLookupForm,
    GoogleSheetForm,
    ScheduleForm,
)
from portal.models import IntegrationConfig
from portal.services import missing_scopes, scopes_for
from portal.slots import bookable_starts, free_gaps, overlaps


class SlotTests(TestCase):
    def test_sample_day_hides_gaps_shorter_than_the_duration(self):
        tz = ZoneInfo("Asia/Kolkata")

        def at(hour, minute=0):
            return datetime(2026, 10, 6, hour, minute, tzinfo=tz)

        busy = [
            (at(9, 0), at(9, 30)),
            (at(11, 0), at(12, 0)),
            (at(14, 0), at(15, 30)),
            (at(16, 0), at(16, 30)),
        ]
        gaps = free_gaps(busy, at(9, 0), at(18, 0))
        starts = [slot[0].strftime("%H:%M") for slot in bookable_starts(gaps, timedelta(minutes=45), timedelta(minutes=30))]
        self.assertEqual(starts, ["09:30", "10:00", "12:00", "12:30", "13:00", "16:30", "17:00"])
        self.assertTrue(overlaps(at(11, 15), at(11, 45), busy))
        self.assertFalse(overlaps(at(12, 0), at(12, 45), busy))


class CryptoTests(TestCase):
    def test_round_trip(self):
        token = "refresh-token-value"
        self.assertEqual(decrypt(encrypt(token)), token)
        self.assertEqual(decrypt(""), "")


class PortalFlowTests(TestCase):
    @override_settings(GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET="")
    def test_first_visit_explains_code_configuration(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Waiting for the Google client")
        self.assertContains(response, "config/settings.py")

    @override_settings(
        GOOGLE_CLIENT_ID="client-id.apps.googleusercontent.com",
        GOOGLE_CLIENT_SECRET="super-secret",
        GOOGLE_REDIRECT_URI="http://127.0.0.1:8000/oauth/callback/",
        GOOGLE_MEET_ALLOWED=True,
        GOOGLE_ATTENDEES_ALLOWED=False,
    )
    def test_google_client_can_be_configured_from_settings(self):
        config = IntegrationConfig.get_solo()
        self.assertEqual(config.client_id, "client-id.apps.googleusercontent.com")
        self.assertEqual(config.client_secret, "super-secret")
        self.assertTrue(config.meet_allowed)
        self.assertFalse(config.attendees_allowed)
        self.assertNotIn("super-secret", config.client_secret_encrypted)

        user = User.objects.create_user("person@example.com", "person@example.com", "unused-pass-123")
        self.client.force_login(user)
        page = self.client.get(reverse("event_new"))
        self.assertEqual(page.status_code, 302)

    def test_event_form_drops_attendees_when_admin_disallows_them(self):
        config = IntegrationConfig.get_solo()
        config.meet_allowed = True
        config.attendees_allowed = False
        config.save()
        form = EventForm(
            data={
                "title": "Review",
                "day": "2026-10-06",
                "start_time": "12:00",
                "duration": 45,
                "timezone_name": "Asia/Kolkata",
                "meet_enabled": "on",
                "attendees_enabled": "on",
                "attendees": "guest@example.com",
            },
            config=config,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.cleaned_data["meet_enabled"])
        self.assertFalse(form.cleaned_data["attendees_enabled"])
        self.assertEqual(form.cleaned_data["attendee_emails"], [])

    def test_anonymous_schedule_returns_home(self):
        response = self.client.get(reverse("schedule"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("home"), response.url)

    def test_schedule_window_must_end_after_it_starts(self):
        form = ScheduleForm(
            data={
                "day": "2026-10-06",
                "window_start": "14:00",
                "window_end": "11:00",
                "duration": 45,
                "timezone_name": "Asia/Kolkata",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("window_end", form.errors)

    def test_google_services_need_a_selection_when_enabled(self):
        form = GoogleConnectForm(data={"google_enabled": "on"})
        self.assertFalse(form.is_valid())
        off = GoogleConnectForm(data={})
        self.assertTrue(off.is_valid(), off.errors)
        self.assertFalse(off.cleaned_data["google_enabled"])

    def test_selected_services_request_only_those_scopes(self):
        scopes = scopes_for(["docs", "forms", "tasks"])
        self.assertIn("https://www.googleapis.com/auth/documents", scopes)
        self.assertIn("https://www.googleapis.com/auth/forms.body", scopes)
        self.assertIn("https://www.googleapis.com/auth/forms.responses.readonly", scopes)
        self.assertIn("https://www.googleapis.com/auth/tasks", scopes)
        self.assertNotIn("https://www.googleapis.com/auth/calendar.events", scopes)
        self.assertEqual(missing_scopes(scopes, scopes), [])

    def test_google_form_builder_parses_question_lines(self):
        form = GoogleFormBuilderForm(
            data={
                "title": "Intake",
                "description": "Tell us what you need.",
                "questions": "Name\nCompany\nName\nWhat do you need?",
                "collect_email": "on",
                "create_sheet": "on",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["questions"], ["Name", "Company", "What do you need?"])

    def test_google_sheet_form_parses_csv_rows(self):
        form = GoogleSheetForm(data={"title": "Pipeline", "rows": "Name, Status\nAda, Open"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["rows"], [["Name", "Status"], ["Ada", "Open"]])

    def test_google_form_lookup_accepts_full_url(self):
        form = GoogleFormLookupForm(
            data={"form_id": "https://docs.google.com/forms/d/abc123/edit"}
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["form_id"], "abc123")
